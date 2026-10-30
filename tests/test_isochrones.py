"""Tests for the isochrones module.

The networks are small synthetic grids whose answer is known: nodes 100 m
apart, joined in both directions by edges of 60 s and 100 m. No network
access is needed.
"""

from __future__ import annotations

import networkx as nx
import numpy as np
import pandas as pd
import pytest
from pyproj import Transformer
from shapely.geometry import Point

from multimodalpy import _matrix, read_matrix
from multimodalpy import isochrones as iso

# Lower-left corner of the grids, in EPSG:25830 (metres, Valencia).
X0, Y0 = 725_000.0, 4_372_000.0
SPACING = 100.0
TO_WGS84 = Transformer.from_crs("EPSG:25830", "EPSG:4326", always_xy=True)


def grid_matrix(size=3, *, crs="EPSG:25830", seconds=60.0, rng=None, extra_nodes=()):
    """A ``size`` x ``size`` grid; node ids are ``row * size + col``.

    With ``rng`` every edge gets a random travel time instead of ``seconds``.
    ``extra_nodes`` adds ``(node_id, x, y)`` nodes with no edges.
    """
    records = []
    for row in range(size):
        for col in range(size):
            records.append((row * size + col, X0 + col * SPACING, Y0 + row * SPACING))
    records += list(extra_nodes)
    nodes = pd.DataFrame(records, columns=["node_id", "x", "y"])
    if crs == "EPSG:4326":
        lon, lat = TO_WGS84.transform(nodes["x"].to_numpy(), nodes["y"].to_numpy())
        nodes["x"], nodes["y"] = lon, lat

    edges = []
    for row in range(size):
        for col in range(size):
            node = row * size + col
            for d_row, d_col in ((0, 1), (1, 0)):
                r, c = row + d_row, col + d_col
                if r < size and c < size:
                    other = r * size + c
                    for a, b in ((node, other), (other, node)):
                        tts = seconds if rng is None else float(rng.uniform(10, 120))
                        edges.append((a, b, tts, SPACING))
    edge_table = pd.DataFrame(edges, columns=["from_node", "to_node", "tts", "length"])
    return _matrix.build_network_matrix(
        nodes,
        edge_table,
        weights=_matrix.OSM_WEIGHTS,
        default_weight="tts",
        layer="walking",
        meta={"mode": "walking", "crs": crs},
    )


# ---------------------------------------------------------------------------
# Weights and cutoffs
# ---------------------------------------------------------------------------
def test_cutoffs_in_minutes_become_seconds_sorted_and_unique():
    given, limits, unit = iso._cutoff_values([10, 5, 10], "s")
    assert given == [5.0, 10.0]
    assert limits == [300.0, 600.0]
    assert unit == "min"


def test_cutoffs_for_length_stay_in_metres():
    assert iso._cutoff_values(500, "m") == ([500.0], [500.0], "m")


@pytest.mark.parametrize(
    ("cutoffs", "message"),
    [([], "cannot be empty"), ([5, 0], "greater than 0"), (["ten"], "numbers")],
)
def test_invalid_cutoffs_are_rejected(cutoffs, message):
    with pytest.raises(ValueError, match=message):
        iso._cutoff_values(cutoffs, "s")


def test_weight_units_come_from_the_metadata():
    matrix = grid_matrix()
    assert iso._weight_unit(matrix, "tts") == "s"
    assert iso._weight_unit(matrix, "length") == "m"
    with pytest.raises(ValueError, match="Available: length, tts"):
        iso._weight_unit(matrix, "tts_m")


# ---------------------------------------------------------------------------
# Origins
# ---------------------------------------------------------------------------
def test_node_ids_are_used_as_they_are():
    matrix = grid_matrix()
    assert iso._snap_origins(matrix, 4, 250).tolist() == [4]
    assert iso._snap_origins(matrix, [0, 8], 250).tolist() == [0, 8]


def test_unknown_node_ids_are_rejected():
    with pytest.raises(ValueError, match="node_id 99 is not in the network"):
        iso._snap_origins(grid_matrix(), 99, 250)


def test_coordinates_snap_to_the_nearest_node():
    matrix = grid_matrix()
    # 30 m right and 20 m up from node 4 (the centre).
    point = (X0 + SPACING + 30, Y0 + SPACING + 20)

    assert iso._snap_origins(matrix, point, 250).tolist() == [4]
    assert iso._snap_origins(matrix, Point(point), 250).tolist() == [4]
    assert iso._snap_origins(matrix, [point, 0], 250).tolist() == [4, 0]


