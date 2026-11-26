"""Tests for the OSM networks read from local files (``_local_osm``)."""

from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, box

from multimodalpy._local_osm import OSMNetworkFileError, load_osm_network
from multimodalpy._osm import build_final_osm_layers
from multimodalpy._writers import _write_layers

# A hand-written OSM extract, built by osmnx as a download is: a 4x4 grid
# with residential streets, a tertiary avenue, a one-way primary road with
# a speed limit, a footway, a living street, traffic signals, and one street
# made of two ways that osmnx merges into a single edge.
LON0, LAT0, STEP = -0.3800, 39.4700, 0.0010


def osm_xml() -> str:
    nodes, ways = [], []

    def nid(r, c):
        return 1000 + r * 10 + c

    for r in range(4):
        for c in range(4):
            tags = '<tag k="highway" v="traffic_signals"/>' if (r, c) == (1, 1) else ""
            lat, lon = LAT0 + r * STEP, LON0 + c * STEP
            nodes.append(
                f'<node id="{nid(r, c)}" lat="{lat:.6f}" lon="{lon:.6f}" version="1">'
                f"{tags}</node>"
            )
    # mid-block vertex on row 0 between c=1 and c=2 (only a shape point)
    lat, lon = LAT0 + 0.0002, LON0 + 1.5 * STEP
    nodes.append(f'<node id="2001" lat="{lat:.6f}" lon="{lon:.6f}" version="1"/>')
    wid = 1

    def way(refs, **tags):
        nonlocal wid
        nd = "".join(f'<nd ref="{r}"/>' for r in refs)
        tg = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
        ways.append(f'<way id="{500 + wid}" version="1">{nd}{tg}</way>')
        wid += 1

    # row 0: two ways meeting at 2001, not an intersection: osmnx merges them
    way([nid(0, 0), nid(0, 1), 2001], highway="residential", name="Carrer A")
    way([2001, nid(0, 2), nid(0, 3)], highway="residential", name="Carrer A")
    for r in (1, 2, 3):
        way([nid(r, c) for c in range(4)], highway="residential", name=f"Carrer {r}")
    way([nid(r, 0) for r in range(4)], highway="tertiary", name="Avinguda Oest")
    way(
        [nid(r, 3) for r in range(4)],
        highway="primary",
        maxspeed="50",
        oneway="yes",
        name="Gran Via",
    )
    way([nid(0, 1), nid(1, 1), nid(2, 1), nid(3, 1)], highway="footway")
    way(
        [nid(0, 2), nid(1, 2), nid(2, 2), nid(3, 2)],
        highway="living_street",
        maxspeed="20",
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6">'
        + "".join(nodes)
        + "".join(ways)
        + "</osm>"
    )


WHOLE = gpd.GeoDataFrame(
    {"area_name": ["test"]}, geometry=[box(-0.39, 39.46, -0.37, 39.48)], crs=4326
)


def downloaded(tmp_path: Path, network_type: str):
    """The graph and layers a download of the extract gives."""
    import osmnx as ox

    path = tmp_path / "net.osm"
    path.write_text(osm_xml(), encoding="utf-8")
    graph = ox.graph_from_xml(
        path, bidirectional=network_type == "walk", simplify=True, retain_all=True
    )
    label = {"walk": "walking", "drive": "driving"}[network_type]
    nodes, edges = build_final_osm_layers(graph, layer_id=label, mode=network_type)
    return graph, nodes, edges


def written(tmp_path: Path, label: str, nodes, edges, file_type: str) -> Path:
    """Write the layers as download_network does; return what to read back."""
    out = tmp_path / file_type
    out.mkdir()
    _write_layers(
        {f"osm_{label}_nodes": nodes, f"osm_{label}_edges": edges},
        out,
        file_type,
        area_slug="test",
        layer_paths={
            f"osm_{label}_nodes": f"{label}/nodes",
            f"osm_{label}_edges": f"{label}/edges",
        },
    )
    return out / "test.gpkg" if file_type == "geopackage" else out


