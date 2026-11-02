# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Job entry tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The jobs of cmd/job: off by default, retained and malformed messages stopped, a repeated key answered from the store, the gate decides with the state the AGV reported."""

import json
import os
import unittest
from unittest import mock

from hydra_umc_bridge_amr import AmrJobEntry, FrameTransform, Vda5050Publisher, Vda5050Target
from hydra_umc_bridge_amr.coordinator import AmrCoordinator
from hydra_umc_bridge_amr.job_entry import ENABLE_ENVIRONMENT, JOB_TOPIC, RESULT_TOPIC
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


def job(key="k1", phase="PREPARE", params=None) -> bytes:
    body = {"job_id": "job-1", "idempotency_key": key, "source": "orchestrator", "phase": phase, "machine_state": "IDLE", "parameters": params if params is not None else {"x": "1.5", "y": "2.5"}}
    return json.dumps(body).encode()


def entry(cell=CellState.READY, machine=MachineState.IDLE, enabled=True, store=None) -> AmrJobEntry:
    publisher = Vda5050Publisher(global_stop=GlobalStopGuard(GlobalStopSettings(False, DEFAULT_TOPIC, False)))
    return AmrJobEntry(AmrCoordinator(), publisher, TARGET, TRANSFORM, lambda: cell, lambda: machine, store, enabled=enabled)


class SwitchTests(unittest.TestCase):
    def test_off_by_default_subscribes_to_nothing_and_answers_nothing(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop(ENABLE_ENVIRONMENT, None)
            off = AmrJobEntry(AmrCoordinator(), Vda5050Publisher(), TARGET, TRANSFORM, lambda: CellState.READY, lambda: MachineState.IDLE)
        client = Client()
        self.assertFalse(off.enabled)
        self.assertEqual(off.subscriptions(), [])
        self.assertIsNone(off.on_message(client, JOB_TOPIC, job()))
        self.assertEqual(client.sent, [])

    def test_the_environment_switches_it_on(self) -> None:
        with mock.patch.dict(os.environ, {ENABLE_ENVIRONMENT: "1"}):
            on = AmrJobEntry(AmrCoordinator(), Vda5050Publisher(), TARGET, TRANSFORM, lambda: CellState.READY, lambda: MachineState.IDLE)
        self.assertTrue(on.enabled)
        self.assertEqual(on.subscriptions(), [(JOB_TOPIC, 1)])


class IntakeTests(unittest.TestCase):
    def test_a_good_job_publishes_the_order_and_answers_allowed(self) -> None:
        client = Client()
        answer = entry().on_message(client, JOB_TOPIC, job())
        self.assertTrue(answer["allowed"])
        self.assertEqual(len(client.vda()), 1)
        self.assertTrue(client.vda()[0].endswith("/order"))
        self.assertTrue(client.results()[0]["allowed"])

    def test_a_retained_message_is_ignored(self) -> None:
        client = Client()
        self.assertIsNone(entry().on_message(client, JOB_TOPIC, job(), retained=True))
        self.assertEqual(client.sent, [])

    def test_malformed_oversized_and_unreadable_payloads_are_refused_and_publish_nothing(self) -> None:
        for payload in (b"not json", b"[]", b"{}", b"x" * 20000, json.dumps({"job_id": "a"}).encode()):
            with self.subTest(size=len(payload)):
                client = Client()
                answer = entry().on_message(client, JOB_TOPIC, payload)
                self.assertFalse(answer["allowed"])
                self.assertEqual(client.vda(), [])

    def test_another_topic_is_not_ours(self) -> None:
        client = Client()
        self.assertIsNone(entry().on_message(client, "hydra/bridges/amr/cmd/other", job()))
        self.assertEqual(client.sent, [])

    def test_a_repeated_key_gets_the_stored_answer_and_publishes_nothing_more(self) -> None:
        client = Client()
        e = entry(store=IdempotencyStore())
        first = e.on_message(client, JOB_TOPIC, job())
        second = e.on_message(client, JOB_TOPIC, job())
        self.assertEqual(first, second)
        self.assertEqual(len(client.vda()), 1)


class GateTests(unittest.TestCase):
    def test_a_cell_that_is_not_ready_publishes_no_movement(self) -> None:
        client = Client()
        answer = entry(cell=CellState.SAFE_STOP).on_message(client, JOB_TOPIC, job())
        self.assertFalse(answer["allowed"])
        self.assertEqual(client.vda(), [])

    def test_the_state_the_agv_reported_decides_not_the_claim_in_the_job(self) -> None:
        client = Client()
        answer = entry(machine=MachineState.FAULT).on_message(client, JOB_TOPIC, job())
        self.assertFalse(answer["allowed"])
        self.assertEqual(client.vda(), [])

    def test_an_abort_is_forwarded_even_with_a_stopped_cell(self) -> None:
        client = Client()
        answer = entry(cell=CellState.SAFE_STOP, machine=MachineState.FAULT).on_message(client, JOB_TOPIC, job(key="k2", phase="ABORT", params={}))
        self.assertTrue(answer["allowed"])
        self.assertTrue(client.vda()[0].endswith("/instantActions"))

    def test_a_movement_without_coordinates_is_refused(self) -> None:
        client = Client()
        answer = entry().on_message(client, JOB_TOPIC, job(params={}))
        self.assertFalse(answer["allowed"])
        self.assertEqual(client.vda(), [])

    def test_a_failing_state_reader_is_answered_not_raised(self) -> None:
        def boom() -> MachineState:
            raise RuntimeError("no state")

        e = AmrJobEntry(AmrCoordinator(), Vda5050Publisher(), TARGET, TRANSFORM, lambda: CellState.READY, boom, enabled=True)
        client = Client()
        answer = e.on_message(client, JOB_TOPIC, job())
        self.assertFalse(answer["allowed"])
        self.assertEqual(client.vda(), [])


if __name__ == "__main__":
    unittest.main()