def test_snapping_measures_metres_in_geographic_networks():
    matrix = grid_matrix(crs="EPSG:4326")
    near = TO_WGS84.transform(X0 + 2 * SPACING + 40, Y0)  # 40 m from node 2
    far = TO_WGS84.transform(X0 + 2 * SPACING + 400, Y0)  # 400 m from node 2

    assert iso._snap_origins(matrix, near, 250).tolist() == [2]
    with pytest.raises(ValueError, match=r"is 400 m from the nearest network node"):
        iso._snap_origins(matrix, far, 250)


def test_snapping_skips_nodes_without_edges():
    # An isolated node sits right on the coordinates; node 0 is 50 m away.
    matrix = grid_matrix(extra_nodes=[(100, X0 - 50, Y0)])
    assert iso._snap_origins(matrix, (X0 - 50, Y0), 250).tolist() == [0]


def test_networks_without_a_crs_cannot_snap_coordinates():
    matrix = grid_matrix()
    matrix.meta["crs"] = None
    with pytest.raises(ValueError, match="no CRS"):
        iso._snap_origins(matrix, (X0, Y0), 250)


# ---------------------------------------------------------------------------
# Travel costs
# ---------------------------------------------------------------------------
def test_costs_from_a_corner_stop_at_the_limit():
    matrix = grid_matrix()

    costs = iso._travel_costs(matrix, "tts", np.array([0]), 130, merge_origins=False)

    # Grid ids:  6 7 8 / 3 4 5 / 0 1 2 ; two steps of 60 s fit in 130 s.
    expected = [0, 60, 120, 60, 120, np.inf, 120, np.inf, np.inf]
    assert costs.shape == (1, 9)
    assert costs[0].tolist() == expected


def test_one_row_per_origin_or_one_merged_row():
    matrix = grid_matrix()
    rows = np.array([0, 8])

    separate = iso._travel_costs(matrix, "tts", rows, 1000, merge_origins=False)
    merged = iso._travel_costs(matrix, "tts", rows, 1000, merge_origins=True)

    assert separate.shape == (2, 9)
    assert merged.shape == (1, 9)
    np.testing.assert_array_equal(merged[0], separate.min(axis=0))


def test_costs_match_networkx_on_a_random_grid():
    rng = np.random.default_rng(7)
    matrix = grid_matrix(size=10, rng=rng)
    graph = nx.DiGraph()
    ids = matrix.nodes["node_id"].tolist()
    for row in range(matrix.n_nodes):
        for k in range(matrix.indptr[row], matrix.indptr[row + 1]):
            graph.add_edge(
                ids[row], ids[matrix.indices[k]], tts=matrix.weights["tts"][k]
            )
    expected = nx.single_source_dijkstra_path_length(graph, 0, cutoff=400, weight="tts")

    costs = iso._travel_costs(matrix, "tts", np.array([0]), 400, merge_origins=False)[0]

    reached = {ids[i]: cost for i, cost in enumerate(costs) if np.isfinite(cost)}
    assert reached == pytest.approx(expected)


# ---------------------------------------------------------------------------
# isochrones(): polygons
# ---------------------------------------------------------------------------
def at(col, row):
    """Point at grid position (col, row), in EPSG:25830."""
    return Point(X0 + col * SPACING, Y0 + row * SPACING)


def test_edges_method_covers_the_streets_reached_and_nothing_else():
    areas = iso.isochrones(grid_matrix(), 0, 1)  # 1 min: one 60 s edge

    area = areas.geometry.iloc[0]
    assert areas["n_nodes"].tolist() == [3]  # nodes 0, 1 and 3
    for point in (at(0, 0), at(1, 0), at(0, 1), at(0.5, 0)):
        assert area.covers(point)
    assert not area.covers(at(1, 1))  # 100 m from the streets reached
    assert not area.covers(at(2, 0))


def test_a_partly_reached_edge_is_drawn_up_to_the_point_reached():
    # 90 s from node 0: edge 1 -> 2 is reached halfway, up to x = 150 m.
    area = iso.isochrones(grid_matrix(), 0, 1.5, buffer_distance=25).geometry.iloc[0]

    assert area.covers(at(1.7, 0))  # 20 m beyond the end of the stretch
    assert not area.covers(at(1.9, 0))  # 40 m beyond it


def test_polygons_are_nested_and_count_the_nodes_reached():
    areas = iso.isochrones(grid_matrix(), 0, [3, 1, 2])

    assert areas["cutoff"].tolist() == [1.0, 2.0, 3.0]
    assert areas["unit"].tolist() == ["min", "min", "min"]
    assert areas["n_nodes"].tolist() == [3, 6, 8]
    polygons = areas.geometry.tolist()
    assert polygons[1].covers(polygons[0])
    assert polygons[2].covers(polygons[1])


