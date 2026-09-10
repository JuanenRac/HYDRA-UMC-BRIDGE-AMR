# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Realistic VDA 5050 2.0.0 AGV emulator (test fixture)
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A protocol-faithful VDA 5050 2.0.0 AGV on the MQTT bus, over the exact
``MqttPublisher`` seam (`publish(topic, payload)`) this bridge's
``Vda5050Publisher`` already talks to.

Until now the only MQTT double here was ``FakeMqttClient`` - it records
`(topic, payload)` and never looks at either. That proves this bridge
builds *a* message; it never proves the message is one a real AGV would
accept, nor what that AGV does with it.

This emulator, on every `publish()`:

  * splits the real topic
    ``{interfaceName}/{majorVersion}/{manufacturer}/{serialNumber}/{channel}``
    and rejects one that is not 5 segments or whose manufacturer/serial
    don't match this AGV (a real AGV only consumes its own topics);
  * validates the JSON body against the real VDA 5050 2.0.0
    order/instantActions schema shape (github.com/VDA5050/VDA5050
    json_schemas/): the required header (`headerId` int and strictly
    increasing, `timestamp`, `version`, `manufacturer`, `serialNumber`);
    for `order` - `orderId`, `orderUpdateId`, `nodes` (each with
    `nodeId`/`sequenceId`/`released` and, if present, a `nodePosition`
    carrying `x`, `y` AND `mapId`), `edges`; every action carrying
    `actionId`, `actionType`, and a `blockingType` in
    {NONE,SOFT,SINGLE,HARD}; for `instantActions` - a non-empty `actions`
    list of the same action shape;
  * then actually executes it: an accepted order becomes the active
    order, its single node action goes WAITING -> RUNNING on `step()` ->
    FINISHED; a `CANCEL_ORDER` instant action preempts the active order
    (its running action -> FAILED, `driving` -> False), exactly as a real
    AGV handles `cancelOrder` on the instantActions topic.

