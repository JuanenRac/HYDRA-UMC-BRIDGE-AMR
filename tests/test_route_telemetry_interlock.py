# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Route planning, state report and zone interlock tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The plant graph and its routes, the multi-node order, the state report for the data lake and the OPC UA table, and the zone interlock (all against fake clients)."""

import json
import os
import tempfile
import unittest

from hydra_umc_bridge_amr import (
    AmrCoordinator,
    AmrJobEntry,
    FrameTransform,
    NoRouteError,
    PlantGraph,
    PlantGraphError,
    StateReporter,
    Vda5050Observer,
    Vda5050Publisher,
    Vda5050Target,
    ZoneInterlock,
)
from hydra_umc_bridge_amr.job_entry import JOB_TOPIC
from hydra_umc_bridge_amr.state_report import OPCUA_TOPIC, STATE_TOPIC, opcua_nodes, state_record
from hydra_umc_sdk.bridge_contract import CellState, MachineState
from hydra_umc_sdk.global_stop import DEFAULT_TOPIC, GlobalStopGuard, GlobalStopSettings

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")
TRANSFORM = FrameTransform(0.0, 0.0, 0.0, "site-a")
GRAPH = {
    "map_id": "site-a",
    "nodes": {
        "A": {"x": 0, "y": 0},
        "B": {"x": 5, "y": 0, "zone": "press-1"},
        "C": {"x": 10, "y": 0},
        "D": {"x": 5, "y": 5},
    },
    "edges": [
        {"from": "A", "to": "B", "bidirectional": True},
        {"from": "B", "to": "C", "bidirectional": True},
        {"from": "A", "to": "D", "length": 1.0},
        {"from": "D", "to": "C", "length": 1.0},
    ],
}


class Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def publish(self, topic: str, payload: str) -> object:
        self.sent.append((topic, payload))
        return type("Info", (), {"rc": 0})()

    def on(self, suffix: str) -> list[dict]:
        return [json.loads(p) for t, p in self.sent if t.endswith(suffix)]


def publisher() -> Vda5050Publisher:
    return Vda5050Publisher(global_stop=GlobalStopGuard(GlobalStopSettings(False, DEFAULT_TOPIC, False)))


def agv_state(last="A", next_nodes=("B",), paused=False, driving=True, charge=80.0, errors=None) -> bytes:
    body = {
        "manufacturer": "hydra-umc",
        "serialNumber": "amr-1",
        "lastNodeId": last,
        "paused": paused,
        "driving": driving,
        "operatingMode": "AUTOMATIC",
        "nodeStates": [{"nodeId": n, "sequenceId": 2 + 2 * i, "released": True} for i, n in enumerate(next_nodes)],
        "actionStates": [],
        "batteryState": {"batteryCharge": charge, "charging": False},
        "agvPosition": {"x": 1.5, "y": 0.25, "theta": 0.1, "positionInitialized": True, "mapId": "site-a"},
        "errors": errors or [],
        "safetyState": {"eStop": "NONE", "fieldViolation": False},
    }
    return json.dumps(body).encode()


def observer_with(payload: bytes) -> Vda5050Observer:
    observer = Vda5050Observer(TARGET)
    observer.on_message(observer.connection_topic, json.dumps({"manufacturer": "hydra-umc", "serialNumber": "amr-1", "connectionState": "ONLINE"}).encode())
    observer.on_message(observer.state_topic, payload)
    return observer


