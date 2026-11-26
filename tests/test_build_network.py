"""Tests for build_network(): the networks of a study area from local files."""

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import requests
from shapely.geometry import box

import multimodalpy
from multimodalpy import _gtfs, _osm, network
from multimodalpy._local_osm import OSMNetworkFileError
from test_gtfs import _write_feed
from test_local_osm import osm_xml

AREA = box(-0.45, 39.42, -0.35, 39.52)
FORMATS = ["geojson", "shapefile", "geopackage", "networkx", "matrix"]
OPTIONS = {
    "formats": FORMATS,
    "write_schedule": True,
    "gtfs_service_date": "2026-11-25",
    "gtfs_peak_periods": {"morning": (7, 10), "night": (22, 6)},
}


@pytest.fixture
def boundary() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        [
            {
                "area_name": "Valencia",
                "prov_codes": [46],
                "prov_names": ["Valencia/València"],
                "mun_codes": ["46250"],
                "mun_names": ["València"],
            }
        ],
        geometry=[AREA],
        crs="EPSG:4326",
    )


def _feed(path: Path, route: str, route_type: int = 3) -> Path:
    """A feed of one route through stops A, B and C, two trips."""
    trips = [(route, "WD", f"{route}_1"), (route, "WD", f"{route}_2")]
    stop_times = []
    for trip, start in ((f"{route}_1", 8), (f"{route}_2", 23)):
        for seq, stop in enumerate("ABC", start=1):
            time = f"{start:02d}:{seq * 5:02d}:00"
            stop_times.append((trip, time, time, stop, seq))
    return _write_feed(path, [(route, route, route_type)], trips, stop_times)


@pytest.fixture
def world(tmp_path, monkeypatch, boundary):
    """Offline stand-ins for the study area, Overpass, the NAP and the web.

    The OSM networks come from a hand-written extract built by osmnx as a
    download is; the NAP has one bus dataset; two addresses serve feeds, one
    a copy of the NAP's. Returns where the feeds are.
    """
    import osmnx as ox

    feeds = tmp_path / "feeds"
    feeds.mkdir()
    nap = _feed(feeds / "nap.zip", "L1")
    served = {
        "https://example.org/copy.zip": (feeds / "nap.zip").read_bytes(),
        "https://example.org/metro.zip": _feed(
            feeds / "metro.zip", "M1", 1
        ).read_bytes(),
    }
    xml = tmp_path / "streets.osm"
    xml.write_text(osm_xml(), encoding="utf-8")

    def fake_find_area(*args, **kwargs):
        return boundary

    def fake_download_osm(bnd, *, modes, output_crs, failures=None, **kwargs):
        results = {}
        for mode in modes:
            network_type, label = _osm._network_type(mode)
            graph = ox.graph_from_xml(
                xml, bidirectional=network_type == "walk", retain_all=True
            )
            nodes, edges = _osm.build_final_osm_layers(
                graph, layer_id=label, mode=network_type, output_crs=output_crs
            )
            results[label] = {
                "nodes": nodes,
                "edges": edges,
                "graph": graph,
                "unconnected_parts": [],
            }
        return results

    def fake_nap_zips(area_name, output_dir, *, report=None, **kwargs):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / "20261124_030000_EMT_Valencia.zip"
        path.write_bytes(nap.read_bytes())
        report.append(
            {
                "province_code": 46,
                "dataset_id": 965,
                "dataset_name": "EMT València",
                "file_id": 1,
                "file_name": path.name,
                "status": "downloaded",
                "path": str(path),
            }
        )
        return [path]

    class Answer:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            return None

    monkeypatch.setattr(network, "find_area", fake_find_area)
    monkeypatch.setattr(network, "download_osm", fake_download_osm)
    monkeypatch.setattr(_gtfs, "download_gtfs_nap_zips", fake_nap_zips)
    monkeypatch.setattr(requests, "get", lambda url, **kw: Answer(served[url]))
    return feeds


def download(tmp_path, **options):
    with pytest.warns(UserWarning, match="repeat another one"):
        return network.download_network(
            "Valencia",
            modes=["walking", "driving", "bus", "train"],
            output_dir=tmp_path / "download",
            gtfs_feeds={
                "copy": "https://example.org/copy.zip",
                "metro": "https://example.org/metro.zip",
            },
            keep_gtfs_zips=True,
            **options,
        )