def same_edges(a: gpd.GeoDataFrame, b: gpd.GeoDataFrame) -> None:
    """Assert two OSM edge layers hold the same edges, whatever their order."""

    def tidy(edges):
        edges = edges.assign(_shape=edges.geometry.apply(lambda geom: geom.wkb))
        edges = edges.drop(columns=edges.geometry.name)
        keys = ["edge_id", "from_node", "to_node", "_shape"]
        return edges.astype({"edge_id": str}).sort_values(keys).reset_index(drop=True)

    a, b = tidy(a), tidy(b)
    assert len(a) == len(b)
    assert list(a.columns) == list(b.columns)
    for column in ("edge_id", "from_node", "to_node", "highway", "hwy_raw", "name"):
        assert a[column].astype(str).tolist() == b[column].astype(str).tolist(), column
    for column in ("length", "tts", "maxspeed"):
        left = pd.to_numeric(a[column], errors="coerce")
        right = pd.to_numeric(b[column], errors="coerce")
        assert ((left - right).abs().fillna(0) < 1e-6).all(), column
        assert (left.isna() == right.isna()).all(), column
    assert a["connector"].astype(bool).tolist() == b["connector"].astype(bool).tolist()


@pytest.mark.parametrize("network_type", ["walk", "drive"])
@pytest.mark.parametrize("file_type", ["geojson", "shapefile", "geopackage"])
def test_a_multimodalpy_output_reads_back_the_same(tmp_path, network_type, file_type):
    _, nodes, edges = downloaded(tmp_path, network_type)
    label = {"walk": "walking", "drive": "driving"}[network_type]
    assert edges["edge_id"].duplicated().any()  # some edges stand for two ways
    source = written(tmp_path, label, nodes, edges, file_type)
    out = load_osm_network(source, label, WHOLE)[label]
    same_edges(edges, out["edges"])
    # Ids as text: the files hold them so (OSM numbers and generated names).
    roles = dict(zip(nodes["node_id"].astype(str), nodes["node_role"], strict=True))
    read = dict(
        zip(out["nodes"]["node_id"].astype(str), out["nodes"]["node_role"], strict=True)
    )
    assert read == roles


def test_the_mode_folder_itself_works_too(tmp_path):
    _, nodes, edges = downloaded(tmp_path, "walk")
    source = written(tmp_path, "walking", nodes, edges, "geojson")
    out = load_osm_network(source / "walking", "caminable", WHOLE)
    same_edges(edges, out["walking"]["edges"])


def test_an_osmnx_graphml_gives_the_same_network(tmp_path):
    import osmnx as ox

    graph, _, edges = downloaded(tmp_path, "drive")
    ox.save_graphml(graph, tmp_path / "net.graphml")
    out = load_osm_network(tmp_path / "net.graphml", "driving", WHOLE)["driving"]
    same_edges(edges, out["edges"])


def test_edges_alone_are_enough(tmp_path):
    _, nodes, edges = downloaded(tmp_path, "walk")
    source = written(tmp_path, "walking", nodes, edges, "geojson")
    out = load_osm_network(
        {"edges": source / "walking" / "edges.geojson"}, "walking", WHOLE
    )["walking"]
    same_edges(edges, out["edges"])


def other_tool_edges(crs=25830, **columns) -> gpd.GeoDataFrame:
    """Three streets as another tool writes them: u/v, no length or tts."""
    lines = [
        LineString([(725000, 4372000), (725100, 4372000)]),
        LineString([(725100, 4372000), (725100, 4372150)]),
        LineString([(725100, 4372150), (726000, 4372150)]),
    ]
    data = {"u": [1, 2, 3], "v": [2, 3, 4], "highway": ["residential"] * 3}
    data.update(columns)
    return gpd.GeoDataFrame(data, geometry=lines, crs=crs)


