# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - tests/test_factsheet_gate.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A job that needs an action the vehicle says it cannot do is refused before anything is published; unknown never refuses and a stop always passes.

Evidence: simulator (a factsheet written by the test; no vehicle has answered a factsheet request).
"""

import json
import os
import unittest
from unittest import mock

from hydra_umc_bridge_amr import AmrJobEntry, FrameTransform, Vda5050Publisher, Vda5050Target
from hydra_umc_bridge_amr.coordinator import AmrCoordinator
from hydra_umc_bridge_amr.job_entry import FACTSHEET_GATE_ENVIRONMENT, JOB_TOPIC, RESULT_TOPIC
from hydra_umc_bridge_amr.observer import Vda5050Observer
from hydra_umc_sdk.bridge_contract import CellState, MachineState
from hydra_umc_sdk.global_stop import DEFAULT_TOPIC, GlobalStopGuard, GlobalStopSettings
from hydra_umc_sdk.job_intake import IdempotencyStore

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")
TRANSFORM = FrameTransform(0.0, 0.0, 0.0, "site-a")


class Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def publish(self, topic: str, payload: str) -> object:
        self.sent.append((topic, payload))
        return type("Info", (), {"rc": 0})()

    def results(self) -> list[dict]:
        return [json.loads(p) for t, p in self.sent if t == RESULT_TOPIC]

    def vda(self) -> list[str]:
        return [t for t, _ in self.sent if t != RESULT_TOPIC]


def job(phase, key="k1", params=None) -> bytes:
    body = {"job_id": "job-1", "idempotency_key": key, "source": "orchestrator", "phase": phase, "machine_state": "IDLE", "parameters": params if params is not None else {"x": "1.5", "y": "2.5"}}
    return json.dumps(body).encode()


def factsheet(*types) -> bytes:
    return json.dumps({"manufacturer": "hydra-umc", "serialNumber": "amr-1", "protocolFeatures": {"agvActions": [{"actionType": t, "actionScopes": ["NODE"]} for t in types]}}).encode()


def make(types=None, gate=None, store=None):
    observer = Vda5050Observer(TARGET, factsheet=True)
    if types is not None:
        observer.on_message(observer.factsheet_topic, factsheet(*types))
    publisher = Vda5050Publisher(global_stop=GlobalStopGuard(GlobalStopSettings(False, DEFAULT_TOPIC, False)))
    entry = AmrJobEntry(
        AmrCoordinator(), publisher, TARGET, TRANSFORM, lambda: CellState.READY, lambda: MachineState.IDLE, store,
        enabled=True, supports_action=observer.supports_action, factsheet_gate=gate,
    )
    return entry


class FactsheetGateTests(unittest.TestCase):
    def test_a_pick_is_refused_when_the_vehicle_does_not_list_it(self):
        client = Client()
        answer = make(types=("drop",)).on_message(client, JOB_TOPIC, job("LOAD", params={}))
        self.assertFalse(answer["allowed"])
        self.assertIn("pick", answer["reason"])
        self.assertEqual(answer["unsupported_action_types"], ["pick"])
        self.assertEqual(client.vda(), [])

    def test_a_pick_goes_through_when_the_vehicle_lists_it(self):
        client = Client()
        answer = make(types=("pick", "drop")).on_message(client, JOB_TOPIC, job("LOAD", params={}))
        self.assertTrue(answer["allowed"], answer)
        self.assertEqual(len(client.vda()), 1)

    def test_no_factsheet_heard_never_refuses(self):
        client = Client()
        answer = make(types=None).on_message(client, JOB_TOPIC, job("LOAD", params={}))
        self.assertTrue(answer["allowed"], answer)

    def test_a_movement_has_no_action_type_and_is_not_refused(self):
        client = Client()
        answer = make(types=("drop",)).on_message(client, JOB_TOPIC, job("PREPARE"))
        self.assertTrue(answer["allowed"], answer)

    def test_the_actions_that_stop_the_vehicle_are_never_refused(self):
        client = Client()
        answer = make(types=("pick",)).on_message(client, JOB_TOPIC, job("ABORT", params={}))
        self.assertTrue(answer["allowed"], answer)
        self.assertEqual(len(client.vda()), 1)

    def test_the_switch_turns_the_gate_off(self):
        client = Client()
        answer = make(types=("drop",), gate=False).on_message(client, JOB_TOPIC, job("LOAD", params={}))
        self.assertTrue(answer["allowed"], answer)

    def test_the_gate_is_on_unless_the_environment_says_zero(self):
        for raw, expected in (("", True), ("1", True), ("true", True), ("0", False)):
            with mock.patch.dict(os.environ, {FACTSHEET_GATE_ENVIRONMENT: raw}):
                self.assertEqual(make(types=("drop",)).factsheet_gate, expected, raw)

    def test_without_a_way_to_ask_the_vehicle_nothing_changes(self):
        publisher = Vda5050Publisher(global_stop=GlobalStopGuard(GlobalStopSettings(False, DEFAULT_TOPIC, False)))
        entry = AmrJobEntry(AmrCoordinator(), publisher, TARGET, TRANSFORM, lambda: CellState.READY, lambda: MachineState.IDLE, enabled=True)
        self.assertTrue(entry.on_message(Client(), JOB_TOPIC, job("LOAD", params={}))["allowed"])

    def test_the_refusal_is_remembered_for_a_repeated_key(self):
        store = IdempotencyStore()
        entry = make(types=("drop",), store=store)
        first = entry.on_message(Client(), JOB_TOPIC, job("LOAD", params={}))
        second = entry.on_message(Client(), JOB_TOPIC, job("LOAD", params={}))
        self.assertFalse(first["allowed"])
        self.assertEqual(first["reason"], second["reason"])


if __name__ == "__main__":
    unittest.main()
