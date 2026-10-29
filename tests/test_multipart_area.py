"""Multi-part municipalities: the roads joining the parts are added.

A synthetic municipality near Donostia/San Sebastián, with no download
(``osmnx.graph_from_polygon`` is replaced by a fake that returns a fixed
street graph):

- part 1 (main, 2 x 2 km) and part 2 (an exclave, 1 x 1 km) are joined
  only by one-way roads that run outside the municipality: north road
  1 -> 2, south road 2 -> 1, each with three junctions outside
  (``truncate_by_edge`` already keeps the outside junctions next to the
  municipality; the middle stretch is what was missing);
- a dead-end street hangs off the north road, also outside: it must not
  be added;
- part 3 is a small islet with streets but no road to the rest.
"""

from __future__ import annotations

import math

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pytest
from shapely.geometry import MultiPolygon, Point, box

from multimodalpy import get_area, get_network, process_osm

LON0, LAT0 = -2.0, 43.28
KM_LON = 1 / (111.32 * math.cos(math.radians(LAT0)))
KM_LAT = 1 / 110.57


def _xy(x_km: float, y_km: float) -> tuple[float, float]:
    return LON0 + x_km * KM_LON, LAT0 + y_km * KM_LAT


def _box(x0, y0, x1, y1):
    return box(*_xy(x0, y0), *_xy(x1, y1))


MAIN = _box(0, 0, 2, 2)
EXCLAVE = _box(4, 0, 5, 1)
ISLET = _box(0, -3, 0.3, -2.7)

NODES = {
    # main part
    1: (0.5, 0.5),
    2: (1.5, 0.5),
    3: (1.5, 1.5),
    4: (1.9, 0.5),
    # exclave
    21: (4.2, 0.5),
    22: (4.8, 0.5),
    # outside: north road, south road, dead end off the north road
    31: (2.5, 0.8),
    36: (3.0, 0.8),
    34: (3.5, 0.8),
    32: (2.5, 0.2),
    37: (3.0, 0.2),
    35: (3.5, 0.2),
    33: (3.0, 1.6),
    # islet
    41: (0.1, -2.9),
    42: (0.2, -2.8),
}
NORTH_ROAD, SOUTH_ROAD, DEAD_END = 900, 901, 902


def _street_graph() -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph(crs="EPSG:4326")
    for node, (x_km, y_km) in NODES.items():
        x, y = _xy(x_km, y_km)
        graph.add_node(node, x=x, y=y, street_count=2)

    def street(u, v, osmid, *, oneway=False):
        x0, y0 = NODES[u]
        x1, y1 = NODES[v]
        attrs = {
            "osmid": osmid,
            "highway": "residential",
            "oneway": oneway,
            "length": 1000 * math.hypot(x1 - x0, y1 - y0),
        }
        graph.add_edge(u, v, reversed=False, **attrs)
        if not oneway:
            graph.add_edge(v, u, reversed=True, **attrs)

    street(1, 2, 100)
    street(2, 3, 101)
    street(2, 4, 102)
    street(21, 22, 200)
    street(4, 31, NORTH_ROAD, oneway=True)
    street(31, 36, NORTH_ROAD, oneway=True)
    street(36, 34, NORTH_ROAD, oneway=True)
    street(34, 21, NORTH_ROAD, oneway=True)
    street(21, 35, SOUTH_ROAD, oneway=True)
    street(35, 37, SOUTH_ROAD, oneway=True)
    street(37, 32, SOUTH_ROAD, oneway=True)
    street(32, 4, SOUTH_ROAD, oneway=True)
    street(36, 33, DEAD_END)
    street(41, 42, 400)
    return graph


@pytest.fixture
def downloads(monkeypatch):
    """Replace the OSM download; record the polygon each call asked for."""
    calls: list = []

    def fake_graph_from_polygon(polygon, **kwargs):
        calls.append({"polygon": polygon, **kwargs})
        # Like osmnx: only the streets of the requested area.
        return ox.truncate.truncate_graph_polygon(
            _street_graph(), polygon, truncate_by_edge=kwargs["truncate_by_edge"]
        )

    monkeypatch.setattr(ox, "graph_from_polygon", fake_graph_from_polygon)
    return calls


def _boundary(geometry) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["Test"]}, geometry=[geometry], crs="EPSG:4326")


def _layers(geometry, **kwargs):
    with pytest.warns(UserWarning) if kwargs.pop("expect_warning", False) else _no_op():
        result = get_area.download_osm_layers(
            _boundary(geometry), modes=["driving"], **kwargs
        )
    return result["driving"]


