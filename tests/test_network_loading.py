"""Tests for loading a network from any output format (_matrix.load_network_matrix).

Every format is written from the same tiny OSM layers, and each one must
load into the same matrix. No network is needed.
"""

from __future__ import annotations

import networkx as nx
import numpy as np
import pytest
from shapely.geometry import LineString

from multimodalpy import _matrix, get_network, read_graph_json, read_matrix
from multimodalpy import _process_osm as process_osm


@pytest.fixture
def layers():
    """Walking layers: 1 <-> 2 two-way, 2 -> 3 one-way."""
    graph = nx.MultiDiGraph(crs="EPSG:4326")
    for node, x in ((1, -0.376), (2, -0.375), (3, -0.374)):
        graph.add_node(node, x=x, y=39.47)
    common = {"length": 86.0, "highway": "residential"}
    graph.add_edge(
        1, 2, osmid=10, oneway=False, reversed=False,
        geometry=LineString([(-0.376, 39.47), (-0.375, 39.47)]), **common,
    )  # fmt: skip
    graph.add_edge(
        2, 1, osmid=10, oneway=False, reversed=True,
        geometry=LineString([(-0.375, 39.47), (-0.376, 39.47)]), **common,
    )  # fmt: skip
    graph.add_edge(
        2, 3, osmid=11, oneway=True, reversed=False,
        geometry=LineString([(-0.375, 39.47), (-0.374, 39.47)]), **common,
    )  # fmt: skip
    return process_osm.build_final_osm_layers(graph, layer_id="w", mode="walk")


@pytest.fixture
def expected(layers):
    nodes, edges = layers
    return _matrix.osm_network_matrix(nodes, edges, mode="walking")


def _assert_same_network(loaded, expected):
    np.testing.assert_array_equal(loaded.indptr, expected.indptr)
    np.testing.assert_array_equal(loaded.indices, expected.indices)
    assert loaded.nodes["node_id"].tolist() == expected.nodes["node_id"].tolist()
    np.testing.assert_allclose(loaded.nodes[["x", "y"]], expected.nodes[["x", "y"]])
    for name in ("tts", "length"):
        np.testing.assert_allclose(loaded.weights[name], expected.weights[name])


def _write(fmt, layers, root):
    """Write the walking layers under ``root`` as main() would in ``fmt``."""
    nodes, edges = layers
    folder = root / "walking"
    folder.mkdir(parents=True, exist_ok=True)
    if fmt == "matrix":
        matrix = _matrix.osm_network_matrix(nodes, edges, mode="walking")
        _matrix.write_matrix(matrix, folder / "matrix.npz")
    elif fmt == "networkx":
        get_network._osm_layers_to_graph_json(nodes, edges, folder / "graph.json")
    elif fmt == "geopackage":
        nodes.to_file(root / "valencia.gpkg", layer="osm_walking_nodes", driver="GPKG")
        edges.to_file(root / "valencia.gpkg", layer="osm_walking_edges", driver="GPKG")
    elif fmt == "geojson":
        nodes.to_file(folder / "nodes.geojson", driver="GeoJSON")
        edges.to_file(folder / "edges.geojson", driver="GeoJSON")
    elif fmt == "shapefile":
        nodes.to_file(folder / "nodes.shp")
        edges.to_file(folder / "edges.shp")


FORMATS = ["matrix", "networkx", "geopackage", "geojson", "shapefile"]


# ---------------------------------------------------------------------------
# Every format gives the same matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", FORMATS)
def test_output_root_with_mode_loads_every_format(fmt, layers, expected, tmp_path):
    _write(fmt, layers, tmp_path)

    loaded = _matrix.load_network_matrix(tmp_path, mode="walking")

    _assert_same_network(loaded, expected)
    assert loaded.meta["mode"] == "walking"


@pytest.mark.parametrize("fmt", ["matrix", "networkx", "geojson", "shapefile"])
def test_mode_folder_loads_without_mode(fmt, layers, expected, tmp_path):
    _write(fmt, layers, tmp_path)

    loaded = _matrix.load_network_matrix(tmp_path / "walking")

    _assert_same_network(loaded, expected)


def test_root_with_a_single_network_needs_no_mode(layers, expected, tmp_path):
    _write("geojson", layers, tmp_path)
    _assert_same_network(_matrix.load_network_matrix(tmp_path), expected)


