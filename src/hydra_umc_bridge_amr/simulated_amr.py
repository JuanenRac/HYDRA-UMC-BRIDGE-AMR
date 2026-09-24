# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Explicit AMR order-state table and simulated AMR
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""An explicit, readable table of which order action is legal from which AMR
state, and a simulated AMR that follows it.

The table is the single answer to "what may happen next": a dispatch that
is not a legal step from the current state is refused, never queued. The
simulated AMR is pure bookkeeping - it imports no transport, opens no
socket and cannot move anything real - so a whole job sequence, including
stop and recovery, can be rehearsed before a real vehicle is connected.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .coordinator import AmrCoordinator, AmrDispatch


class AmrState(str, Enum):
    IDLE = "IDLE"
    MOVING = "MOVING"
    LOADED_WAITING = "LOADED_WAITING"
    UNLOADING = "UNLOADING"


A = AmrCoordinator

# action -> (states it may start from, state it leads to). CANCEL_ORDER is
# the stop/recovery path: legal from every state, always ends in IDLE.
TRANSITIONS: dict[str, tuple[frozenset[AmrState], AmrState]] = {
    A.MOVE_TO_STAGING: (frozenset({AmrState.IDLE}), AmrState.MOVING),
    A.PICK_LOAD: (frozenset({AmrState.MOVING}), AmrState.LOADED_WAITING),
    A.MOVE_TO_DESTINATION: (frozenset({AmrState.LOADED_WAITING}), AmrState.MOVING),
    A.DROP_LOAD: (frozenset({AmrState.MOVING}), AmrState.UNLOADING),
    A.MOVE_TO_HOME: (frozenset({AmrState.UNLOADING, AmrState.IDLE}), AmrState.MOVING),
    A.CANCEL_ORDER: (frozenset(AmrState), AmrState.IDLE),
}


@dataclass(frozen=True)
class StepResult:
    applied: bool
    state: AmrState
    reason: str


class SimulatedAmr:
    """Follows `TRANSITIONS`; performs no real motion and reaches no transport."""

    def __init__(self) -> None:
        self._state = AmrState.IDLE
        self._history: list[tuple[str, AmrState]] = []

    @property
    def state(self) -> AmrState:
        return self._state

    @property
    def history(self) -> tuple[tuple[str, AmrState], ...]:
        return tuple(self._history)

    def apply(self, dispatch: AmrDispatch) -> StepResult:
        if not dispatch.accepted:
            return StepResult(False, self._state, f"dispatch not accepted: {dispatch.reason}")
        rule = TRANSITIONS.get(dispatch.action)
        if rule is None:
            return StepResult(False, self._state, f"unknown action {dispatch.action!r}")
        allowed_from, target = rule
        if self._state not in allowed_from:
            return StepResult(False, self._state, f"{dispatch.action} is not legal from {self._state.value}")
        self._state = target
        self._history.append((dispatch.action, target))
        return StepResult(True, target, "ok")
