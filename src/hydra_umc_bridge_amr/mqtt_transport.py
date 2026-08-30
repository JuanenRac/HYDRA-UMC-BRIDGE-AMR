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
to it and an empty edge list (a real, valid VDA 5050 shape for "go here and
do this", not a multi-waypoint route this bridge was never meant to plan);
a real "instantActions" message carries the action directly. Only a
dispatch the shared SDK gate already accepted is ever published - a
rejected AmrDispatch never reaches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from .coordinator import AmrDispatch


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


def open_mqtt_client(host: str, port: int = 1883, *, keepalive_seconds: int = 60) -> MqttPublisher:
    """Open a real MQTT connection. The only place this module imports paho-mqtt.

    Raises RuntimeError with a clear message if paho-mqtt isn't installed,
    rather than letting an ImportError surface from deep inside this module.
    """

    try:
        import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
    except ImportError as error:
        raise RuntimeError(
            "paho-mqtt is not installed - install it to publish real VDA 5050 orders "
            "(this module's payload-building/gating logic works and is tested without it)"
        ) from error
    client = mqtt.Client()
    client.connect(host, port, keepalive=keepalive_seconds)
    client.loop_start()
    return client


@dataclass(frozen=True)
class PublishResult:
    published: bool
    reason: str
    topic: str | None = None


class Vda5050Publisher:
    """Build and publish a real VDA 5050 message for one already-gated AmrDispatch."""

    def publish(
        self,
        client: MqttPublisher,
        target: Vda5050Target,
        header_id: int,
        timestamp_iso: str,
        dispatch: AmrDispatch,
    ) -> PublishResult:
        # A rejected dispatch (the shared SDK gate already said no) must
        # never reach the network - the transport layer is not a second
        # place to reconsider a safety decision already made.
        if not dispatch.accepted:
            return PublishResult(False, dispatch.reason)

        header = {
            "headerId": header_id,
            "timestamp": timestamp_iso,
            "version": target.protocol_version,
            "manufacturer": target.manufacturer,
            "serialNumber": target.serial_number,
        }
        topic = target.topic(dispatch.channel)

        if dispatch.channel == "instantActions":
            payload = {
                **header,
                "actions": [
                    {
                        "actionId": f"{dispatch.action.lower()}-{header_id}",
                        "actionType": dispatch.action,
                        # HARD blocks everything else, per VDA 5050's own
                        # real blockingType enum (NONE/SOFT/SINGLE/HARD) -
                        # a cancel-order-class action must preempt, not
                        # queue behind whatever the AGV is already doing.
                        "blockingType": "HARD",
                    }
                ],
            }
        else:
            node: dict[str, object] = {
                "nodeId": f"{dispatch.action.lower()}-{header_id}",
                "sequenceId": 0,
                "released": True,
                "actions": [
                    {
                        "actionId": f"{dispatch.action.lower()}-{header_id}",
                        "actionType": dispatch.action,
                        "blockingType": "HARD",
                    }
                ],
            }
            # nodePosition is only meaningful for a movement action -
            # PICK_LOAD/DROP_LOAD resolve no coordinate (see coordinator.py's
            # own _MOVEMENT_ACTIONS), and VDA 5050 allows an omitted
            # nodePosition for a node the AGV already has a fixed location
            # for.
            if dispatch.local_x is not None and dispatch.local_y is not None:
                node["nodePosition"] = {"x": dispatch.local_x, "y": dispatch.local_y}
            payload = {
                **header,
                "orderId": f"hydra-umc-{header_id}",
                "orderUpdateId": 0,
                "nodes": [node],
                # A single-node order has nothing to traverse - this bridge
                # was never meant to plan a multi-waypoint route (see this
                # module's own docstring), so an empty edge list is the
                # real, correct shape here, not a placeholder.
                "edges": [],
            }

        try:
            client.publish(topic, json.dumps(payload))
        except OSError as error:
            return PublishResult(False, f"MQTT publish failed: {error}", topic)
        return PublishResult(True, "published", topic)