class GraphTests(unittest.TestCase):
    def test_the_shortest_route_follows_the_lengths(self) -> None:
        graph = PlantGraph.from_dict(GRAPH)
        self.assertEqual(graph.route("A", "C"), ["A", "D", "C"])
        self.assertEqual(graph.route("C", "A"), ["C", "B", "A"])  # D->A does not exist
        self.assertEqual(graph.route("A", "A"), ["A"])

    def test_no_route_and_unknown_nodes_are_refused(self) -> None:
        graph = PlantGraph.from_dict(GRAPH)
        with self.assertRaises(NoRouteError):
            graph.route("A", "Z")
        with self.assertRaises(NoRouteError):
            PlantGraph.from_dict({"nodes": {"A": {"x": 0, "y": 0}, "B": {"x": 1, "y": 0}}, "edges": []}).route("A", "B")

    def test_a_bad_graph_is_refused_when_loaded(self) -> None:
        for bad in (
            [],
            {"nodes": {}},
            {"nodes": {"A": {"x": float("nan"), "y": 0}}},
            {"nodes": {"A": {"x": 0, "y": 0}}, "edges": [{"from": "A", "to": "Q"}]},
            {"nodes": {"bad id": {"x": 0, "y": 0}}},
            {"nodes": {"A": {"x": True, "y": 0}}},
        ):
            with self.assertRaises(PlantGraphError):
                PlantGraph.from_dict(bad)

    def test_the_graph_loads_from_the_file_named_by_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "graph.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(GRAPH, handle)
            old = os.environ.get("HYDRA_UMC_AMR_PLANT_GRAPH")
            os.environ["HYDRA_UMC_AMR_PLANT_GRAPH"] = path
            try:
                self.assertEqual(PlantGraph.from_environment().route("A", "B"), ["A", "B"])
            finally:
                if old is None:
                    os.environ.pop("HYDRA_UMC_AMR_PLANT_GRAPH")
                else:
                    os.environ["HYDRA_UMC_AMR_PLANT_GRAPH"] = old


def route_job(goal="C", phase="PREPARE", key="k1") -> bytes:
    return json.dumps({"job_id": "job-1", "idempotency_key": key, "source": "orchestrator", "phase": phase, "machine_state": "IDLE", "parameters": {"goal_node": goal}}).encode()


def entry(last="A", cell=CellState.READY, machine=MachineState.IDLE, graph=True) -> AmrJobEntry:
    return AmrJobEntry(
        AmrCoordinator(), publisher(), TARGET, TRANSFORM, lambda: cell, lambda: machine, enabled=True,
        graph=PlantGraph.from_dict(GRAPH) if graph else None, last_node=lambda: last,
    )


class RouteOrderTests(unittest.TestCase):
    def test_a_goal_node_becomes_one_multi_node_order(self) -> None:
        client = Client()
        answer = entry().on_message(client, JOB_TOPIC, route_job())
        self.assertTrue(answer["allowed"], answer)
        self.assertEqual(answer["route"], ["A", "D", "C"])
        order = client.on("/order")[0]
        self.assertEqual([n["nodeId"] for n in order["nodes"]], ["A", "D", "C"])
        self.assertEqual([n["sequenceId"] for n in order["nodes"]], [0, 2, 4])
        self.assertEqual([e["sequenceId"] for e in order["edges"]], [1, 3])
        self.assertEqual(order["edges"][0]["startNodeId"], "A")
        self.assertEqual(order["nodes"][1]["nodePosition"]["mapId"], "site-a")
        self.assertEqual(order["orderId"], "job-1")

    def test_the_gate_still_decides(self) -> None:
        for kwargs in ({"cell": CellState.INHIBITED}, {"machine": MachineState.FAULT}, {"machine": None}):
            client = Client()
            answer = entry(**kwargs).on_message(client, JOB_TOPIC, route_job())
            self.assertFalse(answer["allowed"], kwargs)
            self.assertEqual(client.on("/order"), [])

    def test_an_unknown_last_node_or_goal_or_missing_graph_is_refused(self) -> None:
        for kwargs, goal in (({"last": None}, "C"), ({"last": "ZZ"}, "C"), ({}, "ZZ"), ({"graph": False}, "C")):
            client = Client()
            answer = entry(**kwargs).on_message(client, JOB_TOPIC, route_job(goal))
            self.assertFalse(answer["allowed"], kwargs)
            self.assertEqual(client.on("/order"), [])

    def test_a_global_stop_blocks_the_route(self) -> None:
        guard = GlobalStopGuard(GlobalStopSettings(True, DEFAULT_TOPIC, True))
        built = AmrJobEntry(
            AmrCoordinator(), Vda5050Publisher(global_stop=guard), TARGET, TRANSFORM, lambda: CellState.READY, lambda: MachineState.IDLE,
            enabled=True, graph=PlantGraph.from_dict(GRAPH), last_node=lambda: "A",
        )
        client = Client()
        self.assertFalse(built.on_message(client, JOB_TOPIC, route_job())["allowed"])
        self.assertEqual(client.on("/order"), [])


