"""One Overpass query for several OSM modes (``_osm._SharedDownload``).

A fake Overpass server answers osmnx's per-mode queries with the ways each
mode should get, by hand (``WAYS``), and the shared query with all of them.
The networks built from the shared answer must be the ones osmnx builds
from its own queries, and the hand labels check the filter evaluation.
"""

from __future__ import annotations

import re

import geopandas as gpd
import osmnx as ox
import pandas as pd
import pytest
from shapely.geometry import MultiPolygon, box

from multimodalpy import _osm, _overpass
from multimodalpy._overpass import OSMDownloadError

AREA = box(-0.41, 39.46, -0.39, 39.48)
MODES = {"walk": "walking", "bike": "bike", "drive": "driving"}


def _node(i: int, j: int) -> int:
    return 100 + 10 * i + j


NODES = {
    _node(i, j): (39.464 + 0.004 * i, -0.406 + 0.004 * j)
    for i in range(4)
    for j in range(4)
}
ROW = [[_node(i, j) for j in range(4)] for i in range(4)]
COL = [[_node(i, j) for i in range(4)] for j in range(4)]

# (way id, nodes, tags, network types that must get it)
WAYS = [
    (1, ROW[0], {"highway": "residential"}, {"walk", "bike", "drive"}),
    (2, ROW[1], {"highway": "footway"}, {"walk"}),
    (3, ROW[2], {"highway": "cycleway"}, {"bike"}),
    (4, ROW[3], {"highway": "motorway", "maxspeed": "120"}, {"drive"}),
    (5, COL[0], {"highway": "residential", "oneway": "yes"}, {"walk", "bike", "drive"}),
    (6, COL[1], {"highway": "service", "service": "parking_aisle"}, {"walk", "bike"}),
    (7, COL[2][:2], {"highway": "residential", "access": "private"}, set()),
    (8, COL[2][1:], {"highway": "residential", "foot": "no"}, {"bike", "drive"}),
    (9, COL[3][:3], {"highway": "residential", "bicycle": "no"}, {"walk", "drive"}),
    (10, COL[3][2:], {"highway": "steps"}, {"walk"}),
    (11, [_node(0, 0), _node(1, 1)], {"highway": "pedestrian", "area": "yes"}, set()),
    (
        12,
        [_node(0, 1), _node(1, 2)],
        {"highway": "tertiary", "name": "Carrer Major", "maxspeed": "50"},
        {"walk", "bike", "drive"},
    ),
]

_WAY_FILTER = re.compile(r"way((?:\[[^\]]*\])+)\(poly:")


def _answer(network_types: set[str]) -> dict:
    """The ways of ``network_types`` and their nodes, as Overpass sorts them."""
    ways = [w for w in WAYS if w[3] & network_types]
    used = sorted({n for w in ways for n in w[1]})
    nodes = [
        {"type": "node", "id": n, "lat": NODES[n][0], "lon": NODES[n][1]} for n in used
    ]
    return {
        "version": 0.6,
        "elements": nodes
        + [
            {"type": "way", "id": way_id, "nodes": list(nodes_), "tags": dict(tags)}
            for way_id, nodes_, tags, _ in ways
        ],
    }


@pytest.fixture
def server(monkeypatch):
    """Fake Overpass server; records each query and the types it asked for."""
    from osmnx import _overpass as ox_overpass

    by_filter = {ox_overpass._get_network_filter(nt): nt for nt in MODES}
    queries: list[dict] = []

    def request(data, **kwargs):
        query = data["data"]
        asked = {by_filter[f] for f in _WAY_FILTER.findall(query)}
        queries.append({"query": query, "types": asked})
        if server_state["fail"]:
            raise server_state["fail"]
        return _answer(asked & server_state["serves"])

    server_state: dict = {"fail": None, "serves": set(MODES), "queries": queries}
    monkeypatch.setattr(ox_overpass, "_overpass_request", request)
    monkeypatch.setattr(_overpass, "_sleep", lambda seconds: None)
    return server_state


def _boundary(geometry=AREA) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"area_name": ["Test"]}, geometry=[geometry], crs=4326)


