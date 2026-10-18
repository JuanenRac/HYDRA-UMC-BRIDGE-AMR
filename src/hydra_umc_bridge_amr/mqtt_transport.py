# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Real VDA 5050 MQTT transport
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Publish a gated AmrDispatch as a real, schema-shaped VDA 5050 MQTT message.

This module never computes a route or plans a path - it stays exactly
inside AmrCoordinator's own documented boundary. What it adds is real: a
correctly-shaped VDA 5050 topic
(`{interfaceName}/{majorVersion}/{manufacturer}/{serialNumber}/{topic}`,
github.com/VDA5050/VDA5050 section 4.2) and a minimal, spec-correct message
body per channel - a real "order" message describes exactly ONE destination
node (this bridge's own resolved local_x/local_y) with the action attached
to it and an empty edge list (a destination node only: a route that starts
at the node the AGV stands on needs its reported position and the plant graph,
which are not wired in yet); a real "instantActions" message carries the action
directly. Action types are the ones VDA 5050 defines (`pick`, `drop`,
`cancelOrder`); a movement has no action type of its own. Only a
dispatch the shared SDK gate already accepted is ever published - a
rejected AmrDispatch never reaches the network.

The MQTT client identifies itself with a client id (the broker only accepts a
credential whose client id starts with the credential's prefix), waits for the
broker to accept the connection, and can listen to the AGV's `state` and
`connection` topics through a `Vda5050Observer`. Nothing here has run against a
real AGV or fleet manager.
"""

from __future__ import annotations

import json
import logging
import secrets
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from hydra_umc_sdk.mqtt_connection import apply_connection_settings, settings_from_env

from .coordinator import AmrCoordinator, AmrDispatch

_LOG = logging.getLogger(__name__)

DEFAULT_CLIENT_ID = "hydra-umc-bridge-amr"


@dataclass(frozen=True)
class Vda5050Target:
    """Identifies one real AGV on the VDA 5050 MQTT bus."""

    interface_name: str  # e.g. "vda5050" - the shared root topic segment
    major_version: str  # e.g. "v2" or "v3" - VDA 5050's own topic-level version segment
    manufacturer: str
    serial_number: str
    protocol_version: str = "2.0.0"  # the "version" header field - full semver, per spec

    def topic(self, channel: str) -> str:
        return f"{self.interface_name}/{self.major_version}/{self.manufacturer}/{self.serial_number}/{channel}"


class MqttPublisher(Protocol):
    """The minimal real interface this module depends on - matches paho-mqtt's Client.publish()."""

    def publish(self, topic: str, payload: str) -> object: ...


def connect_with_retry(
    connect,
    *,
    max_attempts: int = 5,
    retry_delay_seconds: float = 2.0,
    sleep=time.sleep,
) -> None:
    """Retries `connect()` on OSError (what an unreachable broker actually
    raises - connection refused, timeout), tolerating the real startup
    race a systemd unit for this bridge hits if it starts before
    HYDRA-UMC-MQTT-BROKER is listening yet (both are independent systemd
    units with no ordering guarantee across a real reboot). Only OSError is retried;
    anything else is a real bug, not a transient startup race, and
    propagates immediately. A TLS error (an invalid certificate) will not get
    better by waiting: it gives up at once. `sleep` is injectable so tests can
    prove the retry/give-up behavior without a real multi-second wait."""
    last_error: OSError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            connect()
            return
        except ssl.SSLError as error:
            raise RuntimeError(f"the TLS handshake with the MQTT broker failed, not retrying: {error}") from error
        except OSError as error:
            last_error = error
            if attempt < max_attempts:
                sleep(retry_delay_seconds)
    raise RuntimeError(
        f"could not reach the MQTT broker after {max_attempts} attempts "
        f"({retry_delay_seconds}s apart): {last_error}"
    ) from last_error


def open_mqtt_client(
    host: str,
    port: int = 1883,
    *,
    keepalive_seconds: int = 60,
    max_connect_attempts: int = 5,
    connect_retry_delay_seconds: float = 2.0,
    username: str | None = None,
    password: str | None = None,
    client_id: str = DEFAULT_CLIENT_ID,
    connack_timeout_seconds: float = 10.0,
    observer: "Observer | None" = None,
) -> MqttPublisher:
    """Open a real MQTT connection. The only place this module imports paho-mqtt.

    Raises RuntimeError with a clear message if paho-mqtt isn't installed,
    rather than letting an ImportError surface from deep inside this module.
    The initial connect is retried (see connect_with_retry()) - once
    connected, paho-mqtt's own loop_start() background thread already
    handles a later mid-session drop/reconnect on its own, so only the
    first connect needed this.

    The broker only authenticates a credential whose client id starts with the
    credential's prefix, so the client id is explicit (`client_id`). A refused
    login arrives as a CONNACK after `connect()` has returned: this waits up to
    `connack_timeout_seconds` for the broker's answer and raises RuntimeError if
    it is a refusal or does not come, instead of returning a client whose every
    publish would fail. With an `observer` (see observer.py) it also subscribes to
    the AGV's `state` and `connection` topics, again after every reconnect.
    """

    if password is not None and username is None:
        raise ValueError("password was given without a username - MQTT credentials need both")

    try:
        import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
    except ImportError as error:
        raise RuntimeError(
            "paho-mqtt is not installed - install it to publish real VDA 5050 orders "
            "(this module's payload-building/gating logic works and is tested without it)"
        ) from error
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    # the credentials and TLS of this service's environment (HYDRA_MQTT_*), unless the caller gave a user name explicitly
    apply_connection_settings(client, settings_from_env(), username=username, password=password)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    answered = threading.Event()
    refusal: list[str] = []

    def on_connect(client: object, userdata: object, flags: object, reason_code: object, properties: object = None) -> None:
        if getattr(reason_code, "is_failure", False):
            refusal.append(str(reason_code))
        elif observer is not None:
            client.subscribe(observer.subscriptions())  # type: ignore[attr-defined]
        answered.set()

    def on_disconnect(client: object, userdata: object, *args: object) -> None:
        _LOG.warning("the MQTT connection to the broker was lost; the client reconnects on its own")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    if observer is not None:
        client.on_message = lambda _client, _userdata, message: observer.on_message(message.topic, bytes(message.payload))

    connect_with_retry(
        lambda: client.connect(host, port, keepalive=keepalive_seconds),
        max_attempts=max_connect_attempts,
        retry_delay_seconds=connect_retry_delay_seconds,
    )
    client.loop_start()
    if not answered.wait(connack_timeout_seconds):
        client.loop_stop()
        raise RuntimeError(f"the MQTT broker did not answer the connection within {connack_timeout_seconds}s")
    if refusal:
        client.loop_stop()
        raise RuntimeError(f"the MQTT broker refused the connection: {refusal[0]}")
    return client


class Observer(Protocol):
    """What `open_mqtt_client` needs of an observer (`Vda5050Observer`)."""

    def subscriptions(self) -> list[tuple[str, int]]: ...
    def on_message(self, topic: str, payload: bytes) -> None: ...


@dataclass(frozen=True)
class PublishResult:
    published: bool
    reason: str
    topic: str | None = None


def _utc_timestamp() -> str:
    """The ISO 8601 UTC time with milliseconds that VDA 5050 asks for in every header."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(None)


class Vda5050Publisher:
    """Build and publish a real VDA 5050 message for one already-gated AmrDispatch.

    VDA 5050 refuses a new order that reuses the `orderId` of an earlier one with `orderUpdateId` 0, and a bridge that restarts counts its `headerId` from the beginning again:
    an `orderId` made from the header id alone then collides with the orders of the previous run. The publisher therefore adds a random token of its own to the `orderId`,
    to the `actionId` and to the `nodeId` (one per publisher, so it differs after every restart; the AGV correlates its action states by `actionId`),
    unless the caller names the order (`order_id`, for example the job id).

    `header_id` and `timestamp_iso` may be left out (None): the publisher then counts the header ids itself, per topic, and stamps the current UTC time.
    When a caller does give them they must be a non-negative integer and an ISO 8601 UTC timestamp, which VDA 5050 requires.
    """

    def __init__(self, clock: Callable[[], str] = _utc_timestamp) -> None:
        self._session = secrets.token_hex(4)
        self._clock = clock
        self._header_ids: dict[str, int] = {}
        self._lock = threading.Lock()

    def _next_header_id(self, topic: str) -> int:
        with self._lock:
            self._header_ids[topic] = self._header_ids.get(topic, 0) + 1
            return self._header_ids[topic]

    def publish(
        self,
        client: MqttPublisher,
        target: Vda5050Target,
        header_id: int | None,
        timestamp_iso: str | None,
        dispatch: AmrDispatch,
        order_id: str | None = None,
    ) -> PublishResult:
        # A rejected dispatch (the shared SDK gate already said no) must
        # never reach the network - the transport layer is not a second
        # place to reconsider a safety decision already made.
        if not dispatch.accepted:
            return PublishResult(False, dispatch.reason)

        topic = target.topic(dispatch.channel)
        if header_id is None:
            header_id = self._next_header_id(topic)
        elif not isinstance(header_id, int) or isinstance(header_id, bool) or header_id < 0:
            return PublishResult(False, f"header_id must be a non-negative integer, got {header_id!r}", topic)
        if timestamp_iso is None:
            timestamp_iso = self._clock()
        elif not _valid_timestamp(timestamp_iso):
            return PublishResult(False, f"timestamp_iso must be an ISO 8601 UTC timestamp, got {timestamp_iso!r}", topic)

        header = {
            "headerId": header_id,
            "timestamp": timestamp_iso,
            "version": target.protocol_version,
            "manufacturer": target.manufacturer,
            "serialNumber": target.serial_number,
        }
        wire_type = AmrCoordinator.VDA_ACTION_TYPES.get(dispatch.action)
        unique = f"{dispatch.action.lower()}-{self._session}-{header_id}"
        action = {"actionId": unique, "actionType": wire_type, "blockingType": "HARD"} if wire_type else None

        if dispatch.channel == "instantActions":
            if action is None:
                return PublishResult(False, f"{dispatch.action} has no VDA 5050 instant action type", topic)
            # HARD blocks everything else, per VDA 5050's own real blockingType enum
            # (NONE/SOFT/SINGLE/HARD) - a cancel-order-class action must preempt, not
            # queue behind whatever the AGV is already doing.
            payload = {**header, "actions": [action]}
        else:
            node: dict[str, object] = {
                "nodeId": unique,
                "sequenceId": 0,
                "released": True,
                # a movement is the node itself: it carries no action type
                "actions": [action] if action else [],
            }
            # nodePosition is only meaningful for a movement action -
            # PICK_LOAD/DROP_LOAD resolve no coordinate (see coordinator.py's
            # own _MOVEMENT_ACTIONS), and VDA 5050 allows an omitted
            # nodePosition for a node the AGV already has a fixed location
            # for.
            if dispatch.local_x is not None and dispatch.local_y is not None:
                # VDA 5050 2.0.0's own real <nodePosition> schema
                # requires x, y AND mapId together
                # (github.com/VDA5050/VDA5050/blob/2.0.0/json_schemas/order.schema) -
                # a message with x/y but no mapId is not spec-valid, so a
                # dispatch missing one (which should never happen once a
                # real AmrCoordinator resolved local_x/local_y from a real
                # FrameTransform - see that class's own required map_id
                # field) is refused here rather than published anyway.
                if not dispatch.map_id:
                    return PublishResult(False, "movement dispatch is missing a real map_id - refusing to publish a non-spec-compliant nodePosition", topic)
                node["nodePosition"] = {"x": dispatch.local_x, "y": dispatch.local_y, "mapId": dispatch.map_id}
            payload = {
                **header,
                "orderId": order_id or f"hydra-umc-{self._session}-{header_id}",
                "orderUpdateId": 0,
                "nodes": [node],
                # A single-node order has nothing to traverse - this bridge
                # was never meant to plan a multi-waypoint route (see this
                # module's own docstring), so an empty edge list is the
                # real, correct shape here, not a placeholder.
                "edges": [],
            }

        try:
            # allow_nan=False: JSON has no NaN/Infinity, and an AGV cannot read them
            message = json.dumps(payload, allow_nan=False)
        except ValueError as error:
            return PublishResult(False, f"the message cannot be encoded as JSON: {error}", topic)
        try:
            result = client.publish(topic, message)
        except OSError as error:
            return PublishResult(False, f"MQTT publish failed: {error}", topic)

        # paho-mqtt's own real Client.publish() does NOT raise on "not
        # connected" - it returns an MQTTMessageInfo whose own `.rc` field
        # (mqtt.MQTT_ERR_SUCCESS == 0 on success, MQTT_ERR_NO_CONN == 4
        # with no live connection, among other real paho-mqtt result
        # codes) is the only real signal a caller gets. Read
        # defensively (`getattr`, default 0/success) rather than assumed
        # present, since a minimal test double's own return value may not
        # carry it.
        rc = getattr(result, "rc", 0)
        if rc != 0:
            return PublishResult(False, f"MQTT publish not queued (rc={rc})", topic)

        # Honest about what this actually confirms: the local MQTT client
        # queued the message for delivery (rc == MQTT_ERR_SUCCESS). It
        # does NOT mean the broker delivered it, nor that the AGV itself
        # received or accepted the order - VDA 5050's own real
        # acceptance/execution ACK arrives later, asynchronously, on that
        # AGV's own state topic (see observer.py), which is not correlated
        # to individual orders here.
        return PublishResult(True, "queued for MQTT delivery (rc=0) - not a confirmed AGV acceptance", topic)
