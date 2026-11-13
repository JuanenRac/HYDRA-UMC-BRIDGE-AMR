# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - What the vehicle reports on its VDA 5050 state and connection topics
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Turn the AGV's own `state` and `connection` messages into the machine state the gate uses.

The bridge used to publish and never listen, so the gate could only believe the
`machine_state` a requester wrote in the job (a message anybody on the broker may
publish). `Vda5050Observer` keeps the last `state` and `connection` of one AGV and
answers `machine_state()`:

* OFFLINE - nothing heard yet, the connection topic says the AGV is not ONLINE, the
  last state is older than `max_age_seconds`, or the AGV is not in AUTOMATIC mode
  (it does not take orders from the master control);
* SAFE_STOP - an emergency stop is active; FAULT - a field violation or a FATAL error;
* HOLDING - the AGV is paused;
* RUNNING - it is driving, or an order still has nodes or actions to go;
* IDLE - none of the above.

`max_age_seconds` defaults to three times the 30 s at which VDA 5050 says an AGV
repeats its state when nothing changes. Everything is taken from a message that
somebody could still forge on a broker without access control: the broker's ACL
decides who may publish on these topics. This has not been run against a real AGV or
fleet manager, only against the emulator in tests/.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable

from hydra_umc_sdk.bridge_contract import MachineState

from .mqtt_transport import Vda5050Target

_DEFAULT_MAX_AGE_SECONDS = 90.0
# Environment variable that caps the size of one `state` or `connection` message the observer will parse (bytes). Unset: no cap, as before.
MAX_MESSAGE_BYTES_ENV = "HYDRA_UMC_AMR_MAX_STATE_BYTES"
# Environment variable that makes the observer also listen to the `factsheet` of the AGV (the answer to a `factsheetRequest`). Unset or anything but 1: off.
FACTSHEET_ENV = "HYDRA_UMC_AMR_FACTSHEET"
_ACTIVE_ACTION_STATUSES = {"WAITING", "INITIALIZING", "RUNNING", "PAUSED"}