def test_formats_are_tried_in_order(layers, tmp_path):
    # Each format gets a different travel time, so the one read is visible.
    nodes, edges = layers
    for factor, fmt in enumerate(FORMATS, start=1):
        scaled = edges.copy()
        scaled["tts"] = edges["tts"] * factor
        _write(fmt, (nodes, scaled), tmp_path)

    seen = []
    for fmt in FORMATS:
        loaded = _matrix.load_network_matrix(tmp_path, mode="walking")
        seen.append(round(loaded.weights["tts"][0] / edges["tts"].iloc[0]))
        if fmt == "matrix":
            (tmp_path / "walking" / "matrix.npz").unlink()
        elif fmt == "networkx":
            (tmp_path / "walking" / "graph.json").unlink()
        elif fmt == "geopackage":
            (tmp_path / "valencia.gpkg").unlink()
        elif fmt == "geojson":
            for path in (tmp_path / "walking").glob("*.geojson"):
                path.unlink()

    assert seen == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("name", ["matrix.npz", "graph.json"])
def test_single_files_load_directly(name, layers, expected, tmp_path):
    _write("matrix" if name == "matrix.npz" else "networkx", layers, tmp_path)
    _assert_same_network(
        _matrix.load_network_matrix(tmp_path / "walking" / name), expected
    )


def test_a_geopackage_file_needs_the_mode(layers, expected, tmp_path):
    _write("geopackage", layers, tmp_path)
    gpkg = tmp_path / "valencia.gpkg"

    _assert_same_network(_matrix.load_network_matrix(gpkg, mode="walking"), expected)
    with pytest.raises(ValueError, match="Pass mode="):
        _matrix.load_network_matrix(gpkg)
    with pytest.raises(ValueError, match="no 'bike' network"):
        _matrix.load_network_matrix(gpkg, mode="bike")


# ---------------------------------------------------------------------------
# Objects in memory
# ---------------------------------------------------------------------------
def test_graph_from_read_graph_json(layers, expected, tmp_path):
    _write("networkx", layers, tmp_path)
    graph = read_graph_json(tmp_path / "walking" / "graph.json")

    loaded = _matrix.load_network_matrix(graph, mode="walking")

    _assert_same_network(loaded, expected)
    assert loaded.meta["crs"] == "EPSG:4326"


def test_read_matrix_result_keeps_its_single_weight(layers, expected, tmp_path):
    _write("matrix", layers, tmp_path)

    loaded = _matrix.load_network_matrix(read_matrix(tmp_path / "walking"))

    assert set(loaded.weights) == {"tts"}
    assert loaded.default_weight == "tts"
    np.testing.assert_array_equal(loaded.indptr, expected.indptr)
    np.testing.assert_array_equal(loaded.indices, expected.indices)
    np.testing.assert_allclose(loaded.weights["tts"], expected.weights["tts"])


def test_network_matrix_is_returned_as_is(expected):
    assert _matrix.load_network_matrix(expected) is expected


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
def test_root_with_several_networks_asks_for_the_mode(layers, tmp_path):
    _write("geojson", layers, tmp_path)
    (tmp_path / "bike").mkdir()

    with pytest.raises(ValueError, match=r"mode= \(available: walking, bike\)"):
        _matrix.load_network_matrix(tmp_path)


def test_mode_folder_and_a_different_mode_are_rejected(layers, tmp_path):
    _write("geojson", layers, tmp_path)
    with pytest.raises(ValueError, match="mode='bike'"):
        _matrix.load_network_matrix(tmp_path / "walking", mode="bike")


def test_missing_network_lists_what_was_looked_for(tmp_path):
    (tmp_path / "walking").mkdir()
    with pytest.raises(FileNotFoundError, match="graph.json, a GeoPackage"):
        _matrix.load_network_matrix(tmp_path / "walking")
    with pytest.raises(FileNotFoundError, match="No network at"):
        _matrix.load_network_matrix(tmp_path / "nowhere")


def test_graph_without_travel_times_is_rejected():
    graph = nx.MultiDiGraph()
    graph.add_node("s1", x=0.0, y=0.0)
    graph.add_node("s2", x=1.0, y=0.0)
    graph.add_edge("s1", "s2", travel_time_seconds_mean=60.0)

    with pytest.raises(ValueError, match="no 'tts' travel times"):
        _matrix.load_network_matrix(graph)


def test_other_objects_are_rejected():
    with pytest.raises(TypeError, match="network must be"):
        _matrix.load_network_matrix(42)