AROUND = gpd.GeoDataFrame(
    {"area_name": ["test"]},
    geometry=[box(725000, 4371900, 725300, 4372300)],
    crs=25830,
).to_crs(4326)


def test_layers_from_another_tool_get_the_missing_columns(tmp_path):
    other_tool_edges().to_file(tmp_path / "streets.gpkg", layer="streets")
    out = load_osm_network(tmp_path / "streets.gpkg", "walking", AROUND)["walking"]
    edges = out["edges"].sort_values("from_node").reset_index(drop=True)
    # length from the geometry, in metres; walking at 5 km/h
    assert edges["length"].round(1).tolist()[:2] == [100.0, 150.0]
    assert (edges["tts"] - edges["length"] / (5 / 3.6)).abs().max() < 1e-6
    assert edges["edge_id"].astype(str).str.startswith("walking_").all()
    assert edges.crs.to_epsg() == 4326


def test_the_network_is_clipped_like_a_download(tmp_path):
    """A street with a node in the area is kept whole; one far away is not."""
    edges = other_tool_edges()
    far = LineString([(740000, 4380000), (740100, 4380000)])
    edges = pd.concat(
        [edges, gpd.GeoDataFrame({"u": [8], "v": [9]}, geometry=[far], crs=25830)],
        ignore_index=True,
    )
    edges.to_file(tmp_path / "streets.geojson")
    out = load_osm_network(tmp_path / "streets.geojson", "walking", AROUND)["walking"]
    assert sorted(out["edges"]["from_node"].astype(int)) == [1, 2, 3]
    # the third street leaves the area but has a node in it: whole
    assert out["edges"]["length"].max() == pytest.approx(900, abs=1)


def test_a_given_travel_time_is_kept(tmp_path):
    other_tool_edges(tts=[11.0, 22.0, 33.0]).to_file(tmp_path / "streets.geojson")
    out = load_osm_network(tmp_path / "streets.geojson", "driving", AROUND)["driving"]
    assert sorted(out["edges"]["tts"].round(6)) == [11.0, 22.0, 33.0]


@pytest.mark.parametrize(
    ("source", "match"),
    [
        (lambda tmp: tmp / "nowhere.gpkg", "not found"),
        (lambda tmp: {"nodes": tmp / "n.geojson"}, "needs an 'edges' file"),
        (lambda tmp: tmp, "no edges file"),
    ],
)
def test_unreadable_sources_are_explained(tmp_path, source, match):
    with pytest.raises(OSMNetworkFileError, match=match):
        load_osm_network(source(tmp_path), "walking", AROUND)


def test_edges_without_nodes_to_join_are_refused(tmp_path):
    edges = other_tool_edges().rename(columns={"u": "a", "v": "b"})
    edges.to_file(tmp_path / "streets.geojson")
    with pytest.raises(OSMNetworkFileError, match="from_node and to_node"):
        load_osm_network(tmp_path / "streets.geojson", "walking", AROUND)


def test_points_are_not_edges(tmp_path):
    gpd.GeoDataFrame(
        {"u": [1], "v": [2]}, geometry=[Point(725000, 4372000)], crs=25830
    ).to_file(tmp_path / "points.geojson")
    with pytest.raises(OSMNetworkFileError, match="must be lines"):
        load_osm_network(tmp_path / "points.geojson", "walking", AROUND)


def test_a_network_outside_the_area_is_refused(tmp_path):
    other_tool_edges().to_file(tmp_path / "streets.geojson")
    madrid = gpd.GeoDataFrame(
        {"area_name": ["Madrid"]}, geometry=[box(-3.75, 40.38, -3.65, 40.45)], crs=4326
    )
    with pytest.raises(OSMNetworkFileError, match="no street in the study area"):
        load_osm_network(tmp_path / "streets.geojson", "walking", madrid)
