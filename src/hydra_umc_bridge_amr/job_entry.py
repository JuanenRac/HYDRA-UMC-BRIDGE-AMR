# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Entry point for jobs (cmd/job)
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The entry point that receives jobs over MQTT, like the CNC and laser bridges, through the shared intake of the SDK.

`hydra/bridges/amr/cmd/job` carries one `BridgeJob` (the `job_to_dict` shape). The shared intake ignores a retained replay, refuses a message that is too large,
unreadable, malformed or (when signed commands are on) unsigned, and answers a repeated idempotency key with the stored answer instead of running the job again.
A new job goes to `AmrCoordinator.dispatch_all` with the cell state this process reports and the state the AGV itself reported (never the claim written in the job);
only the dispatches the gate accepted are published, as VDA 5050 messages, by `Vda5050Publisher`. The answer goes to `hydra/bridges/amr/cmd/job/result`.

Off by default: `HYDRA_UMC_AMR_JOB_ENTRY=1` (or `enabled=True`) switches it on. When it is off the bridge subscribes to nothing and answers nothing (the behaviour before
this entry existed). Nothing here has run against a real AGV or fleet manager.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

from hydra_umc_sdk.bridge_contract import CellState, MachineState
from hydra_umc_sdk.job_intake import IdempotencyStore, JobIntake, read_job_message

from .coordinator import AmrCoordinator, FrameTransform
from .plant_graph import PlantGraph
from .mqtt_transport import MqttPublisher, Vda5050Publisher, Vda5050Target

_LOG = logging.getLogger(__name__)

TOPIC_PREFIX = "hydra/bridges/amr/"
JOB_TOPIC = f"{TOPIC_PREFIX}cmd/job"
RESULT_TOPIC = f"{JOB_TOPIC}/result"
ENABLE_ENVIRONMENT = "HYDRA_UMC_AMR_JOB_ENTRY"
# The factsheet gate is ON unless this is "0": with a vehicle that has told the bridge which actions it supports, a job that needs another one is refused.
FACTSHEET_GATE_ENVIRONMENT = "HYDRA_UMC_AMR_FACTSHEET_GATE"
# Never refused by the gate: what stops the vehicle must always get through, whatever the factsheet says.
_SAFETY_WIRE_TYPES = frozenset({"cancelOrder", "startPause", "stopPause"})
RESULT_QOS = 1


def enabled_from_environment() -> bool:
    return os.environ.get(ENABLE_ENVIRONMENT, "").strip() == "1"


def factsheet_gate_from_environment() -> bool:
    return os.environ.get(FACTSHEET_GATE_ENVIRONMENT, "").strip() != "0"


