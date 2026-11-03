"""Tests for shortest_path, on the synthetic grids of test_isochrones.

Grid ids (3 x 3, nodes 100 m apart, edges of 60 s and 100 m both ways):

    6 7 8
    3 4 5
    0 1 2
"""

from __future__ import annotations

import networkx as nx
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString

from multimodalpy import _matrix, read_matrix, shortest_path
from multimodalpy.analysis import _shortest_path
from test_isochrones import SPACING, TO_WGS84, X0, Y0, grid_matrix


def _line(*cells):
    """Line through grid positions (col, row), in EPSG:25830."""
    return LineString([(X0 + c * SPACING, Y0 + r * SPACING) for c, r in cells])


# ---------------------------------------------------------------------------
# Pairs, costs and threshold
# ---------------------------------------------------------------------------
def test_every_origin_is_paired_with_every_destination_in_order():
    paths = shortest_path(grid_matrix(), [4, 0], [2, 6, 4], None)

    pairs = list(zip(paths["origin"], paths["destination"], strict=True))
    assert pairs == [(4, 2), (4, 6), (4, 4), (0, 2), (0, 6), (0, 4)]
    assert paths["time_min"].tolist() == [2.0, 2.0, 0.0, 2.0, 2.0, 2.0]
    assert paths["length_m"].tolist() == [200.0, 200.0, 0.0, 200.0, 200.0, 200.0]
    assert paths["n_edges"].tolist() == [2, 2, 0, 2, 2, 2]


def test_pairs_beyond_the_threshold_are_left_out():
    paths = shortest_path(grid_matrix(), 0, [1, 4, 8], 2)  # 0 -> 8 takes 4 min

    assert paths["destination"].tolist() == [1, 4]


def test_a_threshold_in_metres_with_length():
    paths = shortest_path(grid_matrix(), 0, [1, 4, 8], 250, weight="length")

    assert paths["destination"].tolist() == [1, 4]
    assert paths["length_m"].tolist() == [100.0, 200.0]


def test_no_pair_within_the_threshold_gives_an_empty_table():
    paths = shortest_path(grid_matrix(), 0, 8, 1)

    assert paths.empty
    assert list(paths.columns) == [
        "origin",
        "destination",
        "time_min",
        "length_m",
        "n_edges",
        "geometry",
    ]
    assert paths.crs.to_epsg() == 25830


def test_both_measures_follow_the_path_chosen():
    # Make the direct 0 -> 1 street slow: the fastest path goes round it,
    # and its length is that of the detour, not of the direct street.
    matrix = grid_matrix()
    matrix.weights["tts"] = matrix.weights["tts"].copy()
    keys = _shortest_path._edge_keys(matrix)
    slow = np.searchsorted(keys, 0 * matrix.n_nodes + 1)
    matrix.weights["tts"][slow] = 1000.0

    fastest = shortest_path(matrix, 0, 1, None)
    shortest = shortest_path(matrix, 0, 1, None, weight="length")

    assert fastest["time_min"].tolist() == [3.0]  # 0 -> 3 -> 4 -> 1
    assert fastest["length_m"].tolist() == [300.0]
    assert shortest["length_m"].tolist() == [100.0]
    assert shortest["time_min"].tolist() == [pytest.approx(1000 / 60)]


def test_costs_match_networkx_on_a_random_grid():
    rng = np.random.default_rng(3)
    matrix = grid_matrix(size=10, rng=rng)
    graph = nx.DiGraph()
    ids = matrix.nodes["node_id"].tolist()
    for row in range(matrix.n_nodes):
        for k in range(matrix.indptr[row], matrix.indptr[row + 1]):
            graph.add_edge(
                ids[row], ids[matrix.indices[k]], tts=matrix.weights["tts"][k]
            )

    origins, destinations = [0, 45, 99], [9, 50, 90, 99]
    paths = shortest_path(matrix, origins, destinations, None)

    for row in paths.itertuples():
        expected = nx.dijkstra_path_length(graph, row.origin, row.destination, "tts")
        route = nx.dijkstra_path(graph, row.origin, row.destination, "tts")
        assert row.time_min == pytest.approx(expected / 60)
        assert row.n_edges == len(route) - 1


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def test_paths_are_drawn_from_origin_to_destination():
    paths = shortest_path(grid_matrix(), 0, 1, None)

    assert paths.geometry.iloc[0].equals(_line((0, 0), (1, 0)))


def test_the_line_follows_every_node_of_the_path():
    # Only 0 -> 1 -> 2 -> 5 -> 8 is open: the line must go through all of it.
    matrix = grid_matrix()
    matrix.weights["tts"] = matrix.weights["tts"].copy()
    keys = _shortest_path._edge_keys(matrix)
    n = matrix.n_nodes
    open_edges = {(0, 1), (1, 2), (2, 5), (5, 8)}
    for a in range(n):
        for k in range(matrix.indptr[a], matrix.indptr[a + 1]):
            b = int(matrix.indices[k])
            if (a, b) not in open_edges:
                matrix.weights["tts"][np.searchsorted(keys, a * n + b)] = np.inf

    line = shortest_path(matrix, 0, 8, None).geometry.iloc[0]

    assert line.equals(_line((0, 0), (1, 0), (2, 0), (2, 1), (2, 2)))


def test_same_origin_and_destination_has_no_line():
    paths = shortest_path(grid_matrix(), 4, 4, None)

    assert paths["time_min"].tolist() == [0.0]
    assert paths.geometry.iloc[0] is None


