# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - A repeated job key whose first attempt left no answer
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A repeat whose first attempt left no answer is answered with what the vehicle reports now (switch off by default) and never published again; no reading keeps it refused. Simulated reading, evidence=simulator."""

import json
import os
import unittest
from unittest import mock

from hydra_umc_bridge_amr import AmrJobEntry, FrameTransform, Vda5050Publisher, Vda5050Target
from hydra_umc_bridge_amr.coordinator import AmrCoordinator
from hydra_umc_bridge_amr.job_entry import IN_DOUBT_CONSULT_ENVIRONMENT, JOB_TOPIC, RESULT_TOPIC
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


def job(key="k1") -> bytes:
    body = {"job_id": "job-1", "idempotency_key": key, "source": "orchestrator", "phase": "PREPARE", "machine_state": "IDLE", "parameters": {"x": "1.5", "y": "2.5"}}
    return json.dumps(body).encode()


def in_doubt_store(key="k1") -> IdempotencyStore:
    store = IdempotencyStore()
    assert store.claim(key)  # the first attempt claimed the key and never stored an answer
    return store


def entry(store, machine=MachineState.RUNNING, **options) -> AmrJobEntry:
    publisher = Vda5050Publisher(global_stop=GlobalStopGuard(GlobalStopSettings(False, DEFAULT_TOPIC, False)))
    return AmrJobEntry(AmrCoordinator(), publisher, TARGET, TRANSFORM, lambda: CellState.READY, lambda: machine, store, enabled=True, **options)


class OffByDefaultTests(unittest.TestCase):
    def test_off_by_default_a_job_in_doubt_is_refused_as_before(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop(IN_DOUBT_CONSULT_ENVIRONMENT, None)
            on = entry(in_doubt_store())
        client = Client()
        answer = on.on_message(client, JOB_TOPIC, job())
        self.assertFalse(answer["allowed"])
        self.assertIn("already handled", answer["reason"])
        self.assertNotIn("in_doubt", answer)
        self.assertEqual(client.vda(), [])

    def test_the_environment_switch_turns_it_on(self) -> None:
        with mock.patch.dict(os.environ, {IN_DOUBT_CONSULT_ENVIRONMENT: "1"}):
            on = entry(in_doubt_store())
        self.assertTrue(on.on_message(Client(), JOB_TOPIC, job())["in_doubt"])

    def test_any_other_value_of_the_switch_keeps_it_off(self) -> None:
        for value in ("0", "", "true", "yes"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {IN_DOUBT_CONSULT_ENVIRONMENT: value}):
                on = entry(in_doubt_store())
                self.assertNotIn("in_doubt", on.on_message(Client(), JOB_TOPIC, job()))


class ConsultTests(unittest.TestCase):
    def test_the_repeat_is_answered_with_the_state_the_vehicle_reports_and_nothing_is_published(self) -> None:
        client = Client()
        answer = entry(in_doubt_store(), in_doubt_consult=True).on_message(client, JOB_TOPIC, job())
        self.assertEqual((answer["allowed"], answer["in_doubt"], answer["published"], answer["machine_state"]), (False, True, False, "RUNNING"))
        self.assertEqual(answer["job_id"], "job-1")
        self.assertIn("RUNNING", answer["reason"])
        self.assertEqual(client.vda(), [], "the order is never sent again")
        self.assertEqual(client.results(), [answer])

    def test_an_idle_vehicle_and_a_ready_cell_do_not_turn_a_repeat_into_a_new_job(self) -> None:
        client = Client()
        answer = entry(in_doubt_store(), machine=MachineState.IDLE, in_doubt_consult=True).on_message(client, JOB_TOPIC, job())
        self.assertFalse(answer["allowed"])
        self.assertEqual(client.vda(), [])

    def test_the_last_node_the_vehicle_reported_is_part_of_the_answer(self) -> None:
        answer = entry(in_doubt_store(), in_doubt_consult=True, last_node=lambda: "node-7").on_message(Client(), JOB_TOPIC, job())
        self.assertEqual(answer["last_node"], "node-7")

    def test_a_vehicle_that_reports_nothing_leaves_the_repeat_refused(self) -> None:
        answer = entry(in_doubt_store(), machine=None, in_doubt_consult=True).on_message(Client(), JOB_TOPIC, job())
        self.assertFalse(answer["allowed"])
        self.assertNotIn("in_doubt", answer)
        self.assertIn("not run again", answer["reason"])

    def test_a_reading_that_raises_leaves_the_repeat_refused(self) -> None:
        def broken() -> MachineState:
            raise RuntimeError("link lost")

        publisher = Vda5050Publisher(global_stop=GlobalStopGuard(GlobalStopSettings(False, DEFAULT_TOPIC, False)))
        refused = AmrJobEntry(AmrCoordinator(), publisher, TARGET, TRANSFORM, lambda: CellState.READY, broken, in_doubt_store(), enabled=True, in_doubt_consult=True)
        answer = refused.on_message(Client(), JOB_TOPIC, job())
        self.assertFalse(answer["allowed"])
        self.assertNotIn("in_doubt", answer)

    def test_the_answer_is_stored_so_a_third_repeat_gets_the_same_one_without_a_new_reading(self) -> None:
        readings = [MachineState.HOLDING, MachineState.IDLE]
        publisher = Vda5050Publisher(global_stop=GlobalStopGuard(GlobalStopSettings(False, DEFAULT_TOPIC, False)))
        bridge = AmrJobEntry(AmrCoordinator(), publisher, TARGET, TRANSFORM, lambda: CellState.READY, lambda: readings.pop(0), in_doubt_store(), enabled=True, in_doubt_consult=True)
        first = bridge.on_message(Client(), JOB_TOPIC, job())
        third = bridge.on_message(Client(), JOB_TOPIC, job())
        self.assertEqual(third, first)
        self.assertEqual(first["machine_state"], "HOLDING")
        self.assertEqual(readings, [MachineState.IDLE], "the second repeat did not read the vehicle again")

    def test_a_key_with_a_stored_answer_still_gets_that_answer_not_a_reading(self) -> None:
        bridge = entry(IdempotencyStore(), machine=MachineState.IDLE, in_doubt_consult=True)
        first = bridge.on_message(Client(), JOB_TOPIC, job())
        second = bridge.on_message(Client(), JOB_TOPIC, job())
        self.assertEqual(second, first)
        self.assertNotIn("in_doubt", second)

    def test_a_new_key_is_decided_normally_with_the_switch_on(self) -> None:
        client = Client()
        answer = entry(in_doubt_store("other"), machine=MachineState.IDLE, in_doubt_consult=True).on_message(client, JOB_TOPIC, job("k2"))
        self.assertTrue(answer["allowed"])
        self.assertEqual(len(client.vda()), 1)

    def test_without_a_store_nothing_is_in_doubt(self) -> None:
        answer = entry(None, machine=MachineState.IDLE, in_doubt_consult=True).on_message(Client(), JOB_TOPIC, job())
        self.assertTrue(answer["allowed"])


if __name__ == "__main__":
    unittest.main()