@pytest.fixture
def offline(monkeypatch):
    """Fail the test if anything tries to reach the network."""

    def refuse(*args, **kwargs):
        raise AssertionError("build_network must not touch the network")

    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(requests, "post", refuse)


def _files(folder: Path) -> set[str]:
    return {
        str(path.relative_to(folder))
        for path in folder.rglob("*")
        if path.is_file() and "gtfs_zips" not in path.parts
    }


def _same_layer(a: Path, b: Path, layer: str | None = None) -> None:
    left = gpd.read_file(a, layer=layer) if layer else gpd.read_file(a)
    right = gpd.read_file(b, layer=layer) if layer else gpd.read_file(b)
    pd.testing.assert_frame_equal(
        pd.DataFrame(left.drop(columns="geometry")),
        pd.DataFrame(right.drop(columns="geometry")),
    )
    assert left.geometry.geom_equals_exact(right.geometry, 1e-9).all()


# ---------------------------------------------------------------------------
# Rebuilding a download
# ---------------------------------------------------------------------------
def test_rebuilding_a_download_gives_the_same_output(tmp_path, world, offline_later):
    first = download(tmp_path, **OPTIONS)
    assert first["gtfs_datasets"] == ["emt_valencia", "metro"]
    offline_later()
    with pytest.warns(UserWarning, match="repeat another one"):
        again = network.build_network(
            source_dir=tmp_path / "download",
            area_name="Valencia",
            output_dir=tmp_path / "again",
            **OPTIONS,
        )
    old, new = tmp_path / "download", tmp_path / "again"
    assert _files(new) == _files(old)
    for name in sorted(_files(old)):
        a, b = old / name, new / name
        if name in ("manifest.json", "gtfs_report.csv"):
            continue
        if name.endswith((".geojson", ".shp")):
            _same_layer(a, b)
        elif name.endswith(".gpkg"):
            import pyogrio

            for layer, geometry_type in pyogrio.list_layers(a):
                if geometry_type is None:
                    pd.testing.assert_frame_equal(_table(a, layer), _table(b, layer))
                else:
                    _same_layer(a, b, layer)
        elif name.endswith(".json"):
            assert json.loads(a.read_text()) == json.loads(b.read_text()), name
        elif name.endswith(".npz"):
            left, right = np.load(a, allow_pickle=True), np.load(b, allow_pickle=True)
            assert sorted(left.files) == sorted(right.files)
            for key in left.files:
                assert np.array_equal(left[key], right[key]), (name, key)
        elif name.endswith(".csv"):
            pd.testing.assert_frame_equal(pd.read_csv(a), pd.read_csv(b))
        # .dbf, .shx, .prj, .cpg: read with their .shp above
    for key in ("gtfs_datasets", "files", "gtfs_period_codes", "osm_modes"):
        assert again[key] == first[key], key
    statuses = [(row["dataset"], row["status"]) for row in first["gtfs_report"]]
    assert [(row["dataset"], row["status"]) for row in again["gtfs_report"]] == statuses
    assert again["built_with"] == "build_network"
    assert again["source_dir"] == str(old)


def _table(path: Path, layer: str) -> pd.DataFrame:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(path)) as connection:
        return pd.read_sql_query(f'select * from "{layer}"', connection)


@pytest.fixture
def offline_later(monkeypatch):
    """Call it to make the rest of the test fail on any network access."""

    def refuse(*args, **kwargs):
        raise AssertionError("build_network must not touch the network")

    def activate():
        monkeypatch.setattr(requests, "get", refuse)
        monkeypatch.setattr(requests, "post", refuse)

    return activate


def test_another_date_needs_no_download(tmp_path, world, offline_later):
    download(tmp_path)
    offline_later()
    with (
        pytest.warns(UserWarning, match="have no trips on 2026-11-28"),
        pytest.warns(UserWarning, match="none of the GTFS feeds could be written"),
    ):
        manifest = network.build_network(
            source_dir=tmp_path / "download",
            area_name="Valencia",
            output_dir=tmp_path / "saturday",
            gtfs_service_date="2026-11-28",  # a Saturday: no WD service
        )
    statuses = {row["dataset"]: row["status"] for row in manifest["gtfs_report"]}
    assert statuses == {
        "emt_valencia": "no_service_on_date",
        "copy": "no_service_on_date",
        "metro": "no_service_on_date",
    }
    assert sorted(manifest["osm_modes"]) == ["driving", "walking"]


