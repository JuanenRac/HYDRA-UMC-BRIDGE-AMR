# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - The vehicle's state for the data lake and for an OPC UA client
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Publish what the AGV reported (battery, errors, position) as the bridge's own `state`, and the same readings as a table of OPC UA nodes.

* `hydra/bridges/amr/state` is the topic the telemetry collector already reads for every bridge (`hydra/bridges/<name>/state`): one JSON object with `observed_at` in
  milliseconds, the `machine_state` the bridge observed and numbers and flags (`battery.charge`, `battery.charging`, `position.x`, `errors.count`, `errors.fatal`...).
  The collector turns it into samples for the data lake, and the anomaly detector reads them from there.
* `hydra/bridges/amr/opcua` is the same reading as a list of nodes (`path`, `type`, `value`) that a gateway can expose to a SCADA as an OPC UA tree, so the fleet is
  seen next to the robots. Nothing is writable.

Both are off by default: `HYDRA_UMC_AMR_PUBLISH_STATE=1` and `HYDRA_UMC_AMR_PUBLISH_OPCUA=1`. `HYDRA_UMC_AMR_STATE_INTERVAL_S` (default 5, at least 1) is the shortest time
between two publications. A state that is unknown, offline or older than the observer allows publishes nothing: the data lake is never given an old value as a new one.
Nothing here has run against a real AGV or fleet manager.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Callable

from .mqtt_transport import MqttPublisher
from .observer import Vda5050Observer

STATE_TOPIC = "hydra/bridges/amr/state"
OPCUA_TOPIC = "hydra/bridges/amr/opcua"
PUBLISH_STATE_ENVIRONMENT = "HYDRA_UMC_AMR_PUBLISH_STATE"
PUBLISH_OPCUA_ENVIRONMENT = "HYDRA_UMC_AMR_PUBLISH_OPCUA"
INTERVAL_ENVIRONMENT = "HYDRA_UMC_AMR_STATE_INTERVAL_S"
DEFAULT_INTERVAL_SECONDS = 5.0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def state_record(state: dict, observed_at_ms: int, machine_state: str) -> dict[str, object]:
    """The readings of one VDA 5050 `state` message as a flat record. A field the AGV did not report is left out, never invented."""
    record: dict[str, object] = {"observed_at": observed_at_ms, "machine_state": machine_state}
    battery = state.get("batteryState")
    if isinstance(battery, dict):
        charge = _number(battery.get("batteryCharge"))
        if charge is not None:
            record["battery.charge"] = charge
        if isinstance(battery.get("charging"), bool):
            record["battery.charging"] = battery["charging"]
        voltage = _number(battery.get("batteryVoltage"))
        if voltage is not None:
            record["battery.voltage"] = voltage
    position = state.get("agvPosition")
    if isinstance(position, dict):
        for key in ("x", "y", "theta"):
            value = _number(position.get(key))
            if value is not None:
                record[f"position.{key}"] = value
        if isinstance(position.get("positionInitialized"), bool):
            record["position.initialized"] = position["positionInitialized"]
    errors = state.get("errors")
    if isinstance(errors, list):
        levels = [e.get("errorLevel") for e in errors if isinstance(e, dict)]
        record["errors.count"] = len(levels)
        record["errors.fatal"] = sum(1 for level in levels if level == "FATAL")
        record["errors.warning"] = sum(1 for level in levels if level == "WARNING")
    for name in ("driving", "paused"):
        if isinstance(state.get(name), bool):
            record[name] = state[name]
    safety = state.get("safetyState")
    if isinstance(safety, dict):
        if isinstance(safety.get("eStop"), str):
            record["e_stop"] = safety["eStop"] != "NONE"
        if isinstance(safety.get("fieldViolation"), bool):
            record["field_violation"] = safety["fieldViolation"]
    return record


def opcua_nodes(record: dict[str, object], vehicle: str) -> list[dict[str, object]]:
    """The record as OPC UA nodes under `Fleet/<vehicle>/`: a `path`, a `type` (Double, Boolean, String, Int64) and the `value`. Read-only by construction."""
    nodes: list[dict[str, object]] = []
    for key in sorted(record):
        if key == "observed_at":
            continue
        value = record[key]
        if isinstance(value, bool):
            kind = "Boolean"
        elif isinstance(value, int):
            kind = "Int64"
        elif isinstance(value, float):
            kind = "Double"
        elif isinstance(value, str):
            kind = "String"
        else:
            continue
        nodes.append({"path": f"Fleet/{vehicle}/" + key.replace(".", "/"), "type": kind, "value": value})
    return nodes


def _switch(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


class StateReporter:
    """Publishes the observed state of one AGV at most once per interval, to the topics that are switched on."""

    def __init__(
        self,
        observer: Vda5050Observer,
        vehicle: str,
        *,
        publish_state: bool | None = None,
        publish_opcua: bool | None = None,
        interval_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._observer = observer
        self._vehicle = vehicle
        self.publish_state = _switch(PUBLISH_STATE_ENVIRONMENT) if publish_state is None else bool(publish_state)
        self.publish_opcua = _switch(PUBLISH_OPCUA_ENVIRONMENT) if publish_opcua is None else bool(publish_opcua)
        if interval_seconds is None:
            raw = os.environ.get(INTERVAL_ENVIRONMENT, "").strip()
            interval_seconds = float(raw) if raw else DEFAULT_INTERVAL_SECONDS
        if not isinstance(interval_seconds, (int, float)) or isinstance(interval_seconds, bool) or not interval_seconds >= 1:
            raise ValueError("the publish interval must be at least 1 second")
        self._interval = float(interval_seconds)
        self._clock = clock
        self._last: float | None = None

    @property
    def enabled(self) -> bool:
        return self.publish_state or self.publish_opcua

    def maybe_publish(self, client: MqttPublisher) -> bool:
        """Publish if a switch is on, the interval has passed and the state is current. Never raises on a publish failure."""
        if not self.enabled:
            return False
        now = self._clock()
        if self._last is not None and now - self._last < self._interval:
            return False
        seen = self._observer.snapshot()
        if seen is None:
            return False
        state, wall_ms = seen
        record = state_record(state, wall_ms, self._observer.machine_state().value)
        try:
            if self.publish_state:
                client.publish(STATE_TOPIC, json.dumps(record, allow_nan=False))
            if self.publish_opcua:
                client.publish(OPCUA_TOPIC, json.dumps({"observed_at": wall_ms, "vehicle": self._vehicle, "nodes": opcua_nodes(record, self._vehicle)}, allow_nan=False))
        except (OSError, ValueError):
            return False
        self._last = now
        return True