`emit_state()` returns a real VDA 5050 `state` message. `set_estop()`,
`set_field_violation()` and `set_operating_mode()` drive the physical
side so a test can prove those surface in `state.safetyState` /
`state.operatingMode` without any real hardware or broker.
"""

from __future__ import annotations

import json
import time

_BLOCKING_TYPES = {"NONE", "SOFT", "SINGLE", "HARD"}
_OPERATING_MODES = {"AUTOMATIC", "SEMIAUTOMATIC", "MANUAL", "SERVICE", "TEACHIN"}


class Vda5050ValidationError(Exception):
    """Raised for a message a real VDA 5050 2.0.0 AGV would reject outright."""


class Vda5050AgvEmulator:
    def __init__(self, manufacturer: str = "hydra-umc", serial_number: str = "amr-01") -> None:
        self.manufacturer = manufacturer
        self.serial_number = serial_number
        self._last_header_id: dict[str, int] = {}  # per channel, must strictly increase
        self.received: list[tuple[str, dict]] = []
        self.validation_errors: list[str] = []

        # AGV state (a real subset of the VDA 5050 `state` message).
        self.order_id: str = ""
        self.order_update_id: int = 0
        self.last_node_id: str = ""
        self.action_states: list[dict] = []  # {actionId, actionType, actionStatus}
        self.driving: bool = False
        self.paused: bool = False
        self.operating_mode: str = "AUTOMATIC"
        self.battery_charge: float = 87.0
        self.charging: bool = False
        self.e_stop: str = "NONE"          # NONE | MANUAL | REMOTE | AUTOACK
        self.field_violation: bool = False
        self.errors: list[dict] = []
        self._state_header_id = 0

    # ---- physical-side drivers ------------------------------------------
    def set_estop(self, kind: str = "MANUAL") -> None:
        self.e_stop = kind
        self.driving = False
        if kind != "NONE":
            self._fail_running_actions("E-STOP")

    def clear_estop(self) -> None:
        self.e_stop = "NONE"

    def set_field_violation(self, active: bool) -> None:
        self.field_violation = active
        if active:
            self.driving = False

    def set_operating_mode(self, mode: str) -> None:
        if mode not in _OPERATING_MODES:
            raise ValueError(f"not a real VDA 5050 operatingMode: {mode!r}")
        self.operating_mode = mode

    def step(self) -> None:
        """Advance action execution one tick: WAITING -> RUNNING -> FINISHED."""
        if self.e_stop != "NONE" or self.field_violation or self.operating_mode != "AUTOMATIC":
            return
        for action in self.action_states:
            if action["actionStatus"] == "WAITING":
                action["actionStatus"] = "RUNNING"
                self.driving = True
                return
            if action["actionStatus"] == "RUNNING":
                action["actionStatus"] = "FINISHED"
                self.driving = False
                return

    # ---- MqttPublisher: the bridge publishes an order/instantAction here ----
    def publish(self, topic: str, payload: str) -> "_PublishAck":
        try:
            channel, body = self._parse_and_validate(topic, payload)
        except Vda5050ValidationError as error:
            self.validation_errors.append(str(error))
            raise
        self.received.append((channel, body))
        if channel == "order":
            self._apply_order(body)
        elif channel == "instantActions":
            self._apply_instant_actions(body)
        return _PublishAck(rc=0)

    # ---- validation ----------------------------------------------------
    def _parse_and_validate(self, topic: str, payload: str) -> tuple[str, dict]:
        segments = topic.split("/")
        if len(segments) != 5:
            raise Vda5050ValidationError(f"topic must have 5 segments, got {len(segments)}: {topic!r}")
        _iface, _ver, manufacturer, serial, channel = segments
        if manufacturer != self.manufacturer or serial != self.serial_number:
            raise Vda5050ValidationError(
                f"topic addresses {manufacturer}/{serial}, this AGV is {self.manufacturer}/{self.serial_number}"
            )
        if channel not in ("order", "instantActions"):
            raise Vda5050ValidationError(f"unknown/unsupported channel: {channel!r}")

        try:
            body = json.loads(payload)
        except json.JSONDecodeError as error:
            raise Vda5050ValidationError(f"payload is not valid JSON: {error}") from error
        if not isinstance(body, dict):
            raise Vda5050ValidationError("payload must be a JSON object")

        for field in ("headerId", "timestamp", "version", "manufacturer", "serialNumber"):
            if field not in body:
                raise Vda5050ValidationError(f"missing required header field: {field}")
        if not isinstance(body["headerId"], int) or isinstance(body["headerId"], bool):
            raise Vda5050ValidationError("headerId must be an integer")
        previous = self._last_header_id.get(channel)
        if previous is not None and body["headerId"] <= previous:
            raise Vda5050ValidationError(
                f"headerId {body['headerId']} on {channel} did not strictly increase (last was {previous})"
            )
        self._last_header_id[channel] = body["headerId"]

        if channel == "order":
            self._validate_order(body)
        else:
            self._validate_instant_actions(body)
        return channel, body

    def _validate_action(self, action: object, where: str) -> None:
        if not isinstance(action, dict):
            raise Vda5050ValidationError(f"{where}: action must be an object")
        for field in ("actionId", "actionType", "blockingType"):
            if field not in action:
                raise Vda5050ValidationError(f"{where}: action missing {field}")
        if action["blockingType"] not in _BLOCKING_TYPES:
            raise Vda5050ValidationError(
                f"{where}: blockingType {action['blockingType']!r} not in {sorted(_BLOCKING_TYPES)}"
            )

    def _validate_order(self, body: dict) -> None:
        for field in ("orderId", "orderUpdateId", "nodes", "edges"):
            if field not in body:
                raise Vda5050ValidationError(f"order missing required field: {field}")
        if not isinstance(body["orderUpdateId"], int) or isinstance(body["orderUpdateId"], bool):
            raise Vda5050ValidationError("orderUpdateId must be an integer")
        if not isinstance(body["nodes"], list) or not body["nodes"]:
            raise Vda5050ValidationError("order.nodes must be a non-empty list")
        if not isinstance(body["edges"], list):
            raise Vda5050ValidationError("order.edges must be a list")
        for index, node in enumerate(body["nodes"]):
            if not isinstance(node, dict):
                raise Vda5050ValidationError(f"nodes[{index}] must be an object")
            for field in ("nodeId", "sequenceId", "released", "actions"):
                if field not in node:
                    raise Vda5050ValidationError(f"nodes[{index}] missing {field}")
            position = node.get("nodePosition")
            if position is not None:
                for field in ("x", "y", "mapId"):
                    if field not in position:
                        raise Vda5050ValidationError(
                            f"nodes[{index}].nodePosition missing {field} (VDA 5050 2.0.0 requires x, y AND mapId)"
                        )
            for action in node["actions"]:
                self._validate_action(action, f"nodes[{index}].actions")

    def _validate_instant_actions(self, body: dict) -> None:
        actions = body.get("actions")
        if not isinstance(actions, list) or not actions:
            raise Vda5050ValidationError("instantActions.actions must be a non-empty list")
        for action in actions:
            self._validate_action(action, "instantActions.actions")

    # ---- execution ----------------------------------------------------
    def _apply_order(self, body: dict) -> None:
        self.order_id = body["orderId"]
        self.order_update_id = body["orderUpdateId"]
        node = body["nodes"][0]
        self.last_node_id = node["nodeId"]
        self.action_states = [
            {"actionId": a["actionId"], "actionType": a["actionType"], "actionStatus": "WAITING"}
            for a in node["actions"]
        ]
        self.driving = False

    def _apply_instant_actions(self, body: dict) -> None:
        for action in body["actions"]:
            if action["actionType"] in ("CANCEL_ORDER", "cancelOrder"):
                self._fail_running_actions("order cancelled")
                self.order_id = ""
                self.driving = False
            elif action["actionType"] in ("startPause",):
                self.paused = True
                self.driving = False
            elif action["actionType"] in ("stopPause",):
                self.paused = False

    def _fail_running_actions(self, _reason: str) -> None:
        for action in self.action_states:
            if action["actionStatus"] in ("WAITING", "RUNNING"):
                action["actionStatus"] = "FAILED"

    # ---- state report -------------------------------------------------
    def emit_state(self) -> dict:
        self._state_header_id += 1
        return {
            "headerId": self._state_header_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "version": "2.0.0",
            "manufacturer": self.manufacturer,
            "serialNumber": self.serial_number,
            "orderId": self.order_id,
            "orderUpdateId": self.order_update_id,
            "lastNodeId": self.last_node_id,
            "lastNodeSequenceId": 0,
            "nodeStates": [],
            "edgeStates": [],
            "driving": self.driving,
            "paused": self.paused,
            "operatingMode": self.operating_mode,
            "actionStates": [dict(a) for a in self.action_states],
            "batteryState": {"batteryCharge": self.battery_charge, "charging": self.charging},
            "errors": list(self.errors),
            "safetyState": {"eStop": self.e_stop, "fieldViolation": self.field_violation},
        }


class _PublishAck:
    """Mirrors paho-mqtt's MQTTMessageInfo - only `.rc` is read by the bridge."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