# ---------------------------------------------------------------------------
# Combining a download with files of your own
# ---------------------------------------------------------------------------
def test_your_own_networks_join_or_replace_the_downloaded_ones(
    tmp_path, world, offline_later
):
    download(tmp_path)
    own = tmp_path / "own"
    own.mkdir()
    edges = gpd.read_file(tmp_path / "download" / "walking" / "edges.geojson")
    edges = edges[edges["highway"] != "footway"]  # my walking network: no footway
    edges.to_file(own / "walking.gpkg", layer="streets")
    bike = edges.rename(columns={"from_node": "u", "to_node": "v"})
    bike[["u", "v", "geometry"]].to_file(own / "bike.geojson")
    offline_later()
    with pytest.warns(UserWarning, match="2 GTFS feed\\(s\\) repeat another one"):
        manifest = network.build_network(
            {"walking": own / "walking.gpkg", "bicicleta": own / "bike.geojson"},
            gtfs_feeds={"tram": world / "metro.zip"},
            source_dir=tmp_path / "download",
            area_name="Valencia",
            output_dir=tmp_path / "mixed",
        )
    assert sorted(manifest["osm_modes"]) == ["bike", "driving", "walking"]
    assert manifest["osm_networks"]["walking"] == str(own / "walking.gpkg")
    assert manifest["osm_networks"]["driving"] == str(tmp_path / "download")
    walking = gpd.read_file(tmp_path / "mixed" / "walking" / "edges.geojson")
    assert "footway" not in set(walking["highway"])  # the file won
    bike = gpd.read_file(tmp_path / "mixed" / "bike" / "edges.geojson")
    assert len(bike) == len(edges) and bike["tts"].notna().all()
    # the own feed repeats metro: same routes and stops, so it is skipped
    statuses = {row["dataset"]: row["status"] for row in manifest["gtfs_report"]}
    assert statuses["tram"] == "duplicate"
    assert manifest["gtfs_feeds"] == [
        {"name": "tram", "source": str(world / "metro.zip")}
    ]


def test_networks_from_files_alone(tmp_path, world, offline):
    edges = gpd.read_file(_written_walking(tmp_path) / "walking" / "edges.geojson")
    edges.to_file(tmp_path / "walk.geojson")
    manifest = network.build_network(
        {"walking": tmp_path / "walk.geojson"},
        [world / "metro.zip"],
        area_name="Valencia",
        output_dir=tmp_path / "out",
        formats="matrix",
    )
    assert manifest["gtfs_datasets"] == ["metro"]
    assert manifest["source_dir"] is None
    assert (tmp_path / "out" / "manifest.json").exists()
    assert not (tmp_path / "out" / "gtfs_zips").exists()  # local files stay put
    matrix = multimodalpy.read_matrix(tmp_path / "out" / "walking")
    assert matrix.matrix.shape[0] > 0


def _written_walking(tmp_path: Path) -> Path:
    """A walking network written by multimodalpy (without Overpass)."""
    import osmnx as ox

    xml = tmp_path / "streets.osm"
    xml.write_text(osm_xml(), encoding="utf-8")
    graph = ox.graph_from_xml(xml, bidirectional=True, retain_all=True)
    nodes, edges = _osm.build_final_osm_layers(graph, layer_id="walking", mode="walk")
    folder = tmp_path / "written"
    (folder / "walking").mkdir(parents=True)
    nodes.to_file(folder / "walking" / "nodes.geojson")
    edges.to_file(folder / "walking" / "edges.geojson")
    return folder


def test_source_dir_without_gtfs_zips_warns(tmp_path, world, offline_later):
    download(tmp_path)
    import shutil

    shutil.rmtree(tmp_path / "download" / "gtfs_zips")
    offline_later()
    with pytest.warns(UserWarning, match="not the GTFS zips"):
        manifest = network.build_network(
            source_dir=tmp_path / "download",
            area_name="Valencia",
            output_dir=tmp_path / "again",
        )
    assert manifest["gtfs_datasets"] == []
    assert sorted(manifest["osm_modes"]) == ["driving", "walking"]


