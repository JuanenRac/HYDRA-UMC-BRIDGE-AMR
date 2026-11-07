# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - A plant graph made from the output of the path planner
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The converter from the waypoints of the path planner to a plant graph (evidence: simulator; the planner is not run here, its documented output is)."""
from __future__ import annotations

import unittest

from hydra_umc_bridge_amr import NoRouteError, PlantGraph, PlantGraphError

PLANNER_OUTPUT = {
    "status": "ok",
    "path": [{"x": -5.0, "y": 0.0, "z": 0.0}, {"x": -1.0, "y": 3.5, "z": 0.0}, {"x": 2.0, "y": 3.5, "z": 0.0}, {"x": 5.0, "y": 0.0, "z": 0.0}],
}


class PathPlannerGraphTests(unittest.TestCase):
    def test_waypoints_become_a_chain_of_nodes(self) -> None:
        graph = PlantGraph.from_path_planner(PLANNER_OUTPUT, map_id="site")
        self.assertEqual(list(graph.nodes), ["P0", "P1", "P2", "P3"])
        self.assertEqual(graph.route("P0", "P3"), ["P0", "P1", "P2", "P3"])
        self.assertEqual(graph.route("P3", "P0"), ["P3", "P2", "P1", "P0"])
        self.assertEqual(graph.map_id, "site")

    def test_one_way_chain_cannot_be_driven_backwards(self) -> None:
        graph = PlantGraph.from_path_planner(PLANNER_OUTPUT, bidirectional=False)
        self.assertEqual(graph.route("P0", "P3")[-1], "P3")
        with self.assertRaises(NoRouteError):
            graph.route("P3", "P0")

    def test_bare_list_and_height_are_accepted_and_ignored(self) -> None:
        graph = PlantGraph.from_path_planner(PLANNER_OUTPUT["path"], prefix="W")
        self.assertEqual(list(graph.nodes), ["W0", "W1", "W2", "W3"])
        self.assertEqual((graph.nodes["W1"].x, graph.nodes["W1"].y), (-1.0, 3.5))

    def test_the_order_has_every_node_and_edge(self) -> None:
        graph = PlantGraph.from_path_planner(PLANNER_OUTPUT)
        nodes, edges = graph.order_parts(graph.route("P0", "P3"), lambda x, y: (x, y), "site")
        self.assertEqual([n["sequenceId"] for n in nodes], [0, 2, 4, 6])
        self.assertEqual([e["edgeId"] for e in edges], ["P0->P1", "P1->P2", "P2->P3"])

    def test_bad_input_is_refused(self) -> None:
        for bad in (
            {"status": "error", "reason": "no_path_found"},
            {"status": "ok", "path": [{"x": 0, "y": 0}]},
            {"status": "ok", "path": "x"},
            [{"x": 0, "y": 0}, {"x": float("nan"), "y": 1}],
            [{"x": 0, "y": 0}, {"x": 1}],
            [{"x": 0, "y": 0}, 3],
            None,
        ):
            with self.subTest(bad=bad), self.assertRaises(PlantGraphError):
                PlantGraph.from_path_planner(bad)
        with self.assertRaises(PlantGraphError):
            PlantGraph.from_path_planner(PLANNER_OUTPUT, prefix="bad prefix")


if __name__ == "__main__":
    unittest.main()