def test_hull_method_covers_the_nodes_reached():
    areas = iso.isochrones(grid_matrix(), 4, 1, method="hull", concavity=1)

    area = areas.geometry.iloc[0]
    for point in (at(1, 1), at(0, 1), at(2, 1), at(1, 0), at(1, 2)):
        assert area.covers(point)
    assert not area.covers(at(0, 0))  # 71 m outside the diamond of nodes reached
    assert area.geom_type == "Polygon"


def test_length_gives_isodistances_in_metres():
    areas = iso.isochrones(grid_matrix(), 0, 150, weight="length")

    assert areas["unit"].tolist() == ["m"]
    assert areas["n_nodes"].tolist() == [3]


def test_one_set_of_polygons_per_origin():
    areas = iso.isochrones(grid_matrix(), [0, 8], [1, 2])

    assert areas["origin"].tolist() == [0, 0, 8, 8]
    assert areas["n_nodes"].tolist() == [3, 6, 3, 6]


def test_merged_origins_give_one_set_of_polygons():
    areas = iso.isochrones(grid_matrix(), [0, 8], 1, merge_origins=True)

    assert areas["origin"].tolist() == ["0;8"]
    assert areas["n_nodes"].tolist() == [6]  # 0, 1, 3 and 8, 7, 5


def test_result_is_in_the_network_crs():
    matrix = grid_matrix(crs="EPSG:4326")
    origin = TO_WGS84.transform(X0 + 10, Y0 + 10)

    areas = iso.isochrones(matrix, origin, 1)

    assert areas.crs.to_epsg() == 4326
    assert areas["origin"].tolist() == [0]
    assert areas.geometry.iloc[0].covers(Point(TO_WGS84.transform(X0 + 50, Y0)))


# ---------------------------------------------------------------------------
# isochrones(): inputs and errors
# ---------------------------------------------------------------------------
def test_any_network_input_gives_the_same_result(tmp_path):
    matrix = grid_matrix()
    _matrix.write_matrix(matrix, tmp_path / "walking" / "matrix.npz")

    expected = iso.isochrones(matrix, 4, [1, 2])
    for network, mode in (
        (tmp_path, "walking"),
        (tmp_path / "walking", None),
        (tmp_path / "walking" / "matrix.npz", None),
        (read_matrix(tmp_path / "walking"), None),
    ):
        result = iso.isochrones(network, 4, [1, 2], mode=mode)
        assert result["n_nodes"].tolist() == expected["n_nodes"].tolist()
        for got, want in zip(result.geometry, expected.geometry, strict=True):
            assert got.equals_exact(want, tolerance=1e-6)


def test_a_read_matrix_result_only_has_its_weight(tmp_path):
    _matrix.write_matrix(grid_matrix(), tmp_path / "walking" / "matrix.npz")
    with pytest.raises(ValueError, match="Available: tts"):
        iso.isochrones(read_matrix(tmp_path / "walking"), 4, 100, weight="length")


@pytest.mark.parametrize(("mode", "layer"), [("bus", "bus/emt"), ("train", "x")])
def test_bus_and_train_networks_are_not_supported_yet(mode, layer):
    matrix = grid_matrix()
    matrix.meta.update(mode=mode, layer=layer)
    with pytest.raises(NotImplementedError, match="multimodal network"):
        iso.isochrones(matrix, 0, 5)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"method": "circle"}, "method must be one of"),
        ({"concavity": 1.5}, "between 0 and 1"),
        ({"buffer_distance": 0}, "with method='edges'"),
        ({"buffer_distance": -5, "method": "hull"}, "greater than 0"),
        ({"max_snap_distance": 0}, "max_snap_distance"),
    ],
)
def test_invalid_arguments_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        iso.isochrones(grid_matrix(), 0, 5, **kwargs)


def test_crossing_stretches_with_the_same_extent_are_both_drawn():
    # The two diagonals of a 100 m square share their bounding box: both must
    # be drawn, not merged as one duplicated stretch.
    start = np.array([[0.0, 0.0], [0.0, 100.0], [100.0, 100.0]])
    end = np.array([[100.0, 100.0], [100.0, 0.0], [0.0, 0.0]])  # last = first reversed
    area = iso._edges_polygon(np.empty((0, 2)), start, end, 5)

    assert area.covers(Point(10, 10)) and area.covers(Point(10, 90))
    assert not area.covers(Point(50, 10))