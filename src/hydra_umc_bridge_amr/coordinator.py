# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - AMR/AGV coordinate-frame and order coordinator
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Map a correlated cell job onto a real AMR-local order, never raw motion.

This module deliberately never plans a path or avoids an obstacle - it does
exactly two real things: (1) a real, hand-checkable 2D rigid-body transform
from the factory's own shared coordinate frame into a specific AMR's local
frame (every fleet has its own origin/heading on the factory floor), and
(2) maps a job phase onto a minimal, VDA 5050-inspired order action name.
Real-time navigation, localization and obstacle avoidance stay the AMR's
own onboard authority, or its fleet manager's (MiR/Omron-class REST/
WebSocket, or an open-source stack's own 2D navigation topics) - reached
only through a future, separately deployed transport adapter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, evaluate_job


@dataclass(frozen=True)
class FrameTransform:
    """A real, hand-checkable 2D rigid-body transform between two frames.

    `origin_x`/`origin_y`/`heading_rad` describe where this AMR's own local
    origin sits, and how it is rotated, expressed in the shared factory
    frame - the exact real quantity a site survey or fleet-manager
    configuration would hand this bridge, not something guessed at.
    """

    origin_x: float
    origin_y: float
    heading_rad: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.origin_x, self.origin_y, self.heading_rad)):
            raise ValueError("FrameTransform origin and heading must be finite")

    @staticmethod
    def _require_finite_point(x: float, y: float) -> None:
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("coordinate x/y must be finite")

    def to_local(self, x: float, y: float) -> tuple[float, float]:
        """Factory-frame (x, y) -> this AMR's own local-frame (x, y)."""

        self._require_finite_point(x, y)
        dx, dy = x - self.origin_x, y - self.origin_y
        cos_h, sin_h = math.cos(self.heading_rad), math.sin(self.heading_rad)
        return dx * cos_h + dy * sin_h, -dx * sin_h + dy * cos_h

    def to_factory(self, x: float, y: float) -> tuple[float, float]:
        """This AMR's own local-frame (x, y) -> factory-frame (x, y) - the real inverse of `to_local`."""

        self._require_finite_point(x, y)
        cos_h, sin_h = math.cos(self.heading_rad), math.sin(self.heading_rad)
        dx = x * cos_h - y * sin_h
        dy = x * sin_h + y * cos_h
        return dx + self.origin_x, dy + self.origin_y


@dataclass(frozen=True)
class AmrDispatch:
    accepted: bool
    action: str
    reason: str
    # Populated only for a movement action (see _MOVEMENT_ACTIONS below) -
    # the real AMR-local target this job's factory-frame x/y resolved to.
    # None for a non-movement order action (PICK_LOAD/DROP_LOAD/
    # CANCEL_ORDER), which never needed a coordinate at all.
    local_x: float | None = None
    local_y: float | None = None
    mode: str = "plan-only"
    # Real VDA 5050 channel this action belongs on - see AmrCoordinator's
    # own _INSTANT_ACTIONS comment for why this matters for interop, not
    # just labeling.
    channel: str = "order"


@dataclass(frozen=True)
class AmrOrderPlan:
    """Static evidence of the real, VDA 5050-inspired order vocabulary."""

    schema_version: str
    mode: str
    actions: tuple[str, ...]
    # Real VDA 5050 channel split - see AmrCoordinator's own
    # _INSTANT_ACTIONS comment. A future transport adapter reads this to
    # know which actions publish to the real "order" topic (queued, part
    # of a route) vs "instantActions" (immediate, bypasses the queue) -
    # VDA 5050 defines these as two genuinely separate MQTT topics, not
    # two labels on the same channel.
    instant_actions: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "actions": list(self.actions),
            "instant_actions": list(self.instant_actions),
        }


class AmrCoordinator:
    """Gate jobs and resolve factory-frame targets before a future fleet-manager adapter reaches a real AMR."""

    MOVE_TO_STAGING = "MOVE_TO_STAGING"
    PICK_LOAD = "PICK_LOAD"
    MOVE_TO_DESTINATION = "MOVE_TO_DESTINATION"
    DROP_LOAD = "DROP_LOAD"
    MOVE_TO_HOME = "MOVE_TO_HOME"
    CANCEL_ORDER = "CANCEL_ORDER"  # matches VDA 5050's own real "cancelOrder" action name

    _MOVEMENT_ACTIONS = {MOVE_TO_STAGING, MOVE_TO_DESTINATION, MOVE_TO_HOME}

    # Real VDA 5050 (github.com/VDA5050/VDA5050 json_schemas/) publishes
    # 8 real MQTT topics, 2 of which carry actions: "order" (a node/edge
    # route with actions embedded in it - MOVE_TO_*/PICK_LOAD/DROP_LOAD
    # here) and "instantActions" - a SEPARATE topic for commands that
    # bypass the order queue and execute immediately regardless of the
    # AGV's current order state (the spec's own real, documented examples:
    # cancelOrder, startPause, stopPause, stateRequest, factsheetRequest).
    # CANCEL_ORDER was previously modeled identically to the queued
    # actions above - real, but not real VDA 5050 wire compatibility: a
    # fleet manager expecting cancelOrder on the instantActions topic
    # would never see it if this bridge published it as an order action
    # instead. This set is what future transport adapter work reads to
    # route each action to its real, correct topic.
    _INSTANT_ACTIONS = {CANCEL_ORDER}

    _phase_actions = {
        JobPhase.PREPARE: MOVE_TO_STAGING,
        JobPhase.LOAD: PICK_LOAD,
        JobPhase.PROCESS: MOVE_TO_DESTINATION,
        JobPhase.UNLOAD: DROP_LOAD,
        JobPhase.COMPLETE: MOVE_TO_HOME,
        # Always forwarded regardless of cell/machine state (see
        # evaluate_job's own "abort requests are always forwarded"
        # comment) - VDA 5050's own cancelOrder is the real analog of an
        # abort for a fleet order.
        JobPhase.ABORT: CANCEL_ORDER,
    }

    def order_plan(self) -> AmrOrderPlan:
        """Return the static order-action vocabulary without opening any real transport."""

        all_actions = tuple(self._phase_actions.values())
        order_actions = tuple(a for a in all_actions if a not in self._INSTANT_ACTIONS)
        instant_actions = tuple(a for a in all_actions if a in self._INSTANT_ACTIONS)
        return AmrOrderPlan("1.1", "plan-only", order_actions, instant_actions)

    def dispatch(self, job: BridgeJob, cell_state: CellState, transform: FrameTransform) -> AmrDispatch:
        action = self._phase_actions.get(job.phase)
        if action is None:
            return AmrDispatch(False, "none", "job phase has no mapped AMR order action")

        local_x = local_y = None
        if action in self._MOVEMENT_ACTIONS:
            x_raw, y_raw = job.parameters.get("x"), job.parameters.get("y")
            if x_raw is None or y_raw is None:
                return AmrDispatch(False, action, f"missing required parameter(s) for {action}: x, y")
            try:
                local_x, local_y = transform.to_local(float(x_raw), float(y_raw))
            except ValueError:
                return AmrDispatch(False, action, f"x/y must be real numbers, got x={x_raw!r} y={y_raw!r}")

        decision = evaluate_job(job, cell_state)
        channel = "instantActions" if action in self._INSTANT_ACTIONS else "order"
        return AmrDispatch(decision.allowed, action, decision.reason, local_x, local_y, channel=channel)