class StateReportTests(unittest.TestCase):
    def test_the_record_holds_only_what_the_vehicle_reported(self) -> None:
        record = state_record(json.loads(agv_state(errors=[{"errorLevel": "FATAL"}, {"errorLevel": "WARNING"}, {"errorLevel": "WARNING"}])), 1_700_000_000_000, "RUNNING")
        self.assertEqual(record["battery.charge"], 80.0)
        self.assertEqual((record["errors.count"], record["errors.fatal"], record["errors.warning"]), (3, 1, 2))
        self.assertEqual(record["position.x"], 1.5)
        self.assertNotIn("battery.voltage", record)
        self.assertEqual(state_record({}, 1, "IDLE"), {"observed_at": 1, "machine_state": "IDLE"})

    def test_the_opcua_table_is_typed_and_under_the_fleet_folder(self) -> None:
        nodes = {n["path"]: n for n in opcua_nodes(state_record(json.loads(agv_state()), 5, "RUNNING"), "amr-1")}
        self.assertEqual(nodes["Fleet/amr-1/battery/charge"], {"path": "Fleet/amr-1/battery/charge", "type": "Double", "value": 80.0})
        self.assertEqual(nodes["Fleet/amr-1/driving"]["type"], "Boolean")
        self.assertEqual(nodes["Fleet/amr-1/errors/count"]["type"], "Int64")
        self.assertEqual(nodes["Fleet/amr-1/machine_state"]["type"], "String")

    def test_off_by_default_and_rate_limited_when_on(self) -> None:
        observer = observer_with(agv_state())
        client = Client()
        self.assertFalse(StateReporter(observer, "amr-1", publish_state=False, publish_opcua=False).maybe_publish(client))
        now = [0.0]
        reporter = StateReporter(observer, "amr-1", publish_state=True, publish_opcua=True, interval_seconds=5, clock=lambda: now[0])
        self.assertTrue(reporter.maybe_publish(client))
        self.assertFalse(reporter.maybe_publish(client))
        now[0] = 6.0
        self.assertTrue(reporter.maybe_publish(client))
        self.assertEqual([t for t, _ in client.sent], [STATE_TOPIC, OPCUA_TOPIC, STATE_TOPIC, OPCUA_TOPIC])
        self.assertEqual(client.on("/state")[0]["battery.charge"], 80.0)

    def test_an_unknown_vehicle_publishes_nothing(self) -> None:
        client = Client()
        self.assertFalse(StateReporter(Vda5050Observer(TARGET), "amr-1", publish_state=True).maybe_publish(client))
        self.assertEqual(client.sent, [])
        with self.assertRaises(ValueError):
            StateReporter(Vda5050Observer(TARGET), "amr-1", interval_seconds=0.1)


ZONE_TOPIC = "hydra/safety/zones/occupied"


def interlock(observer, cell=CellState.READY, unknown="pause", enabled=True, wall=None) -> ZoneInterlock:
    return ZoneInterlock(
        PlantGraph.from_dict(GRAPH), observer, AmrCoordinator(), publisher(), TARGET, lambda: cell,
        enabled=enabled, unknown=unknown, wall_clock_ms=wall or (lambda: 1000.0),
    )


def occupied(zones, stamp=1000) -> bytes:
    return json.dumps({"occupied": zones, "observed_at": stamp}).encode()


