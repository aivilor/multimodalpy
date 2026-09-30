"""The OSM topology must not depend on the requested output CRS.

Regression tests for the node-merging bug: topology nodes used to be
matched on coordinates rounded to 3 decimals *in the output CRS*. With the
default EPSG:4326 that is ~100 m, so separate intersections collapsed into
a single node; with EPSG:25830 it is 1 mm. The same area therefore
produced a different network depending on ``crs``.
"""

from __future__ import annotations

import math

import networkx as nx
import pytest
from shapely.geometry import LineString

from multimodalpy import process_osm

# Centre of Valencia; the grid spacing is ~20 m in both directions, well
# below the ~100 m that 3 decimals of a degree represent.
LON0, LAT0 = -0.3763, 39.4699
SPACING_M = 20.0
DLAT = SPACING_M / 111_320
DLON = SPACING_M / (111_320 * math.cos(math.radians(LAT0)))


def _grid_graph(size: int = 3) -> nx.MultiDiGraph:
    """Two-way street grid of ``size`` x ``size`` intersections, osmnx style."""
    graph = nx.MultiDiGraph(crs="EPSG:4326")

    def node_id(i: int, j: int) -> int:
        return 1000 + i * size + j

    for i in range(size):
        for j in range(size):
            graph.add_node(node_id(i, j), x=LON0 + j * DLON, y=LAT0 + i * DLAT)

    osmid = 1
    for i in range(size):
        for j in range(size):
            for di, dj in ((0, 1), (1, 0)):
                ni, nj = i + di, j + dj
                if ni >= size or nj >= size:
                    continue
                u, v = node_id(i, j), node_id(ni, nj)
                pu = (graph.nodes[u]["x"], graph.nodes[u]["y"])
                pv = (graph.nodes[v]["x"], graph.nodes[v]["y"])
                attrs = {
                    "osmid": osmid,
                    "highway": "residential",
                    "length": SPACING_M,
                    "oneway": False,
                }
                graph.add_edge(
                    u, v, reversed=False, geometry=LineString([pu, pv]), **attrs
                )
                graph.add_edge(
                    v, u, reversed=True, geometry=LineString([pv, pu]), **attrs
                )
                osmid += 1
    return graph


@pytest.mark.parametrize("output_crs", ["EPSG:4326", "EPSG:25830", "EPSG:3857"])
def test_close_intersections_are_not_merged(output_crs):
    nodes, _ = process_osm.build_final_osm_layers(
        _grid_graph(), layer_id="walking", mode="walk", output_crs=output_crs
    )
    assert len(nodes) == 9
    assert nodes["node_id"].is_unique


def test_topology_is_identical_for_every_output_crs():
    results = {}
    for crs in ("EPSG:4326", "EPSG:25830", "EPSG:3857"):
        nodes, edges = process_osm.build_final_osm_layers(
            _grid_graph(), layer_id="walking", mode="walk", output_crs=crs
        )
        assert nodes.crs.to_epsg() == int(crs.split(":")[1])
        assert edges.crs.to_epsg() == int(crs.split(":")[1])
        results[crs] = (
            sorted(nodes["node_id"].astype(str)),
            sorted(zip(nodes["node_id"].astype(str), nodes["node_role"], strict=True)),
            sorted(
                zip(
                    edges["from_node"].astype(str),
                    edges["to_node_id"].astype(str),
                    edges["length"].round(6),
                    strict=True,
                )
            ),
        )
    first = results["EPSG:4326"]
    for crs, result in results.items():
        assert result == first, f"topology differs for {crs}"


@pytest.mark.parametrize("output_crs", ["EPSG:4326", "EPSG:25830"])
def test_edges_only_reference_existing_nodes(output_crs):
    nodes, edges = process_osm.build_final_osm_layers(
        _grid_graph(), layer_id="walking", mode="walk", output_crs=output_crs
    )
    node_ids = set(nodes["node_id"].astype(str))
    assert set(edges["from_node"].astype(str)) <= node_ids
    assert set(edges["to_node_id"].astype(str)) <= node_ids
    # Every edge connects two different intersections.
    assert (edges["from_node"].astype(str) != edges["to_node_id"].astype(str)).all()


def test_nodes_keep_their_real_position_after_reprojection():
    """Node geometry must stay within millimetres of the OSM coordinates."""
    nodes, _ = process_osm.build_final_osm_layers(
        _grid_graph(), layer_id="walking", mode="walk", output_crs="EPSG:4326"
    )
    graph = _grid_graph()
    for _, row in nodes.iterrows():
        osm = graph.nodes[int(row["node_id"])]
        assert row.geometry.x == pytest.approx(osm["x"], abs=1e-7)
        assert row.geometry.y == pytest.approx(osm["y"], abs=1e-7)
