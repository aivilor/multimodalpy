"""Output-format tests: folders, column names and OSM labels.

Covers the fixes from issue #29. OSM and GTFS downloads are mocked, so
these tests don't depend on the network or on credentials.
"""

from __future__ import annotations

import logging
import zipfile
from unittest.mock import patch

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from multimodalpy import get_area, get_network, process_gtfs, process_osm


# ---------------------------------------------------------------------------
# Fixtures: minimal sample data, no network
# ---------------------------------------------------------------------------
def _layer(columns, geometry):
    return gpd.GeoDataFrame(columns, geometry=geometry, crs="EPSG:4326")


@pytest.fixture
def fake_boundary():
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    return _layer({"name": ["fake_area"]}, [polygon])


@pytest.fixture
def fake_osm_layers():
    nodes = _layer({"node_id": ["n1", "n2"]}, [Point(0, 0), Point(1, 1)])
    edges = _layer(
        {"from_node": ["n1"], "to_node_id": ["n2"]}, [LineString([(0, 0), (1, 1)])]
    )
    return {"walking": {"nodes": nodes, "edges": edges, "graph": object()}}


@pytest.fixture
def fake_gtfs_layers():
    """Two datasets, one train and one bus, with long column names."""
    nodes = _layer(
        {"node_id": ["s1", "s2"], "stop_name": ["A", "B"]}, [Point(0, 0), Point(1, 1)]
    )
    edges = _layer(
        {
            "from_node_id": ["s1"],
            "to_node_id": ["s2"],
            "trip_count": [3],
            "trip_count_rest_of_day_weekend": [1],
        },
        [LineString([(0, 0), (1, 1)])],
    )
    schedule = pd.DataFrame({"trip_id": ["t1"]})
    layers = {"nodes": nodes, "edges": edges, "schedule": schedule}
    return {"cercanias": {**layers, "mode": "train"}, "alsa": {**layers, "mode": "bus"}}


@pytest.fixture
def run_main(tmp_path, fake_boundary, fake_osm_layers, fake_gtfs_layers):
    """Run main() with the mocked network; the GTFS download leaves a zip on disk."""

    def fake_download_gtfs(area_name, boundary, zip_dir, **kwargs):
        zip_dir.mkdir(parents=True, exist_ok=True)
        (zip_dir / "feed.zip").write_bytes(b"")
        return fake_gtfs_layers

    def _run(**kwargs):
        with (
            patch.object(get_area, "find_area_boundary", return_value=fake_boundary),
            patch.object(get_area, "download_osm_layers", return_value=fake_osm_layers),
            patch.object(
                get_area, "download_gtfs_layers", side_effect=fake_download_gtfs
            ),
        ):
            return get_network.main(
                area_name="Valencia",
                modes=["walking", "bus", "train"],
                output_path=str(tmp_path),
                **kwargs,
            )

    return _run


# ---------------------------------------------------------------------------
# Folder structure
# ---------------------------------------------------------------------------
def test_layers_are_grouped_in_one_folder_per_mode(tmp_path, run_main):
    manifest = run_main(output_file_type="geojson")

    files = set(manifest["files"])
    assert {
        "study_area_boundary.geojson",
        "walking/nodes.geojson",
        "walking/edges.geojson",
        "bus/alsa/nodes.geojson",
        "train/cercanias/edges.geojson",
    } <= files
    for filename in files:
        assert (tmp_path / filename).exists()


def test_geopackage_is_a_single_file_at_the_root(tmp_path, run_main):
    manifest = run_main(output_file_type="geopackage")

    assert (tmp_path / "valencia.gpkg").exists()
    assert "valencia.gpkg::osm_walking_nodes" in manifest["files"]
    assert not (tmp_path / "walking").exists()


# ---------------------------------------------------------------------------
# main() parameters
# ---------------------------------------------------------------------------
def test_schedule_and_gtfs_zips_are_off_by_default(tmp_path, run_main):
    run_main(output_file_type="geojson")

    assert not list(tmp_path.rglob("schedule.csv"))
    assert not (tmp_path / "gtfs_zips").exists()


def test_schedule_and_gtfs_zips_can_be_kept(tmp_path, run_main):
    run_main(output_file_type="geojson", schedule=True, gtfs_zips=True)

    assert (tmp_path / "bus" / "alsa" / "schedule.csv").exists()
    assert (tmp_path / "gtfs_zips" / "feed.zip").exists()


