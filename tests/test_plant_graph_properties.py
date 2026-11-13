# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - tests/test_plant_graph_properties.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The plant graph against its own rules: every refusal of a malformed graph, and the shortest route checked against an independent all-pairs computation on
random graphs drawn from a fixed seed. No vehicle and no fleet manager: only the graph."""

import json
import math
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hydra_umc_bridge_amr.plant_graph import (
    GRAPH_ENVIRONMENT,
    MAX_EDGES,
    MAX_NODES,
    MAX_ROUTE_NODES,
    NoRouteError,
    PlantGraph,
    PlantGraphError,
    PlantNode,
)

LINE = {"map_id": "m", "nodes": {"A": {"x": 0, "y": 0}, "B": {"x": 3, "y": 4}, "C": {"x": 3, "y": 8, "zone": "press-1"}}, "edges": [{"from": "A", "to": "B"}, {"from": "B", "to": "C", "bidirectional": True}]}


def build(**changes):
    data = json.loads(json.dumps(LINE))
    data.update(changes)
    return data


class Refusals(unittest.TestCase):
    def refuses(self, data, text):
        with self.assertRaisesRegex(PlantGraphError, text):
            PlantGraph.from_dict(data)

    def test_the_shapes_that_are_not_a_graph(self):
        for data in (None, [], "graph", 7):
            self.refuses(data, "JSON object")
        self.refuses({}, "nodes must be an object")
        self.refuses({"nodes": []}, "nodes must be an object")
        self.refuses({"nodes": {}}, "no nodes")

    def test_node_ids_and_bodies(self):
        for bad in ("", "a b", "x" * 65, "ñ", "a/b", "a;b"):
            self.refuses({"nodes": {bad: {"x": 0, "y": 0}}}, "not valid")
        self.refuses({"nodes": {"A": 5}}, "must be an object")
        self.refuses({"nodes": {"A": {"x": 0, "y": 0, "zone": 5}}}, "zone of node A")
        self.refuses({"nodes": {"A": {"x": 0, "y": 0, "zone": "bad zone"}}}, "zone of node A")

    def test_positions_are_finite_real_numbers(self):
        for bad in (None, "1", True, math.nan, math.inf, -math.inf, [1]):
            self.refuses({"nodes": {"A": {"x": bad, "y": 0}}}, "x of node A")
            self.refuses({"nodes": {"A": {"x": 0, "y": bad}}}, "y of node A")
        self.refuses({"nodes": {"A": {"y": 0}}}, "x of node A")

    def test_edges(self):
        base = {"A": {"x": 0, "y": 0}, "B": {"x": 1, "y": 0}}
        self.refuses({"nodes": base, "edges": {}}, "edges must be a list")
        for edge in (5, {}, {"from": "A"}, {"to": "B"}, {"from": 1, "to": "B"}, {"from": "A", "to": None}):
            self.refuses({"nodes": base, "edges": [edge]}, "from and to")
        self.refuses({"nodes": base, "edges": [{"from": "A", "to": "Z"}]}, "does not exist")
        self.refuses({"nodes": base, "edges": [{"from": "Z", "to": "A"}]}, "does not exist")
        for length in ("3", True, math.nan, math.inf, None):
            self.refuses({"nodes": base, "edges": [{"from": "A", "to": "B", "length": length}]}, "length of edge")

    def test_a_negative_length_is_refused_by_the_constructor(self):
        with self.assertRaisesRegex(PlantGraphError, "negative"):
            PlantGraph("m", {"A": PlantNode("A", 0, 0), "B": PlantNode("B", 1, 0)}, [("A", "B", -1.0)])

    def test_the_constructor_checks_what_from_dict_cannot_reach(self):
        node = {"A": PlantNode("A", 0, 0)}
        with self.assertRaisesRegex(PlantGraphError, "map_id must be a string"):
            PlantGraph(None, node, [])  # type: ignore[arg-type]
        with self.assertRaisesRegex(PlantGraphError, "does not exist"):
            PlantGraph("m", node, [("A", "Q", 1.0)])
        with self.assertRaisesRegex(PlantGraphError, "larger than the limits"):
            PlantGraph("m", {f"N{i}": PlantNode(f"N{i}", 0, 0) for i in range(MAX_NODES + 1)}, [])
        with self.assertRaisesRegex(PlantGraphError, "larger than the limits"):
            PlantGraph("m", node, [("A", "A", 1.0)] * (MAX_EDGES + 1))

    def test_a_missing_or_non_string_map_id_becomes_empty(self):
        self.assertEqual(PlantGraph.from_dict(build(map_id=None)).map_id, "")
        self.assertEqual(PlantGraph.from_dict(build(map_id=7)).map_id, "")
        self.assertEqual(PlantGraph.from_dict(build()).map_id, "m")


class Loading(unittest.TestCase):
    def test_a_file_round_trip_and_its_refusals(self):
        with tempfile.TemporaryDirectory() as folder:
            good = Path(folder) / "graph.json"
            good.write_text(json.dumps(LINE), encoding="utf-8")
            self.assertEqual(sorted(PlantGraph.load(good).nodes), ["A", "B", "C"])
            broken = Path(folder) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(PlantGraphError, "cannot be read"):
                PlantGraph.load(broken)
            binary = Path(folder) / "binary.json"
            binary.write_bytes(b"\xff\xfe\x00")
            with self.assertRaisesRegex(PlantGraphError, "cannot be read"):
                PlantGraph.load(binary)
            big = Path(folder) / "big.json"
            with big.open("wb") as handle:
                handle.truncate(8 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(PlantGraphError, "larger than 8 MiB"):
                PlantGraph.load(big)

    def test_the_environment_names_the_graph_and_nothing_means_no_graph(self):
        with tempfile.TemporaryDirectory() as folder:
            good = Path(folder) / "graph.json"
            good.write_text(json.dumps(LINE), encoding="utf-8")
            with mock.patch.dict(os.environ, {GRAPH_ENVIRONMENT: f"  {good}  "}):
                self.assertIsNotNone(PlantGraph.from_environment())
            for empty in ("", "   "):
                with mock.patch.dict(os.environ, {GRAPH_ENVIRONMENT: empty}):
                    self.assertIsNone(PlantGraph.from_environment())
            with mock.patch.dict(os.environ, {GRAPH_ENVIRONMENT: str(Path(folder) / "missing.json")}):
                with self.assertRaises(OSError):
                    PlantGraph.from_environment()


class Planner(unittest.TestCase):
    def test_a_planner_error_object_is_refused_with_its_reason(self):
        with self.assertRaisesRegex(PlantGraphError, "blocked by a wall"):
            PlantGraph.from_path_planner({"status": "failed", "reason": "blocked by a wall"})
        with self.assertRaisesRegex(PlantGraphError, "timeout"):
            PlantGraph.from_path_planner({"status": "timeout"})

    def test_the_path_needs_two_waypoints_that_are_objects(self):
        for data in (None, [], [{"x": 0, "y": 0}], {"path": None}, {"status": "ok"}, "path"):
            with self.assertRaisesRegex(PlantGraphError, "at least two"):
                PlantGraph.from_path_planner(data)
        with self.assertRaisesRegex(PlantGraphError, "waypoint 1 must be an object"):
            PlantGraph.from_path_planner([{"x": 0, "y": 0}, 5])
        with self.assertRaisesRegex(PlantGraphError, "x of node P1"):
            PlantGraph.from_path_planner([{"x": 0, "y": 0}, {"x": math.nan, "y": 0}])

    def test_the_prefix_is_checked(self):
        for prefix in ("a b", 5, "x" * 64, "ñ"):
            with self.assertRaisesRegex(PlantGraphError, "prefix"):
                PlantGraph.from_path_planner([{"x": 0, "y": 0}, {"x": 1, "y": 0}], prefix=prefix)

    def test_a_path_becomes_a_chain_and_the_height_is_ignored(self):
        path = [{"x": 0, "y": 0, "z": 9}, {"x": 3, "y": 4, "z": -2}, {"x": 3, "y": 8}]
        both = PlantGraph.from_path_planner({"status": "ok", "path": path}, map_id="site", prefix="W")
        self.assertEqual((both.map_id, sorted(both.nodes)), ("site", ["W0", "W1", "W2"]))
        self.assertEqual(both.route("W0", "W2"), ["W0", "W1", "W2"])
        self.assertEqual(both.route("W2", "W0"), ["W2", "W1", "W0"])
        one_way = PlantGraph.from_path_planner(path, bidirectional=False)
        self.assertEqual(one_way.route("P0", "P2"), ["P0", "P1", "P2"])
        with self.assertRaises(NoRouteError):
            one_way.route("P2", "P0")


class Routes(unittest.TestCase):
    def test_the_obvious_cases(self):
        graph = PlantGraph.from_dict(LINE)
        self.assertEqual(graph.route("A", "C"), ["A", "B", "C"])
        self.assertEqual(graph.route("A", "A"), ["A"])
        self.assertEqual(graph.route("C", "B"), ["C", "B"])  # the bidirectional edge
        with self.assertRaisesRegex(NoRouteError, "no route from C to A"):
            graph.route("C", "A")  # A->B is one way
        with self.assertRaisesRegex(NoRouteError, "start node 'Z'"):
            graph.route("Z", "A")
        with self.assertRaisesRegex(NoRouteError, "goal node 'Z'"):
            graph.route("A", "Z")

    def test_an_explicit_length_beats_the_distance_between_the_nodes(self):
        data = {
            "nodes": {"A": {"x": 0, "y": 0}, "B": {"x": 1, "y": 0}, "C": {"x": 2, "y": 0}},
            "edges": [{"from": "A", "to": "C", "length": 100}, {"from": "A", "to": "B"}, {"from": "B", "to": "C", "length": 5}],
        }
        self.assertEqual(PlantGraph.from_dict(data).route("A", "C"), ["A", "B", "C"])

    def test_zones(self):
        graph = PlantGraph.from_dict(LINE)
        self.assertEqual((graph.zone_of("C"), graph.zone_of("A")), ("press-1", None))
        self.assertIsNone(graph.zone_of("unknown"))
        self.assertIsNone(graph.zone_of(None))
        self.assertIsNone(graph.zone_of(""))

    def test_a_route_longer_than_one_order_may_carry_is_refused(self):
        count = MAX_ROUTE_NODES + 1
        nodes = {f"N{i}": {"x": i, "y": 0} for i in range(count)}
        edges = [{"from": f"N{i}", "to": f"N{i + 1}"} for i in range(count - 1)]
        graph = PlantGraph.from_dict({"nodes": nodes, "edges": edges})
        self.assertEqual(len(graph.route("N0", f"N{MAX_ROUTE_NODES - 1}")), MAX_ROUTE_NODES)
        with self.assertRaisesRegex(NoRouteError, "longer than the limit"):
            graph.route("N0", f"N{MAX_ROUTE_NODES}")

    def test_zero_length_edges_and_self_loops_do_not_hang_or_break_the_route(self):
        data = {"nodes": {"A": {"x": 0, "y": 0}, "B": {"x": 0, "y": 0}}, "edges": [{"from": "A", "to": "A"}, {"from": "A", "to": "B"}, {"from": "B", "to": "A"}]}
        self.assertEqual(PlantGraph.from_dict(data).route("A", "B"), ["A", "B"])

    def test_the_route_agrees_with_an_all_pairs_computation_on_random_graphs(self):
        rng = random.Random(20260101)
        for case in range(60):
            size = rng.randint(2, 12)
            nodes = {f"N{i}": {"x": rng.uniform(0, 50), "y": rng.uniform(0, 50)} for i in range(size)}
            edges = []
            for _ in range(rng.randint(1, size * 3)):
                a, b = rng.sample(range(size), 2)
                edge = {"from": f"N{a}", "to": f"N{b}", "bidirectional": rng.random() < 0.4}
                if rng.random() < 0.5:
                    edge["length"] = round(rng.uniform(0.5, 40), 3)
                edges.append(edge)
            graph = PlantGraph.from_dict({"nodes": nodes, "edges": edges})
            lengths = [[math.inf] * size for _ in range(size)]
            for i in range(size):
                lengths[i][i] = 0.0
            for item in edges:
                a, b = int(item["from"][1:]), int(item["to"][1:])
                length = item["length"] if "length" in item else math.hypot(nodes[item["to"]]["x"] - nodes[item["from"]]["x"], nodes[item["to"]]["y"] - nodes[item["from"]]["y"])
                lengths[a][b] = min(lengths[a][b], length)
                if item["bidirectional"]:
                    lengths[b][a] = min(lengths[b][a], length)
            for k in range(size):
                for i in range(size):
                    for j in range(size):
                        lengths[i][j] = min(lengths[i][j], lengths[i][k] + lengths[k][j])
            for i in range(size):
                for j in range(size):
                    start, goal = f"N{i}", f"N{j}"
                    if math.isinf(lengths[i][j]):
                        with self.assertRaises(NoRouteError, msg=f"case {case} {start}->{goal}"):
                            graph.route(start, goal)
                        continue
                    route = graph.route(start, goal)
                    self.assertEqual((route[0], route[-1]), (start, goal))
                    self.assertEqual(len(route), len(set(route)), "a shortest route never visits a node twice")
                    steps = [graph._out[a] for a in route[:-1]]
                    total = sum(min(length for there, length in out if there == b) for out, b in zip(steps, route[1:], strict=True))
                    self.assertAlmostEqual(total, lengths[i][j], places=6, msg=f"case {case} {start}->{goal}")


class OrderParts(unittest.TestCase):
    def test_sequence_ids_and_edge_ids_follow_the_vda_5050_pattern(self):
        graph = PlantGraph.from_dict(LINE)
        nodes, edges = graph.order_parts(["A", "B", "C"], lambda x, y: (x + 10, y - 1), "vehicle-map")
        self.assertEqual([n["sequenceId"] for n in nodes], [0, 2, 4])
        self.assertEqual([e["sequenceId"] for e in edges], [1, 3])
        self.assertEqual([e["edgeId"] for e in edges], ["A->B", "B->C"])
        self.assertEqual(nodes[1]["nodePosition"], {"x": 13, "y": 3, "mapId": "vehicle-map"})
        self.assertTrue(all(item["released"] is True and item["actions"] == [] for item in nodes + edges))
        self.assertEqual((edges[0]["startNodeId"], edges[0]["endNodeId"]), ("A", "B"))

    def test_a_single_node_route_has_no_edge(self):
        nodes, edges = PlantGraph.from_dict(LINE).order_parts(["A"], lambda x, y: (x, y), "m")
        self.assertEqual((len(nodes), edges), (1, []))


if __name__ == "__main__":
    unittest.main()