class Vda5050Observer:
    """The last state and connection of one AGV. Safe to feed from the MQTT thread and read from another."""

    def __init__(
        self,
        target: Vda5050Target,
        *,
        max_age_seconds: float = _DEFAULT_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_message_bytes: int | None = None,
        factsheet: bool | None = None,
    ) -> None:
        """`max_message_bytes` caps the size of one message that is parsed; a larger one is ignored like any unreadable message. None reads
        `HYDRA_UMC_AMR_MAX_STATE_BYTES`, and without it there is no cap (the behaviour before the option existed).
        `factsheet` (None reads `HYDRA_UMC_AMR_FACTSHEET`, off unless it is `1`) also keeps the last `factsheet` of the vehicle: which action types it
        supports, so a caller can tell an order the vehicle cannot run before sending it. Off, the subscriptions are exactly what they were."""
        if max_message_bytes is None:
            raw = os.environ.get(MAX_MESSAGE_BYTES_ENV, "").strip()
            if raw:
                try:
                    max_message_bytes = int(raw)
                except ValueError:
                    raise ValueError(f"{MAX_MESSAGE_BYTES_ENV} must be a positive whole number of bytes") from None
        if max_message_bytes is not None and (
            not isinstance(max_message_bytes, int) or isinstance(max_message_bytes, bool) or max_message_bytes <= 0
        ):
            raise ValueError("max_message_bytes must be a positive whole number of bytes")
        self._max_message_bytes = max_message_bytes
        if not isinstance(max_age_seconds, (int, float)) or isinstance(max_age_seconds, bool) or not max_age_seconds > 0:
            raise ValueError("max_age_seconds must be a positive number of seconds")
        self._target = target
        self._max_age = float(max_age_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._state_at = 0.0
        self._state_wall_ms = 0
        self._connection: str | None = None
        self._watch_factsheet = factsheet if factsheet is not None else os.environ.get(FACTSHEET_ENV, "").strip() == "1"
        self._factsheet: dict | None = None

    @property
    def state_topic(self) -> str:
        return self._target.topic("state")

    @property
    def connection_topic(self) -> str:
        return self._target.topic("connection")

    @property
    def factsheet_topic(self) -> str:
        return self._target.topic("factsheet")

    def subscriptions(self) -> list[tuple[str, int]]:
        """(topic, qos) to subscribe to: the connection with QoS 1 (it is the Last Will), the state with QoS 0, and with the factsheet switch on the factsheet with QoS 1."""
        topics = [(self.connection_topic, 1), (self.state_topic, 0)]
        if self._watch_factsheet:
            topics.append((self.factsheet_topic, 1))
        return topics

    def on_message(self, topic: str, payload: bytes) -> None:
        """Take one message of the AGV's topics. Anything unreadable is ignored: it can only make the AGV look less known."""
        if topic not in (self.state_topic, self.connection_topic) and not (self._watch_factsheet and topic == self.factsheet_topic):
            return
        if self._max_message_bytes is not None and len(payload) > self._max_message_bytes:
            return  # larger than the configured cap: never parsed
        try:
            body = json.loads(payload)
        except Exception:  # noqa: BLE001 - invalid JSON, deep nesting, bad encoding: not a message
            return
        if not isinstance(body, dict):
            return
        if body.get("manufacturer") != self._target.manufacturer or body.get("serialNumber") != self._target.serial_number:
            return  # another vehicle, or a forged header
        with self._lock:
            if topic == self.factsheet_topic:
                self._factsheet = body
            elif topic == self.connection_topic:
                connection = body.get("connectionState")
                self._connection = connection if isinstance(connection, str) else None
            else:
                self._state = body
                self._state_at = self._clock()
                self._state_wall_ms = int(time.time() * 1000)

    @property
    def factsheet(self) -> dict | None:
        """A copy of the last `factsheet` of this vehicle, or None when none was heard (or the switch is off)."""
        with self._lock:
            sheet = self._factsheet
        return json.loads(json.dumps(sheet)) if sheet is not None else None

    @property
    def supported_action_types(self) -> frozenset[str] | None:
        """The action types the vehicle says it supports (`protocolFeatures.agvActions[].actionType`), or None when there is no factsheet or it lists none."""
        sheet = self.factsheet
        features = sheet.get("protocolFeatures") if sheet else None
        actions = features.get("agvActions") if isinstance(features, dict) else None
        if not isinstance(actions, list):
            return None
        types = frozenset(a["actionType"] for a in actions if isinstance(a, dict) and isinstance(a.get("actionType"), str))
        return types or None

    def supports_action(self, action_type: str) -> bool | None:
        """True or False when the factsheet settles it, None when it does not (no factsheet heard): the answer "unknown" is never a yes."""
        types = self.supported_action_types
        return None if types is None else action_type in types

    @property
    def connection_state(self) -> str | None:
        with self._lock:
            return self._connection

    @property
    def last_node_id(self) -> str | None:
        with self._lock:
            node = self._state.get("lastNodeId") if self._state else None
        return node if isinstance(node, str) and node else None

    def snapshot(self) -> tuple[dict, int] | None:
        """A copy of the last `state` message and the wall-clock milliseconds at which it arrived; None when it is unknown, offline or older than `max_age_seconds`."""
        with self._lock:
            state, state_at, wall = self._state, self._state_at, self._state_wall_ms
            connection = self._connection
        if state is None or (connection is not None and connection != "ONLINE") or self._clock() - state_at > self._max_age:
            return None
        return json.loads(json.dumps(state)), wall

    @property
    def next_node_id(self) -> str | None:
        """The node the vehicle is going to reach next (the lowest sequenceId of its `nodeStates`), None when it has none or the state is not current."""
        seen = self.snapshot()
        nodes = seen[0].get("nodeStates") if seen else None
        if not isinstance(nodes, list):
            return None
        best: tuple[int, str] | None = None
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("nodeId"), str):
                continue
            sequence = node.get("sequenceId")
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                continue
            if best is None or sequence < best[0]:
                best = (sequence, node["nodeId"])
        return best[1] if best else None

    def machine_state(self) -> MachineState:
        with self._lock:
            state, state_at, connection = self._state, self._state_at, self._connection
        if state is None or (connection is not None and connection != "ONLINE"):
            return MachineState.OFFLINE
        if self._clock() - state_at > self._max_age:
            return MachineState.OFFLINE
        if state.get("operatingMode", "AUTOMATIC") != "AUTOMATIC":
            return MachineState.OFFLINE
        safety = state.get("safetyState")
        safety = safety if isinstance(safety, dict) else {}
        if safety.get("eStop", "NONE") != "NONE":
            return MachineState.SAFE_STOP
        errors = state.get("errors")
        fatal = isinstance(errors, list) and any(isinstance(e, dict) and e.get("errorLevel") == "FATAL" for e in errors)
        if safety.get("fieldViolation") is True or fatal:
            return MachineState.FAULT
        if state.get("paused") is True:
            return MachineState.HOLDING
        actions = state.get("actionStates")
        busy_actions = isinstance(actions, list) and any(
            isinstance(a, dict) and a.get("actionStatus") in _ACTIVE_ACTION_STATUSES for a in actions
        )
        nodes = state.get("nodeStates")
        if state.get("driving") is True or busy_actions or (isinstance(nodes, list) and nodes):
            return MachineState.RUNNING
        return MachineState.IDLE
