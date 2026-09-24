# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Order-state table and simulated AMR tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================

import unittest

from hydra_umc_bridge_amr import AmrCoordinator, BridgeJob, CellState, FrameTransform, JobPhase, MachineState
from hydra_umc_bridge_amr.simulated_amr import TRANSITIONS, AmrState, SimulatedAmr

TRANSFORM = FrameTransform(0.0, 0.0, 0.0, "site-a")


def dispatch(phase, cell=CellState.READY):
    job = BridgeJob("job-1", "idem-1", "amr-1", phase, MachineState.IDLE, {"x": "1.0", "y": "2.0"})
    return AmrCoordinator().dispatch(job, cell, TRANSFORM)


class SimulatedAmrTests(unittest.TestCase):
    def test_every_coordinator_action_has_a_rule(self):
        actions = set(AmrCoordinator._phase_actions.values())
        self.assertEqual(actions, set(TRANSITIONS))

    def test_full_job_sequence_walks_the_table(self):
        amr = SimulatedAmr()
        for phase, expected in (
            (JobPhase.PREPARE, AmrState.MOVING),
            (JobPhase.LOAD, AmrState.LOADED_WAITING),
            (JobPhase.PROCESS, AmrState.MOVING),
            (JobPhase.UNLOAD, AmrState.UNLOADING),
            (JobPhase.COMPLETE, AmrState.MOVING),
        ):
            result = amr.apply(dispatch(phase))
            self.assertTrue(result.applied, result.reason)
            self.assertEqual(amr.state, expected)
        self.assertEqual(len(amr.history), 5)

    def test_out_of_order_step_is_refused_and_state_is_unchanged(self):
        amr = SimulatedAmr()
        result = amr.apply(dispatch(JobPhase.UNLOAD))
        self.assertFalse(result.applied)
        self.assertEqual(amr.state, AmrState.IDLE)
        self.assertEqual(amr.history, ())

    def test_a_dispatch_the_coordinator_refused_never_changes_state(self):
        amr = SimulatedAmr()
        refused = dispatch(JobPhase.PREPARE, cell=CellState.SAFE_STOP)
        self.assertFalse(refused.accepted)
        self.assertFalse(amr.apply(refused).applied)
        self.assertEqual(amr.state, AmrState.IDLE)

    def test_abort_recovers_from_every_state(self):
        for state in AmrState:
            amr = SimulatedAmr()
            amr._state = state
            result = amr.apply(dispatch(JobPhase.ABORT, cell=CellState.FAULT))
            self.assertTrue(result.applied, state)
            self.assertEqual(amr.state, AmrState.IDLE)

    def test_module_imports_no_transport(self):
        import hydra_umc_bridge_amr.simulated_amr as module

        source = open(module.__file__, encoding="utf-8").read()
        self.assertNotIn("mqtt_transport", source.split('"""', 2)[2])


if __name__ == "__main__":
    unittest.main()
