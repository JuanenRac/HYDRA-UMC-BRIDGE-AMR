# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - The plant graph and the multi-node orders planned on it
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A plant graph (nodes with a position, directed edges, optional zones) and the shortest route on it, as the nodes and edges of a VDA 5050 order.

Without a graph a job says "go to x,y" and the order has one node. With a graph file (`HYDRA_UMC_AMR_PLANT_GRAPH`, or the `graph` argument of the job entry) a job may
say `goal_node` instead: the route from the node the vehicle last reported (`lastNodeId`) to that node is planned here and sent as ONE order with every node and edge of
the route, so the vehicle keeps the plant's lanes instead of driving a straight line. The graph is a JSON file, so every number lives in the file and not in the code:

    {"map_id": "site-a",
     "nodes": {"A": {"x": 0.0, "y": 0.0}, "B": {"x": 5.0, "y": 0.0, "zone": "press-1"}},
     "edges": [{"from": "A", "to": "B", "bidirectional": true}]}

Positions are in the factory frame (like the x and y of a job) and are converted with the same `FrameTransform` as a job; `map_id` is optional (the transform's map is what the order carries). `zone` is optional and is what the zone
interlock (`zone_interlock.py`) looks at. The graph is checked when it is loaded: finite numbers, known node ids on every edge, and a cap on its size. Nothing here has
run against a real AGV or fleet manager.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

GRAPH_ENVIRONMENT = "HYDRA_UMC_AMR_PLANT_GRAPH"
MAX_NODES = 5000
MAX_EDGES = 20000
MAX_ROUTE_NODES = 500
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class PlantGraphError(ValueError):
    """The graph file is not usable."""


class NoRouteError(ValueError):
    """There is no route between the two nodes."""


@dataclass(frozen=True)
class PlantNode:
    node_id: str
    x: float
    y: float
    zone: str | None = None


def _finite(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlantGraphError(f"{what} must be a finite number")
    return float(value)


class PlantGraph:
    """Nodes, directed edges and the shortest route (Dijkstra on the length of the edges)."""

    def __init__(self, map_id: str, nodes: dict[str, PlantNode], edges: list[tuple[str, str, float]]) -> None:
        if not isinstance(map_id, str):
            raise PlantGraphError("map_id must be a string")
        if not nodes:
            raise PlantGraphError("the graph has no nodes")
        if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
            raise PlantGraphError("the graph is larger than the limits")
        self.map_id = map_id
        self.nodes = nodes
        self._out: dict[str, list[tuple[str, float]]] = {key: [] for key in nodes}
        for start, end, length in edges:
            if start not in nodes or end not in nodes:
                raise PlantGraphError(f"edge {start}->{end} names a node that does not exist")
            if length < 0:
                raise PlantGraphError("an edge length cannot be negative")
            self._out[start].append((end, length))

    @classmethod
    def from_dict(cls, data: object) -> "PlantGraph":
        if not isinstance(data, dict):
            raise PlantGraphError("the graph must be a JSON object")
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, dict):
            raise PlantGraphError("nodes must be an object keyed by node id")
        nodes: dict[str, PlantNode] = {}
        for key, value in raw_nodes.items():
            if not isinstance(key, str) or not _ID.match(key):
                raise PlantGraphError(f"node id {key!r} is not valid")
            if not isinstance(value, dict):
                raise PlantGraphError(f"node {key} must be an object")
            zone = value.get("zone")
            if zone is not None and (not isinstance(zone, str) or not _ID.match(zone)):
                raise PlantGraphError(f"zone of node {key} is not valid")
            nodes[key] = PlantNode(key, _finite(value.get("x"), f"x of node {key}"), _finite(value.get("y"), f"y of node {key}"), zone)
        raw_edges = data.get("edges", [])
        if not isinstance(raw_edges, list):
            raise PlantGraphError("edges must be a list")
        edges: list[tuple[str, str, float]] = []
        for item in raw_edges:
            if not isinstance(item, dict) or not isinstance(item.get("from"), str) or not isinstance(item.get("to"), str):
                raise PlantGraphError("every edge needs from and to")
            start, end = item["from"], item["to"]
            if start not in nodes or end not in nodes:
                raise PlantGraphError(f"edge {start}->{end} names a node that does not exist")
            if "length" in item:
                length = _finite(item["length"], f"length of edge {start}->{end}")
            else:
                length = math.hypot(nodes[end].x - nodes[start].x, nodes[end].y - nodes[start].y)
            edges.append((start, end, length))
            if item.get("bidirectional") is True:
                edges.append((end, start, length))
        map_id = data.get("map_id")
        return cls(map_id if isinstance(map_id, str) else "", nodes, edges)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "PlantGraph":
        file = Path(path)
        if file.stat().st_size > 8 * 1024 * 1024:
            raise PlantGraphError("the graph file is larger than 8 MiB")
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise PlantGraphError(f"the graph file cannot be read: {error}") from error
        return cls.from_dict(data)

    @classmethod
    def from_environment(cls) -> "PlantGraph | None":
        """The graph named by `HYDRA_UMC_AMR_PLANT_GRAPH`, or None when it is not set (no graph: jobs keep going by x and y)."""
        path = os.environ.get(GRAPH_ENVIRONMENT, "").strip()
        return cls.load(path) if path else None

    def zone_of(self, node_id: str | None) -> str | None:
        node = self.nodes.get(node_id) if node_id else None
        return node.zone if node else None

    def route(self, start: str, goal: str) -> list[str]:
        """The shortest list of node ids from `start` to `goal`, both included. Raises NoRouteError."""
        if start not in self.nodes:
            raise NoRouteError(f"the start node {start!r} is not in the plant graph")
        if goal not in self.nodes:
            raise NoRouteError(f"the goal node {goal!r} is not in the plant graph")
        if start == goal:
            return [start]
        best: dict[str, float] = {start: 0.0}
        came: dict[str, str] = {}
        queue: list[tuple[float, str]] = [(0.0, start)]
        while queue:
            cost, here = heapq.heappop(queue)
            if here == goal:
                break
            if cost > best.get(here, math.inf):
                continue
            for there, length in self._out[here]:
                total = cost + length
                if total < best.get(there, math.inf):
                    best[there] = total
                    came[there] = here
                    heapq.heappush(queue, (total, there))
        if goal not in came:
            raise NoRouteError(f"no route from {start} to {goal}")
        path = [goal]
        while path[-1] != start:
            path.append(came[path[-1]])
        path.reverse()
        if len(path) > MAX_ROUTE_NODES:
            raise NoRouteError("the route is longer than the limit of nodes of one order")
        return path

    def order_parts(self, route: list[str], to_local, map_id: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """The `nodes` and `edges` of a VDA 5050 order for a route (node sequence ids 0, 2, 4..., edge ids 1, 3, 5...).

        `to_local(x, y)` converts a factory position to the vehicle's map (`FrameTransform.to_local`); `map_id` is the map of the vehicle (`FrameTransform.map_id`).
        """
        nodes: list[dict[str, object]] = []
        edges: list[dict[str, object]] = []
        for index, node_id in enumerate(route):
            node = self.nodes[node_id]
            local_x, local_y = to_local(node.x, node.y)
            nodes.append(
                {
                    "nodeId": node_id,
                    "sequenceId": index * 2,
                    "released": True,
                    "nodePosition": {"x": local_x, "y": local_y, "mapId": map_id},
                    "actions": [],
                }
            )
            if index:
                edges.append(
                    {
                        "edgeId": f"{route[index - 1]}->{node_id}",
                        "sequenceId": index * 2 - 1,
                        "released": True,
                        "startNodeId": route[index - 1],
                        "endNodeId": node_id,
                        "actions": [],
                    }
                )
        return nodes, edges
