# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - A shift of jobs against the VDA 5050 AGV emulator, with the vehicle's own state as the gate
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A sequence of jobs where the bridge only moves when the vehicle's own reported state says it is idle (evidence: simulator).

The real coordinator, publisher and observer run against `Vda5050AgvEmulator`: after every step the emulated vehicle's `state`
and `connection` messages are fed to the observer, whose reading is what the gate sees. The faults (field violation,
emergency stop, a manual mode, a silent vehicle, a lost connection) are injected on the emulator side. Nothing here has
run against a real AGV or fleet manager.
"""
from __future__ import annotations

import json
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
from hydra_umc_bridge_amr.observer import Vda5050Observer

from vda5050_emulator import Vda5050AgvEmulator

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")
TRANSFORM = FrameTransform(origin_x=0.0, origin_y=0.0, heading_rad=0.0, map_id="site-a")


class Shift:
    """One cell, one vehicle: jobs go through the gate on the observed state; time moves one emulator step at a time."""

    def __init__(self) -> None:
        self.agv = Vda5050AgvEmulator(manufacturer="hydra-umc", serial_number="amr-1")
        self.clock = [0.0]
        self.observer = Vda5050Observer(TARGET, clock=lambda: self.clock[0])
        self.coordinator, self.publisher = AmrCoordinator(), Vda5050Publisher()
        self.header = 0
        self.job_number = 0
        self.cell = CellState.READY
        self.published_orders = 0
        self.report()

    def report(self, connection: str = "ONLINE") -> None:
        self.observer.on_message(self.observer.connection_topic, json.dumps({"manufacturer": "hydra-umc", "serialNumber": "amr-1", "connectionState": connection}).encode())
        self.observer.on_message(self.observer.state_topic, json.dumps(self.agv.emit_state()).encode())

    def step(self, ticks: int = 1) -> None:
        for _ in range(ticks):
            self.agv.step()
            self.clock[0] += 1.0
            self.report()

    def settle(self, limit: int = 20) -> MachineState:
        """Step until the vehicle reads idle (or the limit), so a stopped vehicle is stepped too and a fault that does not clear shows."""
        for _ in range(limit):
            if self.observer.machine_state() is MachineState.IDLE:
                break
            self.step()
        return self.observer.machine_state()

    def job(self, phase: JobPhase, **params: object):
        self.job_number += 1
        job = BridgeJob(f"job-{self.job_number}", f"idem-{self.job_number}", "cell-a", phase, MachineState.IDLE, {k: str(v) for k, v in params.items()})
        dispatch = self.coordinator.dispatch(job, self.cell, TRANSFORM, self.observer.machine_state())
        if not dispatch.accepted:
            return dispatch, None
        self.header += 1
        result = self.publisher.publish(self.agv, TARGET, self.header, "2026-01-01T00:00:00Z", dispatch)
        if result.published and dispatch.action != "CANCEL_ORDER":
            self.published_orders += 1
        self.report()
        return dispatch, result


class ShiftTests(unittest.TestCase):
    def test_a_shift_of_move_load_move_unload_runs_in_order_and_ends_idle(self) -> None:
        shift = Shift()
        finished: list[str] = []
        for phase, params in ((JobPhase.PROCESS, {"x": 1.0, "y": 1.0}), (JobPhase.LOAD, {}), (JobPhase.PROCESS, {"x": 4.0, "y": 2.0}), (JobPhase.UNLOAD, {})):
            dispatch, result = shift.job(phase, **params)
            self.assertTrue(dispatch.accepted, (phase, dispatch.reason))
            self.assertTrue(result.published, (phase, result.reason))
            self.assertEqual(shift.observer.machine_state(), MachineState.RUNNING)
            self.assertEqual(shift.settle(), MachineState.IDLE)
            finished += [a["actionType"] for a in shift.agv.action_states if a["actionStatus"] == "FINISHED"]
        self.assertEqual(shift.agv.validation_errors, [])
        self.assertEqual(shift.published_orders, 4)
        self.assertEqual(finished, ["pick", "drop"])

    def test_a_job_while_the_vehicle_is_still_working_is_refused_and_nothing_is_published(self) -> None:
        shift = Shift()
        shift.job(JobPhase.PROCESS, x=1.0, y=1.0)
        before = len(shift.agv.received)
        dispatch, result = shift.job(JobPhase.PROCESS, x=9.0, y=9.0)
        self.assertFalse(dispatch.accepted)
        self.assertIsNone(result)
        self.assertEqual(len(shift.agv.received), before)

    def test_a_field_violation_in_the_middle_of_a_drive_stops_it_and_the_vehicle_resumes_when_it_clears(self) -> None:
        shift = Shift()
        shift.job(JobPhase.PROCESS, x=2.0, y=2.0)
        shift.step()  # driving
        shift.agv.set_field_violation(True)
        shift.report()
        self.assertEqual(shift.observer.machine_state(), MachineState.FAULT)
        shift.step(5)
        self.assertEqual(shift.observer.machine_state(), MachineState.FAULT)  # no progress while the field is violated
        self.assertFalse(shift.job(JobPhase.PROCESS, x=3.0, y=3.0)[0].accepted)
        shift.agv.set_field_violation(False)
        self.assertEqual(shift.settle(), MachineState.IDLE)
        self.assertTrue(shift.job(JobPhase.PROCESS, x=3.0, y=3.0)[0].accepted)

    def test_an_emergency_stop_reads_safe_stop_and_the_cancel_still_goes_out(self) -> None:
        shift = Shift()
        shift.job(JobPhase.LOAD)
        shift.step()
        shift.agv.set_estop("MANUAL")
        shift.report()
        self.assertEqual(shift.observer.machine_state(), MachineState.SAFE_STOP)
        self.assertFalse(shift.job(JobPhase.PROCESS, x=1.0, y=1.0)[0].accepted)
        dispatch, result = shift.job(JobPhase.ABORT)
        self.assertTrue(dispatch.accepted and result.published, getattr(result, "reason", dispatch.reason))
        shift.agv.clear_estop()
        shift.report()
        self.assertEqual(shift.settle(), MachineState.IDLE)
        self.assertTrue(shift.job(JobPhase.PROCESS, x=1.0, y=1.0)[0].accepted)

    def test_an_abort_in_the_middle_of_an_order_drops_it_and_the_next_order_is_taken(self) -> None:
        shift = Shift()
        shift.job(JobPhase.LOAD)
        shift.step()
        _, result = shift.job(JobPhase.ABORT)
        self.assertTrue(result.published)
        self.assertEqual(shift.agv.order_id, "")
        shift.report()
        self.assertEqual(shift.settle(), MachineState.IDLE)
        self.assertTrue(shift.job(JobPhase.UNLOAD)[1].published)

    def test_a_vehicle_that_goes_silent_reads_offline_and_nothing_is_sent_to_it(self) -> None:
        shift = Shift()
        shift.clock[0] += 1000.0  # no state for far longer than the freshness limit
        self.assertEqual(shift.observer.machine_state(), MachineState.OFFLINE)
        before = len(shift.agv.received)
        self.assertFalse(shift.job(JobPhase.PROCESS, x=1.0, y=1.0)[0].accepted)
        self.assertEqual(len(shift.agv.received), before)
        shift.report()
        self.assertEqual(shift.observer.machine_state(), MachineState.IDLE)

    def test_a_connection_that_drops_or_a_manual_mode_blocks_new_orders(self) -> None:
        shift = Shift()
        shift.report(connection="CONNECTIONBROKEN")
        self.assertEqual(shift.observer.machine_state(), MachineState.OFFLINE)
        shift.report(connection="ONLINE")
        shift.agv.set_operating_mode("MANUAL")
        shift.report()
        self.assertEqual(shift.observer.machine_state(), MachineState.OFFLINE)
        self.assertFalse(shift.job(JobPhase.PROCESS, x=1.0, y=1.0)[0].accepted)
        shift.agv.set_operating_mode("AUTOMATIC")
        shift.report()
        self.assertTrue(shift.job(JobPhase.PROCESS, x=1.0, y=1.0)[0].accepted)

    def test_a_cell_that_is_not_ready_blocks_orders_even_with_an_idle_vehicle(self) -> None:
        shift = Shift()
        shift.cell = CellState.SAFE_STOP
        self.assertFalse(shift.job(JobPhase.PROCESS, x=1.0, y=1.0)[0].accepted)
        self.assertEqual(shift.agv.received, [])

    def test_the_header_ids_never_go_backwards_over_a_long_shift(self) -> None:
        shift = Shift()
        for index in range(6):
            dispatch, result = shift.job(JobPhase.PROCESS, x=float(index), y=0.0)
            self.assertTrue(result.published, (index, getattr(result, "reason", "")))
            shift.settle()
        ids = [body["headerId"] for _, body in shift.agv.received]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(shift.agv.validation_errors, [])


if __name__ == "__main__":
    unittest.main()
