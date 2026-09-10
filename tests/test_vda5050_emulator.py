# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Bridge <-> realistic VDA 5050 AGV emulator tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Run this bridge's REAL AmrCoordinator.dispatch() + Vda5050Publisher.publish()
end to end against a protocol-faithful VDA 5050 2.0.0 AGV emulator that
validates every published message the way a real AGV would and then
executes it - not a record-only FakeMqttClient.
"""
from __future__ import annotations

import unittest

from hydra_umc_bridge_amr import (
    AmrCoordinator,
    BridgeJob,
    CellState,
    FrameTransform,
    JobPhase,
    MachineState,
    Vda5050Publisher,
    Vda5050Target,
)

from vda5050_emulator import Vda5050AgvEmulator, Vda5050ValidationError

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")
TRANSFORM = FrameTransform(origin_x=0.0, origin_y=0.0, heading_rad=0.0, map_id="site-a")


def _job(phase: JobPhase, **params: object) -> BridgeJob:
    # BridgeJob.parameters is a real str->str map (the wire is text); the
    # coordinator does its own float() on x/y.
    return BridgeJob("job-1", "idem-1", "cell-a", phase, MachineState.IDLE, {k: str(v) for k, v in params.items()})


class BridgeAgainstVda5050AgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agv = Vda5050AgvEmulator(manufacturer="hydra-umc", serial_number="amr-1")
        self.coordinator = AmrCoordinator()
        self.publisher = Vda5050Publisher()

    def _dispatch_and_publish(self, phase: JobPhase, header_id: int, **params: object):
        dispatch = self.coordinator.dispatch(_job(phase, **params), CellState.READY, TRANSFORM)
        return self.publisher.publish(self.agv, TARGET, header_id, "2026-01-01T00:00:00Z", dispatch)

    def test_a_real_move_order_is_accepted_and_becomes_the_active_order(self) -> None:
        result = self._dispatch_and_publish(JobPhase.PROCESS, 1, x=1.5, y=2.5)
        self.assertTrue(result.published, result.reason)
        self.assertEqual(self.agv.validation_errors, [])
        self.assertEqual(len(self.agv.received), 1)
        channel, body = self.agv.received[0]
        self.assertEqual(channel, "order")
        # The AGV took it as its active order.
        self.assertEqual(self.agv.order_id, body["orderId"])
        state = self.agv.emit_state()
        self.assertEqual(state["orderId"], body["orderId"])
        self.assertEqual(state["actionStates"][0]["actionType"], "MOVE_TO_DESTINATION")
        self.assertEqual(state["actionStates"][0]["actionStatus"], "WAITING")

    def test_the_agv_executes_the_order_action_waiting_running_finished(self) -> None:
        self._dispatch_and_publish(JobPhase.PROCESS, 1, x=1.0, y=1.0)
        self.assertEqual(self.agv.action_states[0]["actionStatus"], "WAITING")
        self.agv.step()
        self.assertEqual(self.agv.action_states[0]["actionStatus"], "RUNNING")
        self.assertTrue(self.agv.emit_state()["driving"])
        self.agv.step()
        self.assertEqual(self.agv.action_states[0]["actionStatus"], "FINISHED")
        self.assertFalse(self.agv.emit_state()["driving"])

    def test_cancel_order_on_the_instant_actions_topic_preempts_a_running_order(self) -> None:
        self._dispatch_and_publish(JobPhase.PROCESS, 1, x=1.0, y=1.0)
        self.agv.step()  # action now RUNNING
        self.assertEqual(self.agv.action_states[0]["actionStatus"], "RUNNING")

        result = self._dispatch_and_publish(JobPhase.ABORT, 2)  # -> CANCEL_ORDER, instantActions
        self.assertTrue(result.published, result.reason)
        self.assertEqual(self.agv.received[-1][0], "instantActions")
        # Real AGV behaviour: the running action fails, the order is dropped, driving stops.
        self.assertEqual(self.agv.action_states[0]["actionStatus"], "FAILED")
        self.assertEqual(self.agv.order_id, "")
        self.assertFalse(self.agv.emit_state()["driving"])

    def test_a_stale_header_id_is_rejected_the_way_a_real_agv_rejects_it(self) -> None:
        self._dispatch_and_publish(JobPhase.PROCESS, 5, x=1.0, y=1.0)
        with self.assertRaises(Vda5050ValidationError):
            # headerId 3 < 5 on the same "order" channel - never increasing.
            self._dispatch_and_publish(JobPhase.PROCESS, 3, x=2.0, y=2.0)
        self.assertTrue(any("strictly increase" in e for e in self.agv.validation_errors))

    def test_the_agv_rejects_a_message_addressed_to_a_different_serial(self) -> None:
        other_target = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-99")
        dispatch = self.coordinator.dispatch(_job(JobPhase.PROCESS, x=1.0, y=1.0), CellState.READY, TRANSFORM)
        with self.assertRaises(Vda5050ValidationError):
            self.publisher.publish(self.agv, other_target, 1, "2026-01-01T00:00:00Z", dispatch)

    def test_a_non_movement_phase_publishes_a_valid_order_with_no_nodeposition(self) -> None:
        result = self._dispatch_and_publish(JobPhase.LOAD, 1)  # PICK_LOAD, no x/y
        self.assertTrue(result.published, result.reason)
        _channel, body = self.agv.received[0]
        self.assertNotIn("nodePosition", body["nodes"][0])
        self.assertEqual(self.agv.action_states[0]["actionType"], "PICK_LOAD")

    def test_an_emulated_estop_surfaces_in_the_state_safety_block_and_halts_execution(self) -> None:
        self._dispatch_and_publish(JobPhase.PROCESS, 1, x=1.0, y=1.0)
        self.agv.step()  # RUNNING
        self.agv.set_estop("MANUAL")
        state = self.agv.emit_state()
        self.assertEqual(state["safetyState"]["eStop"], "MANUAL")
        self.assertFalse(state["driving"])
        self.assertEqual(self.agv.action_states[0]["actionStatus"], "FAILED")
        # step() is a no-op while E-STOP is latched.
        self.agv.step()
        self.assertEqual(self.agv.action_states[0]["actionStatus"], "FAILED")

    def test_a_field_violation_stops_driving_and_a_non_automatic_mode_pauses_execution(self) -> None:
        self._dispatch_and_publish(JobPhase.PROCESS, 1, x=1.0, y=1.0)
        self.agv.set_operating_mode("MANUAL")
        self.agv.step()  # no-op: not AUTOMATIC
        self.assertEqual(self.agv.action_states[0]["actionStatus"], "WAITING")
        self.agv.set_operating_mode("AUTOMATIC")
        self.agv.set_field_violation(True)
        self.agv.step()  # no-op: field violation
        self.assertEqual(self.agv.action_states[0]["actionStatus"], "WAITING")
        self.assertFalse(self.agv.emit_state()["driving"])


if __name__ == "__main__":
    unittest.main()