def test_geometry_false_still_returns_a_geodataframe_with_the_costs():
    paths = shortest_path(grid_matrix(), [0, 4], [8], None, geometry=False)

    assert paths["time_min"].tolist() == [4.0, 2.0]
    assert paths.geometry.isna().all()
    assert paths.crs.to_epsg() == 25830


def test_result_is_in_the_network_crs():
    matrix = grid_matrix(crs="EPSG:4326")
    origin = TO_WGS84.transform(X0 + 10, Y0 + 10)
    destination = TO_WGS84.transform(X0 + 2 * SPACING + 20, Y0)

    paths = shortest_path(matrix, origin, destination, None)

    assert paths.crs.to_epsg() == 4326
    assert paths[["origin", "destination"]].values.tolist() == [[0, 2]]
    x, y = paths.geometry.iloc[0].coords[-1]
    assert (x, y) == pytest.approx(TO_WGS84.transform(X0 + 2 * SPACING, Y0))


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------
def test_destinations_snap_to_nodes_that_can_be_reached():
    # Node 9 only has an edge out (9 -> 0): a destination next to it snaps
    # to node 0, the nearest node with an edge in.
    matrix = grid_matrix()
    nodes = pd.concat(
        [matrix.nodes[["node_id", "x", "y"]],
         pd.DataFrame({"node_id": [9], "x": [X0 - 30], "y": [Y0]})],
        ignore_index=True,
    )  # fmt: skip
    rows = [(9, 0)] + [
        (int(matrix.nodes["node_id"][a]), int(matrix.nodes["node_id"][b]))
        for a in range(matrix.n_nodes)
        for b in matrix.indices[matrix.indptr[a] : matrix.indptr[a + 1]]
    ]
    edges = pd.DataFrame(rows, columns=["from_node", "to_node"])
    edges["tts"], edges["length"] = 60.0, 100.0
    matrix = _matrix.build_network_matrix(
        nodes, edges, weights=_matrix.OSM_WEIGHTS, default_weight="tts",
        layer="walking", meta={"mode": "walking", "crs": "EPSG:25830"},
    )  # fmt: skip

    paths = shortest_path(matrix, 4, (X0 - 30, Y0), None)

    assert paths["destination"].tolist() == [0]


def test_errors_name_the_destination():
    with pytest.raises(ValueError, match="Destination node_id 99 is not in"):
        shortest_path(grid_matrix(), 0, 99, None)
    with pytest.raises(ValueError, match="Destination .* m from the nearest"):
        shortest_path(grid_matrix(), 0, (X0 + 5000, Y0), None)
    with pytest.raises(ValueError, match="destinations cannot be empty"):
        shortest_path(grid_matrix(), 0, [], None)


# ---------------------------------------------------------------------------
# Inputs, batches and errors
# ---------------------------------------------------------------------------
def test_a_read_matrix_result_leaves_the_missing_measure_empty(tmp_path):
    _matrix.write_matrix(grid_matrix(), tmp_path / "walking" / "matrix.npz")

    paths = shortest_path(read_matrix(tmp_path / "walking"), 0, 2, None)

    assert paths["time_min"].tolist() == [2.0]
    assert paths["length_m"].isna().all()


def test_any_network_input_gives_the_same_result(tmp_path):
    matrix = grid_matrix()
    _matrix.write_matrix(matrix, tmp_path / "walking" / "matrix.npz")

    expected = shortest_path(matrix, [0, 4], [8, 2], None)
    for network, mode in ((tmp_path, "walking"), (tmp_path / "walking", None)):
        result = shortest_path(network, [0, 4], [8, 2], None, mode=mode)
        pd.testing.assert_frame_equal(
            result.drop(columns="geometry"), expected.drop(columns="geometry")
        )


def test_small_batches_give_the_same_result(monkeypatch):
    matrix = grid_matrix(size=6, rng=np.random.default_rng(5))
    origins, destinations = list(range(0, 36, 5)), list(range(36))
    expected = shortest_path(matrix, origins, destinations, 5)

    monkeypatch.setattr(_shortest_path, "_BATCH_CELLS", matrix.n_nodes * 2)
    result = shortest_path(matrix, origins, destinations, 5)

    pd.testing.assert_frame_equal(result, expected)


def test_repeated_origins_are_computed_once_and_returned_each_time():
    paths = shortest_path(grid_matrix(), [0, 0], [1], None)

    assert paths["origin"].tolist() == [0, 0]


def test_bus_and_train_networks_are_not_supported_yet():
    matrix = grid_matrix()
    matrix.meta.update(mode="bus", layer="bus/emt")
    with pytest.raises(NotImplementedError, match="Shortest paths for bus"):
        shortest_path(matrix, 0, 1, None)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"threshold": 0}, "greater than 0"),
        ({"max_snap_distance": 0}, "max_snap_distance"),
        ({"weight": "tts_m"}, "Available: length, tts"),
    ],
)
def test_invalid_arguments_are_rejected(kwargs, message):
    arguments = {"threshold": None, **kwargs}
    with pytest.raises(ValueError, match=message):
        shortest_path(grid_matrix(), 0, 1, **arguments)


# ---------------------------------------------------------------------------
# Sums along the tree
# ---------------------------------------------------------------------------
def test_sums_along_a_tree():
    # 0 -> 1 -> 2 -> 3 and 1 -> 4, with edge values 10, 20, 30 and 5.
    parent = np.array([-1, 0, 1, 2, 1])
    edge = np.array([0.0, 10.0, 20.0, 30.0, 5.0])

    sums = _shortest_path._sums_along_tree(parent, edge)

    assert sums.tolist() == [0.0, 10.0, 30.0, 60.0, 15.0]