class _no_op:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _routable(edges) -> nx.DiGraph:
    return nx.DiGraph(list(zip(edges["from_node"], edges["to_node"], strict=True)))


# ---------------------------------------------------------------------------
# Joining the parts
# ---------------------------------------------------------------------------
def test_parts_are_joined_in_both_directions(downloads):
    out = _layers(MultiPolygon([MAIN, EXCLAVE]))
    network = _routable(out["edges"])

    assert nx.has_path(network, 1, 22)  # main part -> exclave
    assert nx.has_path(network, 22, 1)  # exclave -> main part


def test_only_the_connecting_roads_are_added_and_marked(downloads):
    edges = _layers(MultiPolygon([MAIN, EXCLAVE]))["edges"]

    connectors = set(edges.loc[edges["connector"], "edge_id"].astype(int))
    assert connectors == {NORTH_ROAD, SOUTH_ROAD}
    assert DEAD_END not in set(edges["edge_id"].astype(int))
    # Streets inside the municipality are not connectors.
    inside = edges[edges["edge_id"].astype(int).isin({100, 101, 102, 200})]
    assert not inside["connector"].any()


def test_network_is_downloaded_over_the_hull_of_the_parts(downloads):
    _layers(MultiPolygon([MAIN, EXCLAVE]))

    (call,) = downloads
    gap = Point(_xy(3.0, 0.5))  # between the parts, outside both
    assert call["polygon"].contains(gap)
    assert call["polygon"].contains(MAIN) and call["polygon"].contains(EXCLAVE)
    assert call["retain_all"] is True


def test_part_without_road_access_is_reported_not_fatal(downloads):
    with pytest.warns(UserWarning, match=r"part\(s\) 3 "):
        out = get_area.download_osm_layers(
            _boundary(MultiPolygon([MAIN, EXCLAVE, ISLET])), modes=["driving"]
        )["driving"]

    assert out["unconnected_parts"] == [3]  # numbered by size: the islet is 3rd
    network = _routable(out["edges"])
    assert nx.has_path(network, 1, 22)  # the exclave is still joined
    assert 41 in network  # the islet's streets are kept


# ---------------------------------------------------------------------------
# Unchanged behaviour
# ---------------------------------------------------------------------------
def test_single_polygon_area_is_downloaded_as_before(downloads):
    out = _layers(MAIN)

    (call,) = downloads
    assert call["polygon"].equals(MAIN)
    assert out["unconnected_parts"] == []
    assert not out["edges"]["connector"].any()


def test_connect_parts_false_leaves_the_parts_separate(downloads):
    area = MultiPolygon([MAIN, EXCLAVE])
    out = _layers(area, connect_parts=False)

    (call,) = downloads
    assert call["polygon"].equals(area)
    assert not out["edges"]["connector"].any()
    network = _routable(out["edges"])
    assert not nx.has_path(network, 1, 22)  # what happened before this fix


def test_connector_column_is_false_for_graphs_without_the_attribute():
    graph = _street_graph().subgraph([1, 2, 3]).copy()
    _, edges = process_osm.build_final_osm_layers(graph, layer_id="d", mode="drive")
    assert edges["connector"].tolist() == [False] * len(edges)


# ---------------------------------------------------------------------------
# main() and the CLI
# ---------------------------------------------------------------------------
def test_main_connects_by_default_and_reports_unconnected_parts(tmp_path, monkeypatch):
    seen = {}
    boundary = _boundary(MultiPolygon([MAIN, EXCLAVE, ISLET]))
    monkeypatch.setattr(get_area, "find_area_boundary", lambda *a, **k: boundary)
    real = get_area.download_osm_layers

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(get_area, "download_osm_layers", spy)
    monkeypatch.setattr(
        ox,
        "graph_from_polygon",
        lambda polygon, **k: ox.truncate.truncate_graph_polygon(
            _street_graph(), polygon, truncate_by_edge=k["truncate_by_edge"]
        ),
    )

    with pytest.warns(UserWarning, match=r"part\(s\) 3 "):
        manifest = get_network.main("Test", modes=["driving"], output_path=tmp_path)

    assert seen["connect_parts"] is True
    assert manifest["connect_parts"] is True
    assert manifest["unconnected_parts"] == {"driving": [3]}


@pytest.mark.parametrize(
    ("argv", "expected"), [([], True), (["--no-connect-parts"], False)]
)
def test_cli_connect_parts_flag(monkeypatch, argv, expected):
    captured = {}
    monkeypatch.setattr(get_network, "main", lambda **kwargs: captured.update(kwargs))
    get_network._cli(["--area", "Donostia", *argv])
    assert captured["connect_parts"] is expected
