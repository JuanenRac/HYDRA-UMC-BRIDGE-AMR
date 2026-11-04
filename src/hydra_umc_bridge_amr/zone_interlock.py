# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Zone interlock: an occupied zone pauses the vehicles that are about to enter it
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Pause an AGV whose next node lies in a zone that is occupied, and let it go on when the zone is free.

The zone of a node comes from the plant graph (`zone` of the node, see plant_graph.py). What is occupied comes from a message on `HYDRA_UMC_AMR_ZONE_TOPIC`
(default `hydra/safety/zones/occupied`): `{"occupied": ["press-1"], "observed_at": <milliseconds>}`. The next node is the lowest `sequenceId` of the vehicle's own
`nodeStates`. The rules:

* a vehicle that is already inside the zone of its next node is not paused (it would stop in the doorway);
* VDA 5050 `startPause` is a safety action and is always forwarded; the way back, `stopPause`, goes through the coordinator's gate (cell READY and the vehicle read as
  HOLDING) and is sent only to a vehicle THIS interlock paused: one paused by anybody else stays paused;
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
DEFAULT_TOPIC = "hydra/safety/zones/occupied"
DEFAULT_MAX_AGE_SECONDS = 5.0
MAX_MESSAGE_BYTES = 64 * 1024


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
        policy = (unknown or os.environ.get(UNKNOWN_ENVIRONMENT, "").strip() or "pause").lower()
        if policy not in ("pause", "ignore"):
            raise ValueError("the policy for an unknown occupancy must be pause or ignore")
        self._unknown = policy
        self._wall = wall_clock_ms
        self._occupied: frozenset[str] | None = None
        self._observed_ms = 0.0
        self._paused_by_us = False

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
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp < self._observed_ms:
            return  # no stamp, or a replay of an older message
        self._occupied = frozenset(zones)
        self._observed_ms = float(stamp)

    def occupied(self) -> frozenset[str] | None:
        """The occupied zones, or None (UNKNOWN) when nothing current has been heard."""
        if self._occupied is None or self._wall() - self._observed_ms > self._max_age_ms:
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
        if blocked and not self._paused_by_us and not paused:
            dispatch = AmrDispatch(True, AmrCoordinator.START_PAUSE, f"the zone {next_zone} ahead is occupied or unreadable", channel="instantActions")
            if self._publisher.publish(client, self._target, None, None, dispatch).published:
                self._paused_by_us = True
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