def test_multimodal_is_rejected_before_any_download(tmp_path):
    with (
        patch.object(get_area, "find_area_boundary") as mock_boundary,
        pytest.raises(NotImplementedError),
    ):
        get_network.main("Valencia", output_path=str(tmp_path), multimodal=True)

    mock_boundary.assert_not_called()


def test_main_does_not_print_to_the_terminal(capsys, run_main):
    run_main(output_file_type="geojson")

    assert capsys.readouterr().out == ""


def test_package_logger_is_silent_unless_configured():
    handlers = logging.getLogger("multimodalpy").handlers
    assert any(isinstance(handler, logging.NullHandler) for handler in handlers)


# ---------------------------------------------------------------------------
# Column names (Shapefile's 10-character limit)
# ---------------------------------------------------------------------------
def test_column_names_fit_the_shapefile_limit():
    rename = process_osm.FINAL_EDGE_RENAME
    osm = [rename.get(column, column) for column in process_osm.FINAL_EDGE_COLUMNS]
    gtfs = list(get_network.GTFS_EDGE_RENAME.values())

    assert all(len(name) <= 10 for name in osm + gtfs)
    assert len(set(gtfs)) == len(gtfs)


def test_shapefile_keeps_the_column_names(tmp_path, run_main):
    run_main(output_file_type="shapefile")

    columns = gpd.read_file(tmp_path / "train" / "cercanias" / "edges.shp").columns
    assert {"from_node", "trips", "trp_rod_we"} <= set(columns)


# ---------------------------------------------------------------------------
# Exported OSM labels
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("footway;steps", "footway"),
        ("unclassified;track", "unclassified"),
        ("track", "track"),
        ("path", "path"),
        (None, None),
        ("", None),
        (" ; ", None),
    ],
)
def test_primary_highway_resolves_compound_tags_only(value, expected):
    assert process_osm._primary_highway(value) == expected


def test_osm_export_has_single_tags_numeric_maxspeed_and_raw_values():
    graph = nx.MultiDiGraph(crs="EPSG:4326")
    graph.add_node(1, x=0.0, y=0.0)
    graph.add_node(2, x=0.001, y=0.0)
    graph.add_node(3, x=0.002, y=0.0)
    common = {"length": 111.0, "oneway": False, "reversed": False}
    graph.add_edge(
        1,
        2,
        osmid=10,
        highway=["footway", "steps"],
        maxspeed=["30", "50"],
        geometry=LineString([(0, 0), (0.001, 0)]),
        **common,
    )
    graph.add_edge(
        2,
        3,
        osmid=11,
        highway="track",
        maxspeed="30 mph",
        geometry=LineString([(0.001, 0), (0.002, 0)]),
        **common,
    )

    _, edges = process_osm.build_final_osm_layers(
        graph, layer_id="walking", mode="walk"
    )
    edges = edges.set_index("edge_id")

    assert edges.loc[10, "highway"] == "footway"
    assert edges.loc[10, "hwy_raw"] == "footway;steps"
    assert edges.loc[11, "highway"] == "track"
    assert edges.loc[10, "maxspeed"] == 50.0
    assert edges.loc[10, "spd_raw"] == "30;50"
    assert edges.loc[11, "maxspeed"] == pytest.approx(48.28, abs=0.01)
    assert "from_node" in edges.columns


# ---------------------------------------------------------------------------
# Transport mode of a GTFS feed
# ---------------------------------------------------------------------------
def _feed(tmp_path, route_types):
    path = tmp_path / "feed.zip"
    rows = "\n".join(f"r{i},{route_type}" for i, route_type in enumerate(route_types))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("routes.txt", f"route_id,route_type\n{rows}\n")
    return path


@pytest.mark.parametrize(
    ("route_types", "expected"),
    [
        ([2], "train"),
        ([1, 0], "train"),
        ([3], "bus"),
        ([3, 3, 2], "bus"),
        ([2, 2, 3], "train"),
    ],
)
def test_transport_mode_follows_the_majority_route_type(
    tmp_path, route_types, expected
):
    assert process_gtfs.infer_transport_mode(_feed(tmp_path, route_types)) == expected


def test_unreadable_feed_is_treated_as_bus(tmp_path):
    missing_routes = tmp_path / "no_routes.zip"
    with zipfile.ZipFile(missing_routes, "w") as zf:
        zf.writestr("stops.txt", "stop_id\n")
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip")

    assert process_gtfs.infer_transport_mode(missing_routes) == "bus"
    assert process_gtfs.infer_transport_mode(corrupt) == "bus"
