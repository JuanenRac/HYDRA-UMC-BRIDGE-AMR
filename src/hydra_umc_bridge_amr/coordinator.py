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

    def to_local(self, x: float, y: float) -> tuple[float, float]:
        """Factory-frame (x, y) -> this AMR's own local-frame (x, y)."""

        dx, dy = x - self.origin_x, y - self.origin_y
        cos_h, sin_h = math.cos(self.heading_rad), math.sin(self.heading_rad)
        return dx * cos_h + dy * sin_h, -dx * sin_h + dy * cos_h

    def to_factory(self, x: float, y: float) -> tuple[float, float]:
        """This AMR's own local-frame (x, y) -> factory-frame (x, y) - the real inverse of `to_local`."""

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


@dataclass(frozen=True)
class AmrOrderPlan:
    """Static evidence of the real, VDA 5050-inspired order vocabulary."""

    schema_version: str
    mode: str
    actions: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "mode": self.mode, "actions": list(self.actions)}


class AmrCoordinator:
    """Gate jobs and resolve factory-frame targets before a future fleet-manager adapter reaches a real AMR."""

    MOVE_TO_STAGING = "MOVE_TO_STAGING"
    PICK_LOAD = "PICK_LOAD"
    MOVE_TO_DESTINATION = "MOVE_TO_DESTINATION"
    DROP_LOAD = "DROP_LOAD"
    MOVE_TO_HOME = "MOVE_TO_HOME"
    CANCEL_ORDER = "CANCEL_ORDER"  # matches VDA 5050's own real "cancelOrder" action name

    _MOVEMENT_ACTIONS = {MOVE_TO_STAGING, MOVE_TO_DESTINATION, MOVE_TO_HOME}

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

        return AmrOrderPlan("1.0", "plan-only", tuple(self._phase_actions.values()))

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
        return AmrDispatch(decision.allowed, action, decision.reason, local_x, local_y)
