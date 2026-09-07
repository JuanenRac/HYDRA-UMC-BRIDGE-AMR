# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Coordinator and frame-transform tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================

import math
import unittest
from types import SimpleNamespace

from hydra_umc_sdk.bridge_contract import BridgeError
from hydra_umc_bridge_amr import AmrCoordinator, BridgeJob, CellState, FrameTransform, JobPhase, MachineState


def job(phase=JobPhase.PROCESS, state=MachineState.IDLE, parameters=None):
    return BridgeJob("job-1", "idempotency-1", "amr-1", phase, state, parameters or {})


class FrameTransformTests(unittest.TestCase):
    """Real, hand-checkable 2D rigid-body transform - not just a smoke test."""

    def test_identity_transform_is_a_pass_through(self):
        transform = FrameTransform(0.0, 0.0, 0.0, "site-a")
        self.assertEqual(transform.to_local(5.0, 3.0), (5.0, 3.0))

    def test_pure_translation(self):
        transform = FrameTransform(10.0, 20.0, 0.0, "site-a")
        x, y = transform.to_local(15.0, 25.0)
        self.assertAlmostEqual(x, 5.0)
        self.assertAlmostEqual(y, 5.0)

    def test_ninety_degree_heading_rotates_factory_axes_into_local_axes(self):
        # Hand-derived: with the AMR's local frame rotated +90 deg from the
        # factory frame, a point one unit along the factory's own +X axis
        # from the AMR's origin lands on the AMR's own local -Y axis, and
        # one unit along factory +Y lands on local +X.
        transform = FrameTransform(10.0, 20.0, math.pi / 2, "site-a")
        origin_x, origin_y = transform.to_local(10.0, 20.0)
        self.assertAlmostEqual(origin_x, 0.0)
        self.assertAlmostEqual(origin_y, 0.0)
        plus_x, minus_y = transform.to_local(11.0, 20.0)
        self.assertAlmostEqual(plus_x, 0.0)
        self.assertAlmostEqual(minus_y, -1.0)
        plus_x2, plus_y2 = transform.to_local(10.0, 21.0)
        self.assertAlmostEqual(plus_x2, 1.0)
        self.assertAlmostEqual(plus_y2, 0.0)

    def test_to_factory_is_the_real_inverse_of_to_local(self):
        transform = FrameTransform(-3.2, 7.7, 1.1, "site-a")
        original = (37.5, -12.25)
        local = transform.to_local(*original)
        round_tripped = transform.to_factory(*local)
        self.assertAlmostEqual(round_tripped[0], original[0])
        self.assertAlmostEqual(round_tripped[1], original[1])

    def test_non_finite_transform_or_coordinate_is_rejected(self):
        with self.assertRaises(ValueError):
            FrameTransform(float("nan"), 0.0, 0.0, "site-a")
        with self.assertRaises(ValueError):
            FrameTransform(0.0, 0.0, 0.0, "site-a").to_local(float("inf"), 0.0)

    def test_missing_map_id_is_rejected_regression_for_amr_02(self):
        # AMR-02 (found in an ecosystem-wide software-improvements audit):
        # a real VDA 5050 nodePosition requires mapId - map_id must be a
        # real, non-empty identity, never silently defaulted.
        with self.assertRaises(ValueError):
            FrameTransform(0.0, 0.0, 0.0, "")


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = AmrCoordinator()
        self.identity = FrameTransform(0.0, 0.0, 0.0, "site-a")

    def test_move_action_resolves_the_real_local_target(self):
        result = self.coordinator.dispatch(job(parameters={"x": "4", "y": "6"}), CellState.READY, FrameTransform(1.0, 2.0, 0.0, "site-a"))
        self.assertTrue(result.accepted)
        self.assertEqual(result.action, "MOVE_TO_DESTINATION")
        self.assertAlmostEqual(result.local_x, 3.0)
        self.assertAlmostEqual(result.local_y, 4.0)
        self.assertEqual(result.map_id, "site-a")  # AMR-02 regression: real map identity carried through

    def test_move_action_without_coordinates_is_rejected_before_any_transport(self):
        result = self.coordinator.dispatch(job(parameters={}), CellState.READY, self.identity)
        self.assertFalse(result.accepted)
        self.assertIn("x", result.reason)

    def test_move_action_with_non_numeric_coordinates_is_rejected(self):
        result = self.coordinator.dispatch(job(parameters={"x": "not-a-number", "y": "0"}), CellState.READY, self.identity)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.local_x)

    def test_move_action_with_non_finite_coordinates_is_rejected(self):
        result = self.coordinator.dispatch(job(parameters={"x": "nan", "y": "0"}), CellState.READY, self.identity)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.local_x)

    def test_pick_load_needs_no_coordinate(self):
        result = self.coordinator.dispatch(job(JobPhase.LOAD, parameters={}), CellState.READY, self.identity)
        self.assertTrue(result.accepted)
        self.assertEqual(result.action, "PICK_LOAD")
        self.assertIsNone(result.local_x)
        self.assertIsNone(result.map_id)  # non-movement action - no coordinate, no map either

    def test_busy_machine_is_not_reused(self):
        result = self.coordinator.dispatch(job(state=MachineState.RUNNING, parameters={"x": "0", "y": "0"}), CellState.READY, self.identity)
        self.assertFalse(result.accepted)

    def test_cancel_order_stays_available_during_fault(self):
        result = self.coordinator.dispatch(job(JobPhase.ABORT, MachineState.FAULT, {}), CellState.FAULT, self.identity)
        self.assertTrue(result.accepted)
        self.assertEqual(result.action, "CANCEL_ORDER")

    def test_cancel_order_is_dispatched_on_the_real_instant_actions_channel(self):
        # Real VDA 5050 wire compatibility: cancelOrder is a documented
        # instantActions-topic command, not an order-topic one - a future
        # transport adapter needs this to route it correctly.
        result = self.coordinator.dispatch(job(JobPhase.ABORT, MachineState.FAULT, {}), CellState.FAULT, self.identity)
        self.assertEqual(result.channel, "instantActions")

    def test_movement_and_load_actions_are_dispatched_on_the_real_order_channel(self):
        result = self.coordinator.dispatch(job(parameters={"x": "0", "y": "0"}), CellState.READY, self.identity)
        self.assertEqual(result.channel, "order")

    # V07-014 (found in an independent revalidation audit, P2, shared
    # with BRIDGE-DROIDS/BRIDGE-OPENPNP/BRIDGE-ROS2): HYDRA-UMC-SDK's own
    # real fix (REV-008) now rejects an unrecognised `phase` AT
    # CONSTRUCTION TIME (`BridgeJob.__post_init__` requires a real
    # `JobPhase` member) - this test used to construct one directly with
    # a raw string, which is no longer possible through the real public
    # constructor at all. Split in two, same as HYDRA-UMC-BRIDGE-UAV's
    # own already-updated test: construction rejection below, and the
    # coordinator's own defensive fallback covered separately with a
    # minimal explicit double instead of weakening the SDK's public
    # contract to let the old construction succeed again.
    def test_constructing_a_bridge_job_with_an_unknown_phase_is_refused_by_the_sdk_itself(self):
        with self.assertRaises(BridgeError):
            BridgeJob("job-2", "idempotency-2", "amr-1", "SOME_FUTURE_PHASE", MachineState.IDLE, {})

    def test_unknown_sdk_phase_fails_closed_instead_of_guessing_an_action(self):
        # A real BridgeJob can never carry an unmapped phase any more (see
        # above) - this proves dispatch()'s own `_phase_actions.get(...)`
        # fallback still fails closed as defense-in-depth, using a minimal
        # explicit double (only the one attribute this fallback actually
        # reads before returning) rather than a real BridgeJob the SDK
        # would now refuse to construct at all.
        unknown = SimpleNamespace(phase="SOME_FUTURE_PHASE")
        result = self.coordinator.dispatch(unknown, CellState.READY, self.identity)
        self.assertFalse(result.accepted)
        self.assertEqual(result.action, "none")

    def test_order_plan_is_static_and_explicitly_not_a_runtime(self):
        plan = self.coordinator.order_plan().to_dict()
        self.assertEqual(plan["schema_version"], "1.1")
        self.assertEqual(plan["mode"], "plan-only")
        self.assertIn("MOVE_TO_DESTINATION", plan["actions"])

    def test_order_plan_separates_the_real_vda5050_order_and_instant_action_channels(self):
        # cancelOrder is a real, documented VDA 5050 instantActions-topic
        # command (github.com/VDA5050/VDA5050 json_schemas/) - it must
        # never appear in the queued "actions" list, only in the separate
        # "instant_actions" one.
        plan = self.coordinator.order_plan().to_dict()
        self.assertNotIn("CANCEL_ORDER", plan["actions"])
        self.assertIn("CANCEL_ORDER", plan["instant_actions"])
        self.assertNotIn("MOVE_TO_DESTINATION", plan["instant_actions"])


if __name__ == "__main__":
    unittest.main()
