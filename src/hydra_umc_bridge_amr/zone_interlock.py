# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Zone interlock: an occupied zone pauses the vehicles that are about to enter it
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Pause an AGV whose next node lies in a zone that is occupied, and let it go on when the zone is free.

The zone of a node comes from the plant graph (`zone` of the node, see plant_graph.py). What is occupied comes from a message on `HYDRA_UMC_AMR_ZONE_TOPIC`
(default `hydra/safety/zones/occupied`): `{"occupied": ["press-1"], "observed_at": <milliseconds>}`, with an optional `"known": false` that says the emitter could not see
the zones (then the empty list is not "free" and the reading is UNKNOWN, whatever `occupied` holds; without the field it reads as before). The next node is the lowest `sequenceId` of the vehicle's own
`nodeStates`. The rules:

* a vehicle that is already inside the zone of its next node is not paused (it would stop in the doorway);
* VDA 5050 `startPause` is a safety action and is always forwarded; the way back, `stopPause`, goes through the coordinator's gate (cell READY and the vehicle read as
  HOLDING) and is sent only to a vehicle THIS interlock paused: one paused by anybody else stays paused;
* a `startPause` that the vehicle has not confirmed (its `state` does not say `paused`) is sent again every `HYDRA_UMC_AMR_ZONE_PAUSE_RETRY_S` seconds (default 2)
  for as long as the zone stays blocked, because a publish is only queued, never confirmed;
* a message stamped more than `HYDRA_UMC_AMR_ZONE_MAX_FUTURE_S` seconds (default 2) ahead of this clock is not believed (a clock that runs ahead, or a forged stamp, would
  otherwise freeze the reading as "fresh" and make every later real message look like a replay);
* an occupancy message that was never heard, is unreadable or is older than the maximum age counts as UNKNOWN. `HYDRA_UMC_AMR_ZONE_UNKNOWN` says what that means:
  `pause` (default, the conservative one: a zone that cannot be read is treated as occupied) or `ignore`.

Off by default: `HYDRA_UMC_AMR_ZONE_INTERLOCK=1` switches it on; without it the bridge subscribes to nothing and pauses nothing. Nothing here has run against a real AGV
or a real occupancy source.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable

from hydra_umc_sdk.bridge_contract import CellState

from .coordinator import AmrCoordinator, AmrDispatch
from .mqtt_transport import MqttPublisher, Vda5050Publisher, Vda5050Target
from .observer import Vda5050Observer
from .plant_graph import PlantGraph

ENABLE_ENVIRONMENT = "HYDRA_UMC_AMR_ZONE_INTERLOCK"
TOPIC_ENVIRONMENT = "HYDRA_UMC_AMR_ZONE_TOPIC"
MAX_AGE_ENVIRONMENT = "HYDRA_UMC_AMR_ZONE_MAX_AGE_S"
UNKNOWN_ENVIRONMENT = "HYDRA_UMC_AMR_ZONE_UNKNOWN"
RETRY_ENVIRONMENT = "HYDRA_UMC_AMR_ZONE_PAUSE_RETRY_S"
FUTURE_ENVIRONMENT = "HYDRA_UMC_AMR_ZONE_MAX_FUTURE_S"
DEFAULT_TOPIC = "hydra/safety/zones/occupied"
DEFAULT_MAX_AGE_SECONDS = 5.0
DEFAULT_PAUSE_RETRY_SECONDS = 2.0
DEFAULT_MAX_FUTURE_SECONDS = 2.0
MAX_MESSAGE_BYTES = 64 * 1024


def _positive_seconds(value: float | None, environment: str, default: float, what: str) -> float:
    if value is None:
        raw = os.environ.get(environment, "").strip()
        value = float(raw) if raw else default
    if not value > 0:
        raise ValueError(f"{what} must be positive")
    return float(value)


