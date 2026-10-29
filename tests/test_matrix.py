"""Tests for the CSR matrix core (_matrix): builder, adapters and npz file.

Everything runs on tiny synthetic layers, so no network is needed.
"""

from __future__ import annotations

import json
import zipfile

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import pytest
from scipy.sparse import load_npz
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import LineString, Point

from multimodalpy import _matrix
from multimodalpy import _process_osm as process_osm
from multimodalpy.get_network import GTFS_EDGE_RENAME


def _abc_tables():
    """The A, B, C example: A->B 30, A->C 50, B->C 20, C->A 45."""
    nodes = pd.DataFrame({"node_id": ["A", "B", "C"], "x": [0, 1, 2], "y": [0, 0, 0]})
    edges = pd.DataFrame(
        {
            "from_node": ["A", "A", "B", "C"],
            "to_node": ["B", "C", "C", "A"],
            "tts": [30.0, 50.0, 20.0, 45.0],
        }
    )
    return nodes, edges


def _build(nodes, edges, weights=None, default="tts"):
    return _matrix.build_network_matrix(
        nodes,
        edges,
        weights=weights or {"tts": "s"},
        default_weight=default,
        layer="walking",
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def test_abc_example_gives_the_documented_csr_arrays():
    matrix = _build(*_abc_tables())

    assert matrix.indptr.tolist() == [0, 2, 3, 4]
    assert matrix.indices.tolist() == [1, 2, 2, 0]
    assert matrix.weights["tts"].tolist() == [30.0, 50.0, 20.0, 45.0]
    assert matrix.nodes["node_id"].tolist() == ["A", "B", "C"]
    dense = matrix.to_scipy().toarray()
    assert dense[0, 1] == 30.0 and dense[2, 0] == 45.0 and dense[1, 0] == 0.0


def test_parallel_edges_keep_the_minimum_of_each_weight_separately():
    nodes, _ = _abc_tables()
    edges = pd.DataFrame(
        {
            "from_node": ["A", "A"],
            "to_node": ["B", "B"],
            "tts": [30.0, 40.0],
            "length": [500.0, 300.0],
        }
    )
    matrix = _build(nodes, edges, weights={"tts": "s", "length": "m"})

    assert matrix.n_edges == 1
    assert matrix.weights["tts"].tolist() == [30.0]
    assert matrix.weights["length"].tolist() == [300.0]


def test_self_loops_are_dropped_and_isolated_nodes_are_empty_rows():
    nodes = pd.DataFrame({"node_id": [1, 2, 3], "x": [0, 1, 2], "y": [0, 0, 0]})
    edges = pd.DataFrame({"from_node": [1, 1], "to_node": [1, 2], "tts": [5.0, 7.0]})
    matrix = _build(nodes, edges)

    assert matrix.indptr.tolist() == [0, 1, 1, 1]
    assert matrix.indices.tolist() == [1]


def test_missing_weights_become_inf_and_zero_weights_are_kept():
    nodes, _ = _abc_tables()
    edges = pd.DataFrame(
        {"from_node": ["A", "B"], "to_node": ["B", "C"], "tts": [0.0, np.nan]}
    )
    matrix = _build(nodes, edges)

    assert matrix.weights["tts"].tolist() == [0.0, np.inf]
    # The zero is an edge for scipy's Dijkstra; the inf one cannot be used.
    distances = dijkstra(matrix.to_scipy(), indices=0)
    assert distances.tolist() == [0.0, 0.0, np.inf]


def test_endpoints_missing_from_the_nodes_are_appended_outside_the_area():
    nodes = pd.DataFrame({"node_id": ["s1"], "x": [0.0], "y": [0.0]})
    edges = gpd.GeoDataFrame(
        {
            "from_node": ["s1", "out", "s1"],
            "to_node": ["out", "s1", "far"],
            "tts": [60.0, 60.0, 90.0],
        },
        geometry=[
            LineString([(0, 0), (5, 5)]),
            LineString([(5, 5), (0, 0)]),
            LineString([(0, 0), (9, 1)]),
        ],
    )
    matrix = _build(nodes, edges)

    table = matrix.nodes
    assert table["node_id"].tolist() == ["s1", "out", "far"]
    assert table["in_area"].tolist() == [True, False, False]
    assert table.loc[1, ["x", "y"]].tolist() == [5.0, 5.0]
    assert table.loc[2, ["x", "y"]].tolist() == [9.0, 1.0]
    assert matrix.n_edges == 3


def test_repeated_node_ids_are_rejected():
    nodes = pd.DataFrame({"node_id": ["A", "A"], "x": [0, 1], "y": [0, 0]})
    with pytest.raises(ValueError, match="Repeated node_id"):
        _build(nodes, _abc_tables()[1])


def test_edges_without_any_weight_column_are_rejected():
    nodes, edges = _abc_tables()
    with pytest.raises(ValueError, match="weight columns"):
        _build(nodes, edges.drop(columns="tts"), weights={"length": "m"})


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------
def test_write_and_read_round_trip(tmp_path):
    nodes, edges = _abc_tables()
    edges["length"] = [1.0, 2.0, 3.0, 0.0]
    matrix = _build(nodes, edges, weights={"tts": "s", "length": "m"})

    path = _matrix.write_matrix(matrix, tmp_path / "walking" / "matrix.npz")
    loaded = _matrix.read_network_matrix(path)

    assert path.name == "matrix.npz"
    np.testing.assert_array_equal(loaded.indptr, matrix.indptr)
    np.testing.assert_array_equal(loaded.indices, matrix.indices)
    for name in ("tts", "length"):
        np.testing.assert_array_equal(loaded.weights[name], matrix.weights[name])
    pd.testing.assert_frame_equal(loaded.nodes, matrix.nodes, check_dtype=False)
    assert loaded.meta == matrix.meta
    assert loaded.edge_type is None


def test_file_opens_with_scipy_load_npz_and_without_pickle(tmp_path):
    path = _matrix.write_matrix(_build(*_abc_tables()), tmp_path / "matrix.npz")

    with np.load(path, allow_pickle=False) as data:
        assert all(data[key].dtype != object for key in data.files)
    loaded = load_npz(path)
    assert loaded.toarray()[0].tolist() == [0.0, 30.0, 50.0]


def test_integer_node_ids_stay_integers(tmp_path):
    nodes = pd.DataFrame({"node_id": [10, 20], "x": [0, 1], "y": [0, 0]})
    edges = pd.DataFrame({"from_node": [10], "to_node": [20], "tts": [3.0]})
    path = _matrix.write_matrix(_build(nodes, edges), tmp_path / "matrix.npz")

    with np.load(path) as data:
        assert data["node_id"].dtype == np.int64


def _rewrite_meta(path, **changes):
    with np.load(path) as data:
        arrays = {key: data[key] for key in data.files}
    meta = json.loads(str(arrays["meta"]))
    meta.update(changes)
    arrays["meta"] = np.array(json.dumps(meta))
    with path.open("wb") as handle:
        np.savez_compressed(handle, **arrays)


def test_newer_format_versions_are_rejected(tmp_path):
    path = _matrix.write_matrix(_build(*_abc_tables()), tmp_path / "matrix.npz")
    _rewrite_meta(path, format_version=_matrix.FORMAT_VERSION + 1)

    with pytest.raises(ValueError, match="format version"):
        _matrix.read_network_matrix(path)


def test_files_that_are_not_matrices_are_rejected(tmp_path):
    path = tmp_path / "other.npz"
    with path.open("wb") as handle:
        np.savez(handle, values=np.arange(3))

    with pytest.raises(ValueError, match="not a multimodalpy matrix"):
        _matrix.read_network_matrix(path)


def test_unknown_weight_lists_the_available_ones():
    matrix = _build(*_abc_tables())
    with pytest.raises(KeyError, match="Available: tts"):
        matrix.to_scipy("length")


def test_written_file_is_a_zip_named_exactly_as_given(tmp_path):
    path = _matrix.write_matrix(_build(*_abc_tables()), tmp_path / "graph_matrix")
    assert path.exists() and zipfile.is_zipfile(path)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
def _osm_layers(mode="drive"):
    graph = nx.MultiDiGraph(crs="EPSG:4326")
    for node, x in ((1, 0.0), (2, 0.001), (3, 0.002)):
        graph.add_node(node, x=x, y=0.0)
    common = {"length": 111.0, "highway": "residential"}
    graph.add_edge(
        1, 2, osmid=10, oneway=False, reversed=False,
        geometry=LineString([(0, 0), (0.001, 0)]), **common,
    )  # fmt: skip
    graph.add_edge(
        2, 1, osmid=10, oneway=False, reversed=True,
        geometry=LineString([(0.001, 0), (0, 0)]), **common,
    )  # fmt: skip
    graph.add_edge(
        2, 3, osmid=11, oneway=True, reversed=False,
        geometry=LineString([(0.001, 0), (0.002, 0)]), **common,
    )  # fmt: skip
    return process_osm.build_final_osm_layers(graph, layer_id="d", mode=mode)


def test_osm_matrix_follows_the_node_layer_and_one_way_streets():
    nodes, edges = _osm_layers()
    matrix = _matrix.osm_network_matrix(nodes, edges, mode="driving")

    assert matrix.nodes["node_id"].tolist() == nodes["node_id"].tolist()
    assert set(matrix.weights) == {"tts", "length"}
    assert matrix.default_weight == "tts"
    assert matrix.meta["crs"] == "EPSG:4326"
    assert matrix.meta["layer"] == "driving"
    row = {node: i for i, node in enumerate(matrix.nodes["node_id"])}
    dense = matrix.to_scipy().toarray()
    assert dense[row[2], row[3]] > 0  # 2 -> 3 is one-way
    assert dense[row[3], row[2]] == 0


def test_osm_matrix_shortest_paths_match_networkx():
    nodes, edges = _osm_layers()
    matrix = _matrix.osm_network_matrix(nodes, edges, mode="driving")

    graph = nx.MultiDiGraph()
    for row in edges.itertuples():
        graph.add_edge(row.from_node, row.to_node, tts=row.tts)
    expected = nx.single_source_dijkstra_path_length(graph, 1, weight="tts")

    distances = dijkstra(matrix.to_scipy("tts"), indices=0)
    ids = matrix.nodes["node_id"].tolist()
    assert {ids[i]: d for i, d in enumerate(distances) if np.isfinite(d)} == (
        pytest.approx(expected)
    )


def test_gtfs_weights_are_the_travel_time_columns_of_the_edge_layer():
    travel_times = {
        short
        for short in GTFS_EDGE_RENAME.values()
        if short.startswith(("tts_", "tm_"))
    }
    assert set(_matrix.GTFS_WEIGHTS) == travel_times
    assert len(_matrix.GTFS_WEIGHTS) == 18


def test_gtfs_matrix_uses_the_present_weights_and_records_the_periods():
    stops = gpd.GeoDataFrame(
        {"node_id": ["s1", "s2"], "stop_name": ["One", "Two"]},
        geometry=[Point(0, 0), Point(1, 0)],
        crs="EPSG:4326",
    )
    edges = gpd.GeoDataFrame(
        {
            "from_node": ["s1", "s1", "s2"],
            "to_node": ["s2", "s2", "s3"],
            "route_id": ["L1", "L5", "L1"],
            "trips": [10, 4, 10],
            "tts_m": [120.0, 100.0, 60.0],
            "tts_m_pam": [np.nan, 110.0, np.nan],
        },
        geometry=[
            LineString([(0, 0), (1, 0)]),
            LineString([(0, 0), (1, 0)]),
            LineString([(1, 0), (2, 0)]),
        ],
        crs="EPSG:4326",
    )
    periods = {"peak_am": (7, 9), "peak_pm": (17, 20)}
    matrix = _matrix.gtfs_network_matrix(
        stops, edges, mode="bus", dataset="emt", periods=periods
    )

    assert set(matrix.weights) == {"tts_m", "tts_m_pam"}
    assert matrix.default_weight == "tts_m"
    assert matrix.nodes["layer"].unique().tolist() == ["bus/emt"]
    assert matrix.nodes["node_id"].tolist() == ["s1", "s2", "s3"]
    assert matrix.nodes["in_area"].tolist() == [True, True, False]
    # Two routes s1 -> s2: the fastest per weight; s2 -> s3 has no service
    # in the morning peak.
    assert matrix.weights["tts_m"].tolist() == [100.0, 60.0]
    assert matrix.weights["tts_m_pam"].tolist() == [110.0, np.inf]
    assert matrix.meta["gtfs_periods"] == {"peak_am": [7, 9], "peak_pm": [17, 20]}
    assert matrix.meta["dataset"] == "emt"