class InterlockTests(unittest.TestCase):
    def test_off_by_default_subscribes_and_pauses_nothing(self) -> None:
        old = os.environ.pop("HYDRA_UMC_AMR_ZONE_INTERLOCK", None)
        try:
            off = ZoneInterlock(PlantGraph.from_dict(GRAPH), observer_with(agv_state()), AmrCoordinator(), publisher(), TARGET, lambda: CellState.READY)
        finally:
            if old is not None:
                os.environ["HYDRA_UMC_AMR_ZONE_INTERLOCK"] = old
        client = Client()
        self.assertEqual(off.subscriptions(), [])
        self.assertIsNone(off.step(client))
        self.assertEqual(client.sent, [])

    def test_an_occupied_zone_ahead_pauses_once_and_a_free_zone_resumes(self) -> None:
        observer = observer_with(agv_state(last="A", next_nodes=("B",)))
        zones = interlock(observer)
        client = Client()
        zones.on_message(ZONE_TOPIC, occupied(["press-1"]))
        self.assertEqual(zones.step(client), "pause")
        self.assertIsNone(zones.step(client))  # not repeated
        self.assertEqual(client.on("/instantActions")[0]["actions"][0]["actionType"], "startPause")
        # the vehicle reports itself paused and the zone is freed
        observer.on_message(observer.state_topic, agv_state(last="A", next_nodes=("B",), paused=True, driving=False))
        zones.on_message(ZONE_TOPIC, occupied([], 1001))
        self.assertEqual(zones.step(client), "resume")
        self.assertEqual(client.on("/instantActions")[1]["actions"][0]["actionType"], "stopPause")

    def test_resume_waits_for_a_ready_cell(self) -> None:
        observer = observer_with(agv_state())
        zones = interlock(observer, cell=CellState.INHIBITED)
        client = Client()
        zones.on_message(ZONE_TOPIC, occupied(["press-1"]))
        self.assertEqual(zones.step(client), "pause")
        observer.on_message(observer.state_topic, agv_state(paused=True, driving=False))
        zones.on_message(ZONE_TOPIC, occupied([], 1001))
        self.assertIsNone(zones.step(client))
        self.assertEqual(len(client.on("/instantActions")), 1)

    def test_a_vehicle_paused_by_somebody_else_is_never_resumed(self) -> None:
        observer = observer_with(agv_state(paused=True, driving=False))
        zones = interlock(observer)
        client = Client()
        zones.on_message(ZONE_TOPIC, occupied([], 1000))
        self.assertIsNone(zones.step(client))
        self.assertEqual(client.sent, [])

    def test_a_zone_that_cannot_be_read_counts_as_occupied_unless_told_otherwise(self) -> None:
        client = Client()
        self.assertEqual(interlock(observer_with(agv_state())).step(client), "pause")  # never heard
        stale = interlock(observer_with(agv_state()), wall=lambda: 100_000.0)
        stale.on_message(ZONE_TOPIC, occupied([], 1000))  # free, but 99 s old
        self.assertEqual(stale.step(Client()), "pause")
        self.assertIsNone(interlock(observer_with(agv_state()), unknown="ignore").step(Client()))

    def test_a_vehicle_already_inside_the_zone_is_not_stopped_in_the_doorway(self) -> None:
        observer = observer_with(agv_state(last="B", next_nodes=("B",)))
        zones = interlock(observer)
        zones.on_message(ZONE_TOPIC, occupied(["press-1"]))
        self.assertIsNone(zones.step(Client()))

    def test_replayed_unreadable_and_foreign_messages_change_nothing(self) -> None:
        zones = interlock(observer_with(agv_state(next_nodes=("C",))))
        zones.on_message(ZONE_TOPIC, occupied(["press-1"], 1000))
        zones.on_message(ZONE_TOPIC, occupied([], 900))  # older: a replay
        zones.on_message(ZONE_TOPIC, b"not json")
        zones.on_message(ZONE_TOPIC, json.dumps({"occupied": "press-1", "observed_at": 1000}).encode())
        zones.on_message("some/other/topic", occupied([], 1001))
        self.assertEqual(zones.occupied(), frozenset({"press-1"}))
        self.assertIsNone(zones.step(Client()))  # the next node C has no zone

    def test_a_bad_policy_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            interlock(observer_with(agv_state()), unknown="shrug")


if __name__ == "__main__":
    unittest.main()