class ZoneInterlock:
    def __init__(
        self,
        graph: PlantGraph,
        observer: Vda5050Observer,
        coordinator: AmrCoordinator,
        publisher: Vda5050Publisher,
        target: Vda5050Target,
        cell_state: Callable[[], CellState],
        *,
        enabled: bool | None = None,
        topic: str | None = None,
        max_age_seconds: float | None = None,
        unknown: str | None = None,
        pause_retry_seconds: float | None = None,
        max_future_seconds: float | None = None,
        wall_clock_ms: Callable[[], float] = lambda: time.time() * 1000.0,
    ) -> None:
        self._graph, self._observer, self._coordinator = graph, observer, coordinator
        self._publisher, self._target, self._cell_state = publisher, target, cell_state
        self.enabled = os.environ.get(ENABLE_ENVIRONMENT, "").strip() == "1" if enabled is None else bool(enabled)
        self.topic = topic or os.environ.get(TOPIC_ENVIRONMENT, "").strip() or DEFAULT_TOPIC
        if max_age_seconds is None:
            raw = os.environ.get(MAX_AGE_ENVIRONMENT, "").strip()
            max_age_seconds = float(raw) if raw else DEFAULT_MAX_AGE_SECONDS
        if not max_age_seconds > 0:
            raise ValueError("the maximum age of the occupancy must be positive")
        self._max_age_ms = float(max_age_seconds) * 1000.0
        self._retry_ms = _positive_seconds(pause_retry_seconds, RETRY_ENVIRONMENT, DEFAULT_PAUSE_RETRY_SECONDS, "the pause retry interval") * 1000.0
        self._max_future_ms = _positive_seconds(max_future_seconds, FUTURE_ENVIRONMENT, DEFAULT_MAX_FUTURE_SECONDS, "the tolerated lead of a stamp") * 1000.0
        policy = (unknown or os.environ.get(UNKNOWN_ENVIRONMENT, "").strip() or "pause").lower()
        if policy not in ("pause", "ignore"):
            raise ValueError("the policy for an unknown occupancy must be pause or ignore")
        self._unknown = policy
        self._wall = wall_clock_ms
        self._occupied: frozenset[str] | None = None
        self._observed_ms = 0.0
        self._seen = True  # False when the last message said `known: false`
        self._paused_by_us = False
        self._pause_sent_ms = 0.0

    def set_unknown_policy(self, policy: str) -> None:
        """What an occupancy that cannot be read means from now on: `pause` (the conservative one) or `ignore`."""
        if policy not in ("pause", "ignore"):
            raise ValueError("the policy for an unknown occupancy must be pause or ignore")
        self._unknown = policy

    def subscriptions(self) -> list[tuple[str, int]]:
        return [(self.topic, 1)] if self.enabled else []

    def handles(self, topic: str) -> bool:
        return self.enabled and topic == self.topic

    def on_message(self, topic: str, payload: bytes) -> None:
        """Take one occupancy message. Anything unreadable leaves the last reading to age out (and so become UNKNOWN)."""
        if not self.handles(topic) or len(payload) > MAX_MESSAGE_BYTES:
            return
        try:
            body = json.loads(payload)
        except Exception:  # noqa: BLE001 - not JSON, too deep, bad encoding
            return
        zones = body.get("occupied") if isinstance(body, dict) else None
        stamp = body.get("observed_at") if isinstance(body, dict) else None
        if not isinstance(zones, list) or not all(isinstance(z, str) for z in zones):
            return
        known = body.get("known", True)
        if not isinstance(known, bool):
            return  # a field of the contract with the wrong type: the message is not believed
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp < self._observed_ms:
            return  # no stamp, or a replay of an older message
        if stamp > self._wall() + self._max_future_ms:
            return  # stamped in the future: not believed, and it must not become the newest stamp
        self._occupied = frozenset(zones)
        self._seen = known
        self._observed_ms = float(stamp)

    def occupied(self) -> frozenset[str] | None:
        """The occupied zones, or None (UNKNOWN) when nothing current has been heard."""
        now = self._wall()
        if self._occupied is None or not self._seen or now - self._observed_ms > self._max_age_ms or self._observed_ms - now > self._max_future_ms:
            return None
        return self._occupied

    def _blocked(self, zone: str | None) -> bool:
        if zone is None:
            return False
        occupied = self.occupied()
        if occupied is None:
            return self._unknown == "pause"
        return zone in occupied

    def step(self, client: MqttPublisher) -> str | None:
        """Look at the vehicle once; return "pause", "resume" or None for what was sent. Never raises on a publish failure."""
        if not self.enabled:
            return None
        seen = self._observer.snapshot()
        if seen is None:
            return None
        state = seen[0]
        next_zone = self._graph.zone_of(self._observer.next_node_id)
        inside = self._graph.zone_of(self._observer.last_node_id)
        blocked = self._blocked(next_zone) and next_zone != inside
        paused = state.get("paused") is True
        if blocked and not paused:
            now = self._wall()
            if self._paused_by_us and now - self._pause_sent_ms < self._retry_ms:
                return None  # asked a moment ago; the vehicle has not said yet
            dispatch = AmrDispatch(True, AmrCoordinator.START_PAUSE, f"the zone {next_zone} ahead is occupied or unreadable", channel="instantActions")
            if self._publisher.publish(client, self._target, None, None, dispatch).published:
                self._paused_by_us = True
                self._pause_sent_ms = now
                return "pause"
            return None
        if not blocked and self._paused_by_us:
            if not paused and state.get("driving") is True:
                self._paused_by_us = False  # somebody else already let it go
                return None
            dispatch = self._coordinator.resume(self._cell_state(), self._observer.machine_state())
            if dispatch.accepted and self._publisher.publish(client, self._target, None, None, dispatch).published:
                self._paused_by_us = False
                return "resume"
        return None