def _graph_contents(graph):
    nodes = sorted((n, sorted(d.items())) for n, d in graph.nodes(data=True))
    edges = sorted(
        (u, v, k, sorted((a, str(b)) for a, b in d.items()))
        for u, v, k, d in graph.edges(keys=True, data=True)
    )
    return nodes, edges


@pytest.mark.parametrize(
    "geometry",
    [
        AREA,
        MultiPolygon(
            [box(-0.41, 39.46, -0.399, 39.48), box(-0.396, 39.46, -0.39, 39.48)]
        ),
    ],
    ids=["one polygon", "two parts"],
)
def test_shared_query_builds_the_networks_osmnx_builds(server, geometry):
    shared = _osm.download_osm(_boundary(geometry), modes=list(MODES.values()))
    assert len(server["queries"]) == 1
    assert server["queries"][0]["types"] == set(MODES)

    for network_type, label in MODES.items():
        single = _osm.download_osm(_boundary(geometry), modes=[label])
        assert server["queries"][-1]["types"] == {network_type}
        assert _graph_contents(shared[label]["graph"]) == _graph_contents(
            single[label]["graph"]
        )
        for layer in ("nodes", "edges"):
            pd.testing.assert_frame_equal(
                shared[label][layer].to_wkt(), single[label][layer].to_wkt()
            )
        assert shared[label]["unconnected_parts"] == single[label]["unconnected_parts"]


def test_each_network_gets_the_ways_of_its_mode(server):
    results = _osm.download_osm(_boundary(), modes=list(MODES.values()))
    for network_type, label in MODES.items():
        expected = {way_id for way_id, _, _, types in WAYS if network_type in types}
        osmids = set()
        for _, _, data in results[label]["graph"].edges(data=True):
            ids = data["osmid"]
            osmids |= set(ids) if isinstance(ids, list) else {ids}
        assert osmids == expected, label


def test_the_shared_query_is_one_union_of_the_filters(server):
    _osm.download_osm(_boundary(), modes=["walking", "driving"])
    (asked,) = server["queries"]
    assert asked["types"] == {"walk", "drive"}
    assert asked["query"].count("way[") == 2
    assert asked["query"].endswith(");>;);out;")


def test_one_mode_keeps_osmnx_own_query(server):
    _osm.download_osm(_boundary(), modes=["bike"])
    assert len(server["queries"]) == 1
    assert server["queries"][0]["query"].count("way[") == 1


def test_synonyms_of_one_mode_are_downloaded_once(server):
    results = _osm.download_osm(_boundary(), modes=["walking", "caminable"])
    assert list(results) == ["walking"]
    assert server["queries"][0]["query"].count("way[") == 1


def test_a_failed_shared_query_fails_every_mode(server):
    import requests

    server["fail"] = requests.exceptions.ConnectionError("connection reset")
    failures: dict = {}
    results = _osm.download_osm(
        _boundary(), modes=["walking", "bike", "driving"], failures=failures
    )
    assert results == {}
    assert list(failures) == ["walking", "bike", "driving"]
    error = failures["walking"]
    assert "walking, bike and driving networks" in str(error)
    assert error.transient
    assert len(server["queries"]) == 1 + len(_overpass.RETRY_WAITS)


def test_a_failed_shared_query_raises_without_a_failures_dict(server):
    import requests

    server["fail"] = requests.exceptions.ConnectionError("connection reset")
    with pytest.raises(OSMDownloadError, match="walking and bike networks"):
        _osm.download_osm(_boundary(), modes=["walking", "bike"])


def test_a_mode_with_no_streets_in_the_answer_fails_alone(server, monkeypatch):
    # The answer only has footways: no network for driving.
    monkeypatch.setattr(
        _osm,
        "_keep_ways",
        lambda answer, clauses, real=_osm._keep_ways: real(
            {
                **answer,
                "elements": [
                    e
                    for e in answer["elements"]
                    if e["type"] == "node" or e["tags"]["highway"] == "footway"
                ],
            },
            clauses,
        ),
    )
    failures: dict = {}
    results = _osm.download_osm(
        _boundary(), modes=["walking", "driving"], failures=failures
    )
    assert list(results) == ["walking"]
    assert list(failures) == ["driving"]
    assert not failures["driving"].transient


