# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - The zone interlock acts on silence
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A timer, not only the arrival of a message, makes the interlock look at the vehicle; a silent vehicle is never resumed."""

import json
import threading
import unittest

from hydra_umc_bridge_amr import AmrCoordinator, PlantGraph, Vda5050Observer, ZoneInterlock
from hydra_umc_sdk.bridge_contract import CellState

from test_route_telemetry_interlock import GRAPH, TARGET, ZONE_TOPIC, Client, agv_state, occupied, publisher


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds


def build(clock: Clock, **options) -> tuple[ZoneInterlock, Vda5050Observer]:
    observer = Vda5050Observer(TARGET, max_age_seconds=10.0, clock=clock)
    observer.on_message(observer.connection_topic, json.dumps({"manufacturer": "hydra-umc", "serialNumber": "amr-1", "connectionState": "ONLINE"}).encode())
    observer.on_message(observer.state_topic, agv_state(last="A", next_nodes=("B",)))
    zones = ZoneInterlock(
        PlantGraph.from_dict(GRAPH),
        observer,
        AmrCoordinator(),
        publisher(),
        TARGET,
        lambda: CellState.READY,
        enabled=True,
        topic=ZONE_TOPIC,
        wall_clock_ms=lambda: 1000.0 + clock.seconds * 1000.0,
        **options,
    )
    return zones, observer


class SilenceTests(unittest.TestCase):
    def test_an_occupancy_that_ages_out_pauses_without_any_message(self) -> None:
        clock = Clock()
        zones, _observer = build(clock)
        client = Client()
        zones.on_message(ZONE_TOPIC, occupied([], 1000))
        self.assertIsNone(zones.step(client))
        clock.seconds = 6.0  # past the maximum age of the occupancy
        self.assertEqual(zones.step(client), "pause")

    def test_a_silent_vehicle_with_a_blocked_zone_ahead_is_paused(self) -> None:
        clock = Clock()
        zones, observer = build(clock)
        client = Client()
        clock.seconds = 11.0  # the state is older than the observer accepts
        self.assertIsNone(observer.snapshot())
        zones.on_message(ZONE_TOPIC, occupied(["press-1"], 12000))
        self.assertEqual(zones.step(client), "pause")
        self.assertEqual(client.on("/instantActions")[0]["actions"][0]["actionType"], "startPause")

    def test_an_offline_vehicle_is_paused_and_never_resumed(self) -> None:
        clock = Clock()
        zones, observer = build(clock)
        client = Client()
        observer.on_message(observer.connection_topic, json.dumps({"manufacturer": "hydra-umc", "serialNumber": "amr-1", "connectionState": "OFFLINE"}).encode())
        zones.on_message(ZONE_TOPIC, occupied(["press-1"], 1000))
        self.assertEqual(zones.step(client), "pause")
        clock.seconds = 3.0
        zones.on_message(ZONE_TOPIC, occupied([], 4000))
        self.assertIsNone(zones.step(client))
        self.assertEqual(len(client.sent), 1)

    def test_the_old_behaviour_is_one_switch_away(self) -> None:
        clock = Clock()
        zones, _observer = build(clock, silence_pause=False)
        client = Client()
        clock.seconds = 11.0
        self.assertIsNone(zones.step(client))
        self.assertEqual(client.sent, [])

    def test_the_timer_looks_at_the_vehicle_and_can_be_stopped(self) -> None:
        sent = threading.Event()

        class Signalling(Client):
            def publish(self, topic: str, payload: str) -> object:
                sent.set()
                return super().publish(topic, payload)

        zones, _observer = build(Clock(), tick_seconds=0.01)
        zones.start(Signalling())
        try:
            self.assertTrue(sent.wait(5.0))  # nothing was heard about the zones: UNKNOWN, so a pause
        finally:
            zones.stop()

    def test_a_tick_of_zero_starts_no_timer_and_a_negative_one_is_refused(self) -> None:
        zones, _observer = build(Clock(), tick_seconds=0)
        zones.start(Client())
        self.assertIsNone(zones._ticker)
        with self.assertRaises(ValueError):
            build(Clock(), tick_seconds=-1)


if __name__ == "__main__":
    unittest.main()
