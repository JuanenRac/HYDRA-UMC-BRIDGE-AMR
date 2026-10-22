# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Observed machine state and VDA 5050 order ids that survive a restart
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
import json
import unittest

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState

from hydra_umc_bridge_amr import AmrCoordinator, FrameTransform, Vda5050Publisher, Vda5050Target

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")
TRANSFORM = FrameTransform(0.0, 0.0, 0.0, "site-a")


class Client:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))

        class Info:
            rc = 0

        return Info()


def job(claimed=MachineState.IDLE, phase=JobPhase.PROCESS):
    return BridgeJob("job-1", "key-1", "orchestrator", phase, claimed, {"x": "1.0", "y": "2.0"})


class ObservedStateTests(unittest.TestCase):
    def test_the_state_the_fleet_reported_beats_the_claim_in_the_job(self):
        dispatch = AmrCoordinator().dispatch(job(MachineState.IDLE), CellState.READY, TRANSFORM, MachineState.RUNNING)
        self.assertFalse(dispatch.accepted)
        self.assertIn("RUNNING", dispatch.reason)

    def test_an_observed_idle_vehicle_is_accepted_whatever_the_job_claims(self):
        dispatch = AmrCoordinator().dispatch(job(MachineState.FAULT), CellState.READY, TRANSFORM, MachineState.IDLE)
        self.assertTrue(dispatch.accepted)

    def test_without_an_observation_the_job_is_refused_whatever_it_claims(self):
        # the claim in the job message is not a reading of the vehicle: no reading, no productive order
        for claimed in (MachineState.IDLE, MachineState.RUNNING):
            dispatch = AmrCoordinator().dispatch(job(claimed), CellState.READY, TRANSFORM)
            self.assertFalse(dispatch.accepted)
            self.assertIn("did not read", dispatch.reason)

    def test_an_abort_needs_no_observation(self):
        dispatch = AmrCoordinator().dispatch(job(phase=JobPhase.ABORT), CellState.READY, TRANSFORM)
        self.assertTrue(dispatch.accepted)


class OrderIdTests(unittest.TestCase):
    def order_id(self, publisher, header_id=1, **kwargs):
        client = Client()
        dispatch = AmrCoordinator().dispatch(job(), CellState.READY, TRANSFORM, MachineState.IDLE)
        publisher.publish(client, TARGET, header_id, "2026-01-01T00:00:00Z", dispatch, **kwargs)
        return json.loads(client.published[0][1])["orderId"]

    def test_a_restarted_publisher_does_not_reuse_the_order_ids_of_the_previous_run(self):
        # the header id starts again from 1 after a restart: without a token of its own the orderId would be the same
        self.assertNotEqual(self.order_id(Vda5050Publisher()), self.order_id(Vda5050Publisher()))

    def test_one_publisher_gives_a_different_order_id_to_each_header_id(self):
        publisher = Vda5050Publisher()
        self.assertNotEqual(self.order_id(publisher, 1), self.order_id(publisher, 2))

    def test_a_caller_may_name_the_order(self):
        self.assertEqual(self.order_id(Vda5050Publisher(), 5, order_id="job-1"), "job-1")


if __name__ == "__main__":
    unittest.main()
