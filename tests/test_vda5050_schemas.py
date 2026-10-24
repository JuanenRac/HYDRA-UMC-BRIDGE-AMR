# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - tests/test_vda5050_schemas.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Every message the publisher builds is valid against the official VDA 5050 2.0.0 JSON schemas (vendored in tests/vda5050_schemas)."""

import json
import unittest
from pathlib import Path

from hydra_umc_bridge_amr import AmrCoordinator, BridgeJob, CellState, FrameTransform, JobPhase, MachineState
from hydra_umc_bridge_amr.mqtt_transport import Vda5050Publisher, Vda5050Target

try:
    import jsonschema
except ImportError:  # the schema checks need the dev extra; the rest of the suite does not
    jsonschema = None

SCHEMAS = Path(__file__).parent / "vda5050_schemas"
TARGET = Vda5050Target("uagv", "v2", "hydra", "amr-1")


class _Client:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload):
        self.sent.append((topic, json.loads(payload)))

        class _Info:
            rc = 0

        return _Info()


def _schema(name):
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


@unittest.skipIf(jsonschema is None, "jsonschema is not installed (pip install -e .[dev])")
class OfficialSchemaTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = AmrCoordinator()
        self.publisher = Vda5050Publisher(clock=lambda: "2026-01-01T00:00:00.000Z")
        self.transform = FrameTransform(0.0, 0.0, 0.0, "site-a")

    def _publish(self, phase, state=MachineState.IDLE, cell=CellState.READY, parameters=None, coordinator=None):
        job = BridgeJob("job-1", "idem-1", "amr-1", phase, state, parameters or {})
        dispatch = (coordinator or self.coordinator).dispatch(job, cell, self.transform, MachineState.IDLE if cell is CellState.READY else MachineState.FAULT)
        client = _Client()
        result = self.publisher.publish(client, TARGET, None, None, dispatch)
        self.assertTrue(result.published, result.reason)
        return client.sent[0]

    def _validate(self, payload, schema_name):
        jsonschema.Draft202012Validator(_schema(schema_name), format_checker=jsonschema.FormatChecker()).validate(payload)

    def test_the_vendored_schemas_are_the_official_version_2_documents(self):
        self.assertEqual(_schema("order")["subtopic"], "/order")
        self.assertEqual(_schema("instantActions")["subtopic"], "/instantActions")

    def test_every_order_the_bridge_builds_is_valid(self):
        movement = {"x": "4", "y": "6"}
        for phase, parameters in (
            (JobPhase.PREPARE, movement),
            (JobPhase.LOAD, {}),
            (JobPhase.PROCESS, movement),
            (JobPhase.UNLOAD, {}),
            (JobPhase.COMPLETE, movement),
        ):
            with self.subTest(phase=phase):
                topic, payload = self._publish(phase, parameters=parameters)
                self.assertTrue(topic.endswith("/order"))
                self._validate(payload, "order")

    def test_the_cancel_instant_action_is_valid(self):
        topic, payload = self._publish(JobPhase.ABORT, state=MachineState.FAULT, cell=CellState.FAULT)
        self.assertTrue(topic.endswith("/instantActions"))
        self._validate(payload, "instantActions")
        self.assertEqual(payload["actions"][0]["actionType"], "cancelOrder")

    def test_the_schema_check_has_teeth(self):
        _, payload = self._publish(JobPhase.PROCESS, parameters={"x": "1", "y": "2"})
        broken = {key: value for key, value in payload.items() if key != "orderId"}
        with self.assertRaises(jsonschema.ValidationError):
            self._validate(broken, "order")