class AmrJobEntry:
    """Receives the jobs of `cmd/job`, decides them with the coordinator and publishes what the gate accepts."""

    def __init__(
        self,
        coordinator: AmrCoordinator,
        publisher: Vda5050Publisher,
        target: Vda5050Target,
        transform: FrameTransform,
        cell_state: Callable[[], CellState],
        machine_state: Callable[[], MachineState | None],
        idempotency: IdempotencyStore | None = None,
        *,
        enabled: bool | None = None,
        intake: JobIntake | None = None,
        graph: PlantGraph | None = None,
        last_node: Callable[[], str | None] | None = None,
        supports_action: Callable[[str], bool | None] | None = None,
        factsheet_gate: bool | None = None,
    ) -> None:
        """`cell_state` and `machine_state` are read for every job (the state of this cell, and the state the AGV reported: `Vda5050Observer.machine_state`).
        `enabled` None follows `HYDRA_UMC_AMR_JOB_ENTRY=1` (default off).
        With a `graph` (see plant_graph.py) and `last_node` (`lambda: observer.last_node_id`), a movement job that names `goal_node` is planned on the graph and published as one multi-node order; without them a job with `goal_node` is refused.
        With `supports_action` (`observer.supports_action`) a job whose actions the factsheet of the vehicle says it does not support (`False`) is refused before anything is published;
        "unknown" (no factsheet heard) never refuses, and the actions that stop the vehicle are never refused. `factsheet_gate` None follows `HYDRA_UMC_AMR_FACTSHEET_GATE` (on unless it is `0`)."""
        self._coordinator = coordinator
        self._publisher = publisher
        self._target = target
        self._transform = transform
        self._cell_state = cell_state
        self._machine_state = machine_state
        self._store = idempotency
        self._intake = intake if intake is not None else JobIntake(idempotency)
        self._graph = graph
        self._last_node = last_node
        self._supports_action = supports_action
        self.factsheet_gate = factsheet_gate_from_environment() if factsheet_gate is None else bool(factsheet_gate)
        self.enabled = enabled_from_environment() if enabled is None else bool(enabled)

    def subscriptions(self) -> list[tuple[str, int]]:
        """What the MQTT client subscribes to after every (re)connect: nothing while the entry is off."""
        return [(JOB_TOPIC, 1)] if self.enabled else []

    def handles(self, topic: str) -> bool:
        return self.enabled and topic == JOB_TOPIC

    def on_message(self, client: MqttPublisher, topic: str, payload: bytes, *, retained: bool = False) -> dict[str, object] | None:
        """Handle one message of `cmd/job`; publish the answer and return it (None when there is nothing to answer). Never raises."""
        if not self.handles(topic):
            return None
        try:
            answer = self._decide(client, topic, payload, retained)
        except Exception as error:  # noqa: BLE001 - nothing a message does may end the loop
            _LOG.exception("could not handle %s", topic)
            answer = {"allowed": False, "reason": f"the job could not be handled: {type(error).__name__}"}
        if answer is None:
            return None
        client.publish(RESULT_TOPIC, json.dumps(answer))
        return answer

    def _decide(self, client: MqttPublisher, topic: str, payload: bytes, retained: bool) -> dict[str, object] | None:
        message = read_job_message(self._intake, topic, payload, retained=retained)
        if message.ignore:
            return None
        if message.refusal is not None:
            return message.refusal
        if message.repeat_answer is not None:
            try:
                return json.loads(message.repeat_answer)
            except ValueError:
                return {"allowed": False, "reason": "this idempotency key was already handled"}
        job = message.job
        assert job is not None
        # fail closed: what the AGV reported, not what the job claims; an unreadable cell is not READY
        observed = self._machine_state()
        cell = self._cell_state()
        if "goal_node" in job.parameters:
            return self._route_answer(client, job, cell, observed)
        dispatches = self._coordinator.dispatch_all(job, cell, self._transform, observed)
        unsupported = self._unsupported_by_vehicle(dispatches)
        if unsupported:
            refused: dict[str, object] = {
                "allowed": False,
                "reason": f"the factsheet of the vehicle does not list the action type {unsupported[0]!r}: nothing was published",
                "job_id": job.job_id,
                "unsupported_action_types": unsupported,
            }
            if self._store is not None:
                self._store.remember(job.idempotency_key, json.dumps(refused))
            return refused
        results = []
        for dispatch in dispatches:
            published = self._publisher.publish(client, self._target, None, None, dispatch, order_id=job.job_id)
            results.append(
                {
                    "action": dispatch.action,
                    "channel": dispatch.channel,
                    "accepted": dispatch.accepted,
                    "reason": dispatch.reason,
                    "published": published.published,
                    "publish_reason": published.reason,
                }
            )
        allowed = bool(results) and all(r["accepted"] and r["published"] for r in results)
        answer: dict[str, object] = {
            "allowed": allowed,
            "reason": "published" if allowed else next((str(r["publish_reason"] if r["accepted"] else r["reason"]) for r in results if not (r["accepted"] and r["published"])), "nothing to publish"),
            "job_id": job.job_id,
            "dispatches": results,
        }
        if self._store is not None:
            self._store.remember(job.idempotency_key, json.dumps(answer))
        return answer

    def _unsupported_by_vehicle(self, dispatches) -> list[str]:
        """The VDA 5050 action types of the accepted dispatches that the factsheet of the vehicle says it cannot do (empty when the gate is off, nothing is known or all are listed)."""
        if not self.factsheet_gate or self._supports_action is None:
            return []
        missing: list[str] = []
        for dispatch in dispatches:
            wire_type = AmrCoordinator.VDA_ACTION_TYPES.get(dispatch.action)
            if not dispatch.accepted or wire_type is None or wire_type in _SAFETY_WIRE_TYPES or wire_type in missing:
                continue
            if self._supports_action(wire_type) is False:
                missing.append(wire_type)
        return missing

    def _route_answer(self, client: MqttPublisher, job, cell: CellState, observed: MachineState | None) -> dict[str, object]:
        """A job with `goal_node`: planned on the plant graph from the last node the vehicle reported, published as one order."""
        if self._graph is None:
            answer: dict[str, object] = {"allowed": False, "reason": "goal_node needs a plant graph (HYDRA_UMC_AMR_PLANT_GRAPH) and none is loaded", "job_id": job.job_id}
        else:
            start = self._last_node() if self._last_node is not None else None
            route = self._coordinator.dispatch_route(job, cell, self._transform, self._graph, start, observed)
            published = self._publisher.publish_route(client, self._target, route, order_id=job.job_id)
            allowed = route.accepted and published.published
            answer = {
                "allowed": allowed,
                "reason": "published" if allowed else (published.reason if route.accepted else route.reason),
                "job_id": job.job_id,
                "route": list(route.route),
            }
        if self._store is not None:
            self._store.remember(job.idempotency_key, json.dumps(answer))
        return answer