# ---------------------------------------------------------------------------
# Checks before anything is read
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("arguments", "match"),
    [
        ({"osm_networks": {"walking": "x.gpkg"}}, "needs the study area"),
        ({"area_name": "Valencia"}, "Nothing to build from"),
        (
            {"area_name": "Valencia", "gtfs_feeds": ["https://example.org/a.zip"]},
            "only reads local files",
        ),
        ({"area_name": "Valencia", "osm_networks": {"plane": "x"}}, "plane"),
        ({"area_name": "Valencia", "source_dir": "no/such/folder"}, "not a folder"),
        (
            {
                "area_name": "Valencia",
                "osm_networks": {"walking": "x"},
                "formats": "pdf",
            },
            "not valid",
        ),
        (
            {
                "area_name": "Valencia",
                "osm_networks": {"walking": "x"},
                "gtfs_hour_band_size": 5,
            },
            "equal bands",
        ),
    ],
)
def test_wrong_arguments_fail_before_reading(tmp_path, monkeypatch, arguments, match):
    def no_area(*args, **kwargs):
        raise AssertionError("checked before looking for the area")

    monkeypatch.setattr(network, "find_area", no_area)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=match):
        network.build_network(output_dir=tmp_path / "out", **arguments)


def test_a_missing_gtfs_feed_fails_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "find_area", lambda *a, **k: pytest.fail("too late"))
    with pytest.raises(FileNotFoundError, match="missing.zip"):
        network.build_network(
            gtfs_feeds=[tmp_path / "missing.zip"],
            area_name="Valencia",
            output_dir=tmp_path / "out",
        )


def test_output_dir_cannot_be_the_source_dir(tmp_path, world):
    download(tmp_path)
    with pytest.raises(ValueError, match="another folder than source_dir"):
        network.build_network(
            source_dir=tmp_path / "download",
            area_name="Valencia",
            output_dir=tmp_path / "download",
        )


def test_an_unreadable_network_is_explained(tmp_path, world):
    (tmp_path / "broken.geojson").write_text("not json")
    with pytest.raises(OSMNetworkFileError, match="broken.geojson"):
        network.build_network(
            {"walking": tmp_path / "broken.geojson"},
            area_name="Valencia",
            output_dir=tmp_path / "out",
        )


# ---------------------------------------------------------------------------
# Command line: multimodalpy build
# ---------------------------------------------------------------------------
def test_cli_build_passes_every_option(monkeypatch, capsys):
    from multimodalpy import _cli

    received: dict = {}

    def fake_build(osm_networks, gtfs_feeds, **kwargs):
        received.update(osm_networks=osm_networks, gtfs_feeds=gtfs_feeds, **kwargs)
        return {"output_dir": "final", "osm_modes": ["walking"], "gtfs_datasets": []}

    monkeypatch.setattr(network, "build_network", fake_build)
    _cli.main(
        [
            "build",
            "--source-dir", "dl",
            "--osm-network", "walking=streets.gpkg",
            "--osm-network", "bike=bike.geojson",
            "--gtfs-feed", "metro=feeds/metro.zip",
            "--gtfs-feed", "feeds/tram.zip",
            "--area", "Valencia",
            "--output-dir", "final",
            "--formats", "geojson", "matrix",
            "--gtfs-service-date", "2026-11-25",
            "--gtfs-peak-period", "morning=7-10",
            "--buffer-m", "200",
        ]
    )  # fmt: skip
    assert received["osm_networks"] == {
        "walking": "streets.gpkg",
        "bike": "bike.geojson",
    }
    assert received["gtfs_feeds"] == [("metro", "feeds/metro.zip"), "feeds/tram.zip"]
    assert received["source_dir"] == "dl"
    assert received["area_name"] == "Valencia"
    assert received["formats"] == ["geojson", "matrix"]
    assert received["gtfs_peak_periods"] == {"morning": (7, 10)}
    assert received["buffer_m"] == 200
    assert "Written to final/" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--source-dir", "dl"], "give the study area"),
        (["--area", "Valencia"], "give --source-dir, --osm-network or --gtfs-feed"),
        (["--area", "Valencia", "--osm-network", "streets.gpkg"], "MODE=PATH"),
    ],
)
def test_cli_build_explains_missing_options(monkeypatch, capsys, arguments, message):
    from multimodalpy import _cli

    monkeypatch.setattr(network, "build_network", lambda *a, **k: pytest.fail())
    with pytest.raises(SystemExit):
        _cli.main(["build", *arguments])
    assert message in capsys.readouterr().err


def test_cli_without_build_still_downloads(monkeypatch):
    from multimodalpy import _cli

    called = []
    monkeypatch.setattr(
        network, "download_network", lambda **kwargs: called.append(kwargs) or {}
    )
    _cli.main(["--area", "Valencia", "--modes", "walking"])
    assert called and called[0]["modes"] == ["walking"]