class HoldOptionTests(unittest.TestCase):
    def test_the_default_abort_is_cancel_order_and_the_hold_is_a_choice(self):
        self.assertEqual(AmrCoordinator()._phase_actions[JobPhase.ABORT], AmrCoordinator.CANCEL_ORDER)
        self.assertEqual(AmrCoordinator(abort_action=AmrCoordinator.START_PAUSE)._phase_actions[JobPhase.ABORT], AmrCoordinator.START_PAUSE)
        with self.assertRaises(ValueError):
            AmrCoordinator(abort_action="MOVE_TO_HOME")

    def test_the_choice_belongs_to_one_coordinator(self):
        AmrCoordinator(abort_action=AmrCoordinator.START_PAUSE)
        self.assertEqual(AmrCoordinator()._phase_actions[JobPhase.ABORT], AmrCoordinator.CANCEL_ORDER)

    def test_the_hold_is_forwarded_whatever_the_cell_state_on_the_instant_channel(self):
        coordinator = AmrCoordinator(abort_action=AmrCoordinator.START_PAUSE)
        job = BridgeJob("job-1", "idem-1", "amr-1", JobPhase.ABORT, MachineState.FAULT, {})
        result = coordinator.dispatch(job, CellState.FAULT, FrameTransform(0.0, 0.0, 0.0, "site-a"), MachineState.FAULT)
        self.assertTrue(result.accepted)
        self.assertEqual(result.action, "START_PAUSE")
        self.assertEqual(result.channel, "instantActions")
        self.assertIn("START_PAUSE", coordinator.order_plan().instant_actions)

    @unittest.skipIf(jsonschema is None, "jsonschema is not installed (pip install -e .[dev])")
    def test_the_pause_message_is_valid_against_the_official_schema(self):
        coordinator = AmrCoordinator(abort_action=AmrCoordinator.START_PAUSE)
        job = BridgeJob("job-1", "idem-1", "amr-1", JobPhase.ABORT, MachineState.FAULT, {})
        dispatch = coordinator.dispatch(job, CellState.FAULT, FrameTransform(0.0, 0.0, 0.0, "site-a"), MachineState.FAULT)
        client = _Client()
        result = Vda5050Publisher(clock=lambda: "2026-01-01T00:00:00.000Z").publish(client, TARGET, None, None, dispatch)
        self.assertTrue(result.published, result.reason)
        _, payload = client.sent[0]
        jsonschema.Draft202012Validator(_schema("instantActions"), format_checker=jsonschema.FormatChecker()).validate(payload)
        self.assertEqual(payload["actions"][0]["actionType"], "startPause")
        self.assertEqual(payload["actions"][0]["actionName"], "startPause")


class ResumeAndBothTests(unittest.TestCase):
    def test_resume_needs_a_ready_cell_and_a_vehicle_read_as_paused(self):
        coordinator = AmrCoordinator()
        ok = coordinator.resume(CellState.READY, MachineState.HOLDING)
        self.assertTrue(ok.accepted)
        self.assertEqual((ok.action, ok.channel), ("STOP_PAUSE", "instantActions"))
        for cell in (CellState.INHIBITED, CellState.FAULT, CellState.SAFE_STOP):
            self.assertFalse(coordinator.resume(cell, MachineState.HOLDING).accepted, cell)
        for machine in (MachineState.IDLE, MachineState.RUNNING, MachineState.FAULT, MachineState.SAFE_STOP, MachineState.OFFLINE, None):
            self.assertFalse(coordinator.resume(CellState.READY, machine).accepted, machine)

    def test_stop_pause_is_in_the_vocabulary_as_an_instant_action(self):
        plan = AmrCoordinator().order_plan()
        self.assertIn("STOP_PAUSE", plan.instant_actions)
        self.assertNotIn("STOP_PAUSE", plan.actions)

    @unittest.skipIf(jsonschema is None, "jsonschema is not installed (pip install -e .[dev])")
    def test_the_resume_message_is_valid_against_the_official_schema(self):
        dispatch = AmrCoordinator().resume(CellState.READY, MachineState.HOLDING)
        client = _Client()
        result = Vda5050Publisher(clock=lambda: "2026-01-01T00:00:00.000Z").publish(client, TARGET, None, None, dispatch)
        self.assertTrue(result.published, result.reason)
        _, payload = client.sent[0]
        jsonschema.Draft202012Validator(_schema("instantActions"), format_checker=jsonschema.FormatChecker()).validate(payload)
        self.assertEqual(payload["actions"][0]["actionType"], "stopPause")

    def test_cancel_and_pause_sends_both_in_order_whatever_the_cell_state(self):
        coordinator = AmrCoordinator(abort_action=AmrCoordinator.ABORT_BOTH)
        job = BridgeJob("job-1", "idem-1", "amr-1", JobPhase.ABORT, MachineState.FAULT, {})
        sent = coordinator.dispatch_all(job, CellState.FAULT, FrameTransform(0.0, 0.0, 0.0, "site-a"), MachineState.FAULT)
        self.assertEqual([d.action for d in sent], ["CANCEL_ORDER", "START_PAUSE"])
        self.assertTrue(all(d.accepted and d.channel == "instantActions" for d in sent))
        self.assertIn("START_PAUSE", coordinator.order_plan().instant_actions)

    def test_the_other_choices_send_one_and_other_phases_send_one(self):
        transform = FrameTransform(0.0, 0.0, 0.0, "site-a")
        abort = BridgeJob("job-1", "idem-1", "amr-1", JobPhase.ABORT, MachineState.IDLE, {})
        self.assertEqual(len(AmrCoordinator().dispatch_all(abort, CellState.READY, transform, MachineState.IDLE)), 1)
        both = AmrCoordinator(abort_action=AmrCoordinator.ABORT_BOTH)
        process = BridgeJob("job-2", "idem-2", "amr-1", JobPhase.PROCESS, MachineState.IDLE, {"x": "1", "y": "2"})
        self.assertEqual(len(both.dispatch_all(process, CellState.READY, transform, MachineState.IDLE)), 1)

    def test_a_bad_choice_is_still_refused(self):
        with self.assertRaises(ValueError):
            AmrCoordinator(abort_action="STOP_PAUSE")