def test_unreadable_filters_fall_back_to_one_query_per_mode(server, monkeypatch):
    monkeypatch.setattr(_osm, "_parse_way_filter", lambda way_filter: None)
    results = _osm.download_osm(_boundary(), modes=["walking", "bike"])
    assert set(results) == {"walking", "bike"}
    assert [q["types"] for q in server["queries"]] == [{"walk"}, {"bike"}]


def test_missing_osmnx_internals_fall_back_to_one_query_per_mode(server, monkeypatch):
    from osmnx import graph as ox_graph

    monkeypatch.delattr(ox_graph, "_create_graph")
    asked: list[str] = []

    def graph_from_polygon(polygon, *, network_type, **kwargs):
        asked.append(network_type)
        return _street_graph(network_type)

    monkeypatch.setattr(ox, "graph_from_polygon", graph_from_polygon)
    results = _osm.download_osm(_boundary(), modes=["walking", "bike"])
    assert set(results) == {"walking", "bike"}
    assert asked == ["walk", "bike"]
    assert server["queries"] == []


def _street_graph(network_type):
    import networkx as nx

    graph = nx.MultiDiGraph(crs="EPSG:4326")
    for way_id, nodes, tags, types in WAYS:
        if network_type not in types:
            continue
        for n in nodes:
            graph.add_node(n, x=NODES[n][1], y=NODES[n][0], street_count=2)
        for u, v in zip(nodes, nodes[1:], strict=False):
            graph.add_edge(u, v, osmid=way_id, length=100.0, **tags)
    return graph


# ---------------------------------------------------------------------------
# Reading Overpass filters
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("network_type", ["walk", "bike", "drive"])
def test_osmnx_filters_can_be_read(network_type):
    from osmnx import _overpass as ox_overpass

    clauses = _osm._parse_way_filter(ox_overpass._get_network_filter(network_type))
    assert clauses is not None
    assert clauses[0] == ("highway", None, "")


@pytest.mark.parametrize(
    "way_filter",
    ['["highway"~"primary",i]', '[~"^addr"~"."]', '["highway"] junk', ""],
)
def test_other_filters_are_not_read(way_filter):
    assert _osm._parse_way_filter(way_filter) is None


@pytest.mark.parametrize(
    ("tags", "way_filter", "passes"),
    [
        ({"highway": "residential"}, '["highway"]', True),
        ({}, '["highway"]', False),
        ({"highway": "footway"}, '["highway"!~"footway|steps"]', False),
        ({"highway": "residential"}, '["highway"!~"footway|steps"]', True),
        ({"highway": "motorway_link"}, '["highway"!~"motor"]', False),  # anywhere
        ({}, '["access"!~"private"]', True),  # missing key passes !~
        ({"foot": "No"}, '["foot"!~"no"]', True),  # case-sensitive
        ({"highway": "primary"}, '["highway"~"^(primary|secondary)$"]', True),
        ({}, '["highway"~"primary"]', False),
        ({"lanes": "2"}, '["lanes"="2"]', True),
        ({"lanes": "3"}, '["lanes"!="2"]', True),
        ({}, '["lanes"!="2"]', True),
    ],
)
def test_tags_match_like_overpass(tags, way_filter, passes):
    clauses = _osm._parse_way_filter(way_filter)
    assert _osm._tags_match(tags, clauses) is passes


def test_keep_ways_drops_the_nodes_only_other_ways_use():
    answer = _answer({"walk", "bike", "drive"})
    kept = _osm._keep_ways(answer, _osm._parse_way_filter('["highway"="motorway"]'))
    ways = [e["id"] for e in kept["elements"] if e["type"] == "way"]
    nodes = [e["id"] for e in kept["elements"] if e["type"] == "node"]
    assert ways == [4]
    assert nodes == ROW[3]


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["walking"], "walking"),
        (["walking", "bike"], "walking and bike"),
        (["walking", "bike", "driving"], "walking, bike and driving"),
    ],
)
def test_joined(labels, expected):
    assert _osm._joined(labels) == expected
