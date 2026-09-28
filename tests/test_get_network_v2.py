"""Tests for ``multimodalpy.get_network``.

Nothing here touches the network: the three backends that ``main()`` calls
(``find_area_boundary``, ``download_osm_layers`` and ``download_gtfs_layers``)
are replaced by small fakes that return tiny in-memory layers. What is tested
is get_network's own logic: mode splitting, argument validation, column
renaming, file writing per format, the manifest and the CLI.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, box

from multimodalpy import get_network

# Captured at import time, before any test monkeypatches it.
REAL_FIND_AREA_BOUNDARY = get_network.get_area.find_area_boundary


# ---------------------------------------------------------------------------
# Small in-memory layers used by the fakes
# ---------------------------------------------------------------------------
def make_osm_result() -> dict:
    """One OSM 'mode' result, shaped like ``get_area.download_osm_layers``."""
    nodes = gpd.GeoDataFrame(
        {"node_id": ["n1", "n2"], "node_role": ["intersection", "endpoint"]},
        geometry=[Point(-0.40, 39.40), Point(-0.39, 39.40)],
        crs="EPSG:4326",
    )
    edges = gpd.GeoDataFrame(
        {
            "edge_id": [1],
            "from_node": ["n1"],
            "to_node_id": ["n2"],
            "length": [100.0],
            "tts": [72.0],
        },
        geometry=[LineString([(-0.40, 39.40), (-0.39, 39.40)])],
        crs="EPSG:4326",
    )
    graph = nx.MultiDiGraph()
    graph.add_edge("n1", "n2", length=100.0, highway=["residential", "tertiary"])
    return {"nodes": nodes, "edges": edges, "graph": graph}


def make_gtfs_result(mode: str = "bus") -> dict:
    """One GTFS dataset result, shaped like ``get_area.download_gtfs_layers``."""
    nodes = gpd.GeoDataFrame(
        {
            "node_id": ["s1", "s2"],
            "stop_id": ["s1", "s2"],
            "stop_name": ["Stop A", "Stop B"],
            "not_kept": [1, 2],
        },
        geometry=[Point(-0.40, 39.40), Point(-0.39, 39.41)],
        crs="EPSG:4326",
    )
    edges = gpd.GeoDataFrame(
        {
            "edge_id": ["ds1_s1_s2"],
            "from_node_id": ["s1"],
            "to_node_id": ["s2"],
            "route_id": ["R1"],
            "trip_count": [12],
            "travel_time_seconds_mean": [95.0],
            "not_in_whitelist": ["x"],
        },
        geometry=[LineString([(-0.40, 39.40), (-0.39, 39.41)])],
        crs="EPSG:4326",
    )
    schedule = pd.DataFrame(
        {
            "edge_id": ["ds1_s1_s2"],
            "trip_id": ["t1"],
            "departure_time": ["08:00:00"],
        }
    )
    return {"nodes": nodes, "edges": edges, "schedule": schedule, "mode": mode}


@pytest.fixture
def boundary() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        [{"area_name": "Valencia", "province_code": 46}],
        geometry=[box(-0.5, 39.3, -0.2, 39.6)],
        crs="EPSG:4326",
    )


@pytest.fixture
def backends(monkeypatch, boundary):
    """Replace the three backends of ``main()`` with offline fakes.

    Returns a dict that records every call. Tests can set
    ``backends["gtfs_result"]`` or ``backends["gtfs_error"]`` to change what
    the fake GTFS download does.
    """
    get_area = get_network.get_area
    calls: dict = {
        "boundary": [],
        "osm": [],
        "gtfs": [],
        "gtfs_result": {},
        "gtfs_error": None,
    }

    def fake_find_area_boundary(area_name, boundaries_path=None, **kwargs):
        calls["boundary"].append(
            {"area_name": area_name, "boundaries_path": boundaries_path, **kwargs}
        )
        return boundary

    def fake_download_osm_layers(bnd, *, modes, output_crs, **kwargs):
        calls["osm"].append(
            {"boundary": bnd, "modes": list(modes), "output_crs": output_crs}
        )
        results = {}
        for mode in modes:
            network_type = get_area.OSM_NETWORK_TYPES[get_area.normalize_name(mode)]
            label = get_area.NETWORK_TYPE_TO_LAYER_LABEL[network_type]
            results[label] = make_osm_result()
        return results

    def fake_download_gtfs_layers(area_name, bnd, zip_dir, **kwargs):
        calls["gtfs"].append(
            {"area_name": area_name, "zip_dir": Path(zip_dir), **kwargs}
        )
        # The real function leaves the downloaded ZIPs in zip_dir.
        Path(zip_dir).mkdir(parents=True, exist_ok=True)
        (Path(zip_dir) / "feed.zip").write_bytes(b"not a real zip")
        if calls["gtfs_error"] is not None:
            raise calls["gtfs_error"]
        return calls["gtfs_result"]

    monkeypatch.setattr(get_area, "find_area_boundary", fake_find_area_boundary)
    monkeypatch.setattr(get_area, "download_osm_layers", fake_download_osm_layers)
    monkeypatch.setattr(get_area, "download_gtfs_layers", fake_download_gtfs_layers)
    return calls


# ---------------------------------------------------------------------------
# Shapefile-safe column names for the GTFS edges layer
# ---------------------------------------------------------------------------
def test_gtfs_edge_rename_fits_shapefile_limit():
    too_long = {
        new: old for old, new in get_network.GTFS_EDGE_RENAME.items() if len(new) > 10
    }
    assert too_long == {}


def test_gtfs_edge_rename_targets_are_unique():
    targets = list(get_network.GTFS_EDGE_RENAME.values())
    assert len(targets) == len(set(targets))


def test_gtfs_edge_rename_expected_examples():
    rename = get_network.GTFS_EDGE_RENAME
    assert rename["from_node_id"] == "from_node"
    assert rename["trip_count"] == "trips"
    assert rename["travel_time_seconds_mean"] == "tts_m"
    assert rename["travel_time_seconds_mean_peak_am"] == "tts_m_pam"
    assert rename["trip_count_weekend"] == "trips_we"
    assert rename["travel_time_seconds_mean_rest_of_day_weekday"] == "tm_rod_wd"
    assert rename["trip_count_peak_pm_weekend"] == "trp_ppm_we"


# ---------------------------------------------------------------------------
# _split_modes
# ---------------------------------------------------------------------------
def test_split_modes_separates_osm_and_gtfs():
    osm_modes, nap_modes = get_network._split_modes(["walking", "bus", "train"])
    assert osm_modes == ["walking"]
    assert nap_modes == {1, 2}


@pytest.mark.parametrize(
    ("modes", "expected_osm", "expected_nap"),
    [
        (["  Walking "], ["walking"], set()),
        (["BIKE", "Bus"], ["bike"], {1}),
        (["coche", "caminable"], ["coche", "caminable"], set()),  # synonyms
        (["train"], [], {2}),
        ([], [], set()),
    ],
)
def test_split_modes_is_case_and_whitespace_insensitive(
    modes, expected_osm, expected_nap
):
    assert get_network._split_modes(modes) == (expected_osm, expected_nap)


def test_split_modes_rejects_unknown_mode():
    with pytest.raises(ValueError, match="Modo desconocido 'helicopter'"):
        get_network._split_modes(["walking", "helicopter"])


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def test_json_safe_handles_nan_lists_and_scalars():
    json_safe = get_network._json_safe
    assert json_safe(float("nan")) is None
    assert json_safe(None) is None
    assert json_safe("text") == "text"
    assert json_safe(3) == 3
    assert json.loads(json_safe(["a", "b"])) == ["a", "b"]
    assert json_safe({"clave": "áé"}) == '{"clave": "áé"}'  # keeps accents
    assert json_safe({1, 2}) == "{1, 2}"  # not JSON-serialisable -> str()


def test_relative_name_uses_forward_slashes(tmp_path):
    path = tmp_path / "walking" / "nodes.geojson"
    assert get_network._relative_name(path, tmp_path) == "walking/nodes.geojson"


# ---------------------------------------------------------------------------
# _write_layers
# ---------------------------------------------------------------------------
def test_write_layers_geojson_uses_layer_paths_and_skips_empty(tmp_path):
    osm = make_osm_result()
    layers = {
        "osm_walking_nodes": osm["nodes"],
        "no_path_entry": osm["edges"],  # falls back to the layer name
        "empty": gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"),
        "missing": None,
    }
    written = get_network._write_layers(
        layers,
        tmp_path,
        "geojson",
        area_slug="valencia",
        layer_paths={"osm_walking_nodes": "walking/nodes"},
    )
    assert written == ["walking/nodes.geojson", "no_path_entry.geojson"]
    assert (tmp_path / "walking" / "nodes.geojson").is_file()
    assert not (tmp_path / "empty.geojson").exists()
    assert not (tmp_path / "missing.geojson").exists()


def test_write_layers_shapefile_keeps_short_column_names(tmp_path):
    nodes = make_osm_result()["nodes"]
    written = get_network._write_layers(
        {"osm_walking_nodes": nodes},
        tmp_path,
        "shapefile",
        area_slug="valencia",
        layer_paths={"osm_walking_nodes": "walking/nodes"},
    )
    assert written == ["walking/nodes.shp"]
    back = gpd.read_file(tmp_path / "walking" / "nodes.shp")
    assert {"node_id", "node_role"} <= set(back.columns)
    assert len(back) == 2


def test_write_layers_geopackage_puts_layers_in_one_file(tmp_path):
    osm = make_osm_result()
    written = get_network._write_layers(
        {"nodes": osm["nodes"], "edges": osm["edges"]},
        tmp_path,
        "geopackage",
        area_slug="valencia",
        layer_paths={},
    )
    assert written == ["valencia.gpkg::nodes", "valencia.gpkg::edges"]
    gpkg = tmp_path / "valencia.gpkg"
    assert len(gpd.read_file(gpkg, layer="nodes")) == 2
    assert len(gpd.read_file(gpkg, layer="edges")) == 1


def test_write_layers_rejects_unsupported_format(tmp_path):
    nodes = make_osm_result()["nodes"]
    with pytest.raises(ValueError, match="Formato no soportado"):
        get_network._write_layers(
            {"nodes": nodes},
            tmp_path,
            "csv",
            area_slug="valencia",
            layer_paths={},
        )


# ---------------------------------------------------------------------------
# _write_schedule_tables
# ---------------------------------------------------------------------------
def test_write_schedule_tables_csv_for_non_geopackage(tmp_path):
    schedule = make_gtfs_result()["schedule"]
    written = get_network._write_schedule_tables(
        {
            "gtfs_ds1_schedule": schedule,
            "gtfs_empty_schedule": pd.DataFrame(),
            "gtfs_none_schedule": None,
        },
        tmp_path,
        "geojson",
        area_slug="valencia",
        layer_paths={"gtfs_ds1_schedule": "bus/ds1/schedule"},
    )
    assert written == ["bus/ds1/schedule.csv"]
    back = pd.read_csv(tmp_path / "bus" / "ds1" / "schedule.csv")
    assert back["trip_id"].tolist() == ["t1"]


def test_write_schedule_tables_geopackage_uses_plain_sqlite_table(tmp_path):
    osm = make_osm_result()
    get_network._write_layers(
        {"nodes": osm["nodes"]},
        tmp_path,
        "geopackage",
        area_slug="valencia",
        layer_paths={},
    )
    written = get_network._write_schedule_tables(
        {"gtfs_ds1_schedule": make_gtfs_result()["schedule"]},
        tmp_path,
        "geopackage",
        area_slug="valencia",
        layer_paths={},
    )
    assert written == ["valencia.gpkg::gtfs_ds1_schedule"]

    gpkg = tmp_path / "valencia.gpkg"
    with closing(sqlite3.connect(gpkg)) as connection:
        rows = connection.execute("SELECT trip_id FROM gtfs_ds1_schedule").fetchall()
    assert rows == [("t1",)]
    # The spatial layer written first must still be readable.
    assert len(gpd.read_file(gpkg, layer="nodes")) == 2


# ---------------------------------------------------------------------------
# NetworkX / JSON exports
# ---------------------------------------------------------------------------
def _links(data: dict) -> list[dict]:
    # networkx renamed "links" to "edges" in node_link_data (3.4+).
    return data.get("links") or data.get("edges")


def test_osm_graph_to_json_serialises_list_attributes(tmp_path):
    graph = make_osm_result()["graph"]
    path = get_network._osm_graph_to_json(graph, tmp_path / "walking" / "graph.json")

    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {node["id"] for node in data["nodes"]} == {"n1", "n2"}
    links = _links(data)
    assert len(links) == 1
    assert links[0]["length"] == 100.0
    assert json.loads(links[0]["highway"]) == ["residential", "tertiary"]


def test_gtfs_to_graph_json_builds_stop_graph(tmp_path):
    result = make_gtfs_result()
    path = get_network._gtfs_to_graph_json(
        result["nodes"], result["edges"], tmp_path / "bus" / "ds1" / "graph.json"
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = {node["id"]: node for node in data["nodes"]}
    assert set(nodes) == {"s1", "s2"}
    assert nodes["s1"]["stop_name"] == "Stop A"
    assert nodes["s1"]["x"] == pytest.approx(-0.40)
    assert nodes["s1"]["y"] == pytest.approx(39.40)

    (link,) = _links(data)
    assert (link["source"], link["target"]) == ("s1", "s2")
    assert link["route_id"] == "R1"
    assert float(link["trip_count"]) == 12
    assert float(link["travel_time_seconds_mean"]) == pytest.approx(95.0)
    assert link["hour_band"] is None  # column absent -> null, not an error


# ---------------------------------------------------------------------------
# main(): argument validation (fails before any backend is called)
# ---------------------------------------------------------------------------
def test_main_multimodal_is_not_implemented(tmp_path):
    with pytest.raises(NotImplementedError):
        get_network.main("Valencia", output_path=tmp_path, multimodal=True)


@pytest.mark.parametrize("bad_type", ["csv", "gpkg", ["geojson", "csv"]])
def test_main_rejects_unknown_output_file_type(tmp_path, bad_type):
    with pytest.raises(ValueError, match="no valido"):
        get_network.main("Valencia", output_path=tmp_path, output_file_type=bad_type)


def test_main_rejects_empty_output_file_type_list(tmp_path):
    with pytest.raises(ValueError, match="no puede estar vacio"):
        get_network.main("Valencia", output_path=tmp_path, output_file_type=[])


def test_main_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError, match="Modo desconocido"):
        get_network.main("Valencia", modes=["helicopter"], output_path=tmp_path)


# ---------------------------------------------------------------------------
# main(): OSM only
# ---------------------------------------------------------------------------
def test_main_osm_geojson_writes_one_folder_per_mode(tmp_path, backends):
    out = tmp_path / "out"
    manifest = get_network.main("Valencia", modes=["walking", "bike"], output_path=out)

    expected = {
        "study_area_boundary.geojson",
        "walking/nodes.geojson",
        "walking/edges.geojson",
        "bike/nodes.geojson",
        "bike/edges.geojson",
    }
    assert set(manifest["files"]) == expected
    for relative in expected:
        assert (out / relative).is_file()

    assert manifest["area_name"] == "Valencia"
    assert manifest["modes"] == ["walking", "bike"]
    assert manifest["osm_modes"] == ["walking", "bike"]
    assert manifest["gtfs_datasets"] == []
    assert manifest["crs"] == "EPSG:4326"
    assert manifest["output_file_type"] == "geojson"
    assert manifest["output_path"] == str(out)
    assert "files_by_format" not in manifest
    assert "gtfs_status" not in manifest

    assert len(backends["osm"]) == 1
    assert backends["osm"][0]["modes"] == ["walking", "bike"]
    assert backends["gtfs"] == []


def test_main_accepts_a_single_mode_as_string(tmp_path, backends):
    manifest = get_network.main("Valencia", modes="walking", output_path=tmp_path)
    assert manifest["modes"] == ["walking"]
    assert "walking/nodes.geojson" in manifest["files"]


def test_main_maps_spanish_synonyms_to_canonical_folders(tmp_path, backends):
    manifest = get_network.main("Valencia", modes=["coche"], output_path=tmp_path)
    assert "driving/nodes.geojson" in manifest["files"]
    assert "driving/edges.geojson" in manifest["files"]


def test_main_passes_boundary_arguments_and_reprojects_output(tmp_path, backends):
    manifest = get_network.main(
        "Valencia",
        modes=["walking"],
        output_path=tmp_path,
        boundaries_path="my_boundaries.geojson",
        area_code="46250",
        crs="EPSG:25830",
        output_file_type="geopackage",
    )

    call = backends["boundary"][0]
    assert call["area_name"] == "Valencia"
    assert call["boundaries_path"] == "my_boundaries.geojson"
    assert call["area_code"] == "46250"
    assert call["target_crs"] == "EPSG:4326"  # the search always runs in WGS84
    assert backends["osm"][0]["output_crs"] == "EPSG:25830"

    gpkg = tmp_path / "valencia.gpkg"
    assert f"{gpkg.name}::study_area_boundary" in manifest["files"]
    layer = gpd.read_file(gpkg, layer="study_area_boundary")
    assert layer.crs.to_epsg() == 25830


def test_main_with_real_boundary_file(tmp_path, backends, monkeypatch):
    """End to end through the real ``find_area_boundary`` (OSM still faked)."""
    monkeypatch.setattr(
        get_network.get_area, "find_area_boundary", REAL_FIND_AREA_BOUNDARY
    )
    boundaries = gpd.GeoDataFrame(
        {"nombre": ["Segovia", "Madrid"], "NATCODE": ["34074040136", "34132828079"]},
        geometry=[box(-4.2, 40.9, -4.0, 41.0), box(-3.8, 40.3, -3.5, 40.6)],
        crs="EPSG:4326",
    )
    path = tmp_path / "boundaries.geojson"
    boundaries.to_file(path, driver="GeoJSON")

    manifest = get_network.main(
        "Segovia",
        modes=["walking"],
        output_path=tmp_path / "out",
        boundaries_path=path,
    )

    assert "walking/nodes.geojson" in manifest["files"]
    used = backends["osm"][0]["boundary"].iloc[0]
    assert used["matched_value"] == "Segovia"
    assert used["province_code"] == 40


# ---------------------------------------------------------------------------
# main(): output formats
# ---------------------------------------------------------------------------
def test_main_multiple_formats_download_once_and_write_each(tmp_path, backends):
    manifest = get_network.main(
        "Valencia",
        modes=["walking"],
        output_path=tmp_path,
        output_file_type=["geojson", "geopackage", "GeoJSON"],  # duplicate ignored
    )

    assert manifest["output_file_type"] == ["geojson", "geopackage"]
    by_format = manifest["files_by_format"]
    assert set(by_format) == {"geojson", "geopackage"}
    assert by_format["geojson"] == [
        "study_area_boundary.geojson",
        "walking/nodes.geojson",
        "walking/edges.geojson",
    ]
    assert by_format["geopackage"] == [
        "valencia.gpkg::study_area_boundary",
        "valencia.gpkg::osm_walking_nodes",
        "valencia.gpkg::osm_walking_edges",
    ]
    assert manifest["files"] == by_format["geojson"] + by_format["geopackage"]
    assert len(backends["osm"]) == 1  # the pipeline is not repeated per format


def test_main_single_format_given_as_list_returns_a_list(tmp_path, backends):
    manifest = get_network.main(
        "Valencia",
        modes=["walking"],
        output_path=tmp_path,
        output_file_type=["geojson"],
    )
    assert manifest["output_file_type"] == ["geojson"]
    assert set(manifest["files_by_format"]) == {"geojson"}


def test_main_networkx_writes_graph_json(tmp_path, backends):
    manifest = get_network.main(
        "Valencia",
        modes=["walking"],
        output_path=tmp_path,
        output_file_type="networkx",
    )
    assert set(manifest["files"]) == {
        "study_area_boundary.geojson",
        "walking/graph.json",
    }
    data = json.loads((tmp_path / "walking" / "graph.json").read_text("utf-8"))
    assert len(data["nodes"]) == 2


def test_main_shapefile_writes_layer_files(tmp_path, backends):
    manifest = get_network.main(
        "Valencia",
        modes=["walking"],
        output_path=tmp_path,
        output_file_type="shapefile",
    )
    assert "walking/nodes.shp" in manifest["files"]
    assert (tmp_path / "walking" / "nodes.shp").is_file()


# ---------------------------------------------------------------------------
# main(): GTFS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("mode", "nap_id"), [("bus", 1), ("train", 2)])
def test_main_gtfs_writes_filtered_and_renamed_layers(tmp_path, backends, mode, nap_id):
    backends["gtfs_result"] = {"ds1": make_gtfs_result(mode)}
    manifest = get_network.main(
        "Valencia", modes=[mode], output_path=tmp_path, api_key="secret"
    )

    assert manifest["gtfs_datasets"] == ["ds1"]
    assert manifest["osm_modes"] == []
    assert backends["osm"] == []
    assert "gtfs_status" not in manifest
    assert set(manifest["files"]) == {
        "study_area_boundary.geojson",
        f"{mode}/ds1/nodes.geojson",
        f"{mode}/ds1/edges.geojson",
    }

    kwargs = backends["gtfs"][0]
    assert kwargs["modes"] == {nap_id}
    assert kwargs["api_key"] == "secret"
    assert kwargs["output_crs"] == "EPSG:4326"

    # Only whitelisted node columns are kept.
    nodes = gpd.read_file(tmp_path / mode / "ds1" / "nodes.geojson")
    assert set(nodes.columns) == {"node_id", "stop_name", "geometry"}

    # Edge columns are whitelisted and then renamed to shapefile-safe names.
    edges = gpd.read_file(tmp_path / mode / "ds1" / "edges.geojson")
    assert {"edge_id", "from_node", "to_node_id", "trips", "tts_m"} <= set(
        edges.columns
    )
    assert "from_node_id" not in edges.columns
    assert "not_in_whitelist" not in edges.columns


def test_main_gtfs_schedule_table_only_written_when_requested(tmp_path, backends):
    backends["gtfs_result"] = {"ds1": make_gtfs_result()}

    without = get_network.main("Valencia", modes=["bus"], output_path=tmp_path / "a")
    assert "bus/ds1/schedule.csv" not in without["files"]
    assert backends["gtfs"][0]["include_schedule_table"] is False

    with_schedule = get_network.main(
        "Valencia", modes=["bus"], output_path=tmp_path / "b", schedule=True
    )
    assert "bus/ds1/schedule.csv" in with_schedule["files"]
    assert (tmp_path / "b" / "bus" / "ds1" / "schedule.csv").is_file()
    assert backends["gtfs"][1]["include_schedule_table"] is True


@pytest.mark.parametrize("keep_zips", [False, True])
def test_main_removes_gtfs_zips_unless_asked_to_keep_them(
    tmp_path, backends, keep_zips
):
    backends["gtfs_result"] = {"ds1": make_gtfs_result()}
    get_network.main(
        "Valencia", modes=["bus"], output_path=tmp_path, gtfs_zips=keep_zips
    )
    assert (tmp_path / "gtfs_zips").exists() is keep_zips


def test_main_keeps_osm_layers_when_gtfs_download_fails(tmp_path, backends, caplog):
    backends["gtfs_error"] = RuntimeError("NAP down")
    with caplog.at_level(logging.WARNING):
        manifest = get_network.main(
            "Valencia", modes=["walking", "bus"], output_path=tmp_path
        )

    assert manifest["gtfs_status"] == "RuntimeError: NAP down"
    assert manifest["gtfs_datasets"] == []
    assert "walking/nodes.geojson" in manifest["files"]
    assert "No se pudieron descargar los datos GTFS" in caplog.text


def test_main_reports_when_nap_has_no_published_datasets(tmp_path, backends):
    backends["gtfs_result"] = {}
    manifest = get_network.main("Valencia", modes=["bus"], output_path=tmp_path)
    assert manifest["gtfs_status"] == "sin conjuntos de datos publicados"
    assert manifest["files"] == ["study_area_boundary.geojson"]


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------
def test_cli_forwards_arguments_to_main(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(get_network, "main", lambda **kwargs: captured.update(kwargs))

    get_network._cli(
        [
            "--area", "Valencia",
            "--modes", "walking", "bus",
            "--output", "out",
            "--crs", "EPSG:25830",
            "--output-file-type", "geojson", "geopackage",
            "--area-code", "46250",
            "--gtfs-hour-range", "7", "9",
            "--schedule",
            "--gtfs-zips",
        ]
    )  # fmt: skip

    assert captured["area_name"] == "Valencia"
    assert captured["modes"] == ["walking", "bus"]
    assert captured["output_path"] == "out"
    assert captured["crs"] == "EPSG:25830"
    assert captured["output_file_type"] == ["geojson", "geopackage"]
    assert captured["area_code"] == "46250"
    assert captured["gtfs_hour_range"] == (7, 9)
    assert captured["schedule"] is True
    assert captured["gtfs_zips"] is True


def test_cli_defaults(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(get_network, "main", lambda **kwargs: captured.update(kwargs))

    get_network._cli(["--area", "Valencia"])

    assert captured["modes"] == ["walking"]
    assert captured["output_path"] == "output"
    assert captured["crs"] == "EPSG:4326"
    assert captured["output_file_type"] == ["geojson"]
    assert captured["boundaries_path"] is None
    assert captured["gtfs_hour_range"] is None
    assert captured["schedule"] is False
    assert captured["gtfs_zips"] is False


def test_cli_requires_area_and_valid_output_type():
    with pytest.raises(SystemExit):
        get_network._cli([])
    with pytest.raises(SystemExit):
        get_network._cli(["--area", "Valencia", "--output-file-type", "csv"])
