"""OSM street networks read from local files instead of downloaded.

What :func:`multimodalpy.build_network` uses for ``osm_networks``. A network
comes from one of:

- a folder or GeoPackage written by multimodalpy (``walking/nodes.geojson``
  and ``walking/edges.geojson``, the ``osm_walking_*`` layers...);
- node and edge layers from other tools: a GeoPackage, GeoJSON or Shapefile
  of edges (lines), with or without a nodes layer, given as one file or as
  ``{"edges": path, "nodes": path}``;
- an osmnx GraphML file (``osmnx.save_graphml``).

Whatever the source, it becomes an osmnx graph, is clipped to the study area
with the same rules as a download (``buffer_m``; the streets that cross the
boundary are kept whole) and goes through the same standardization
(:func:`multimodalpy._osm.build_final_osm_layers`), so the layers have
exactly the columns of a downloaded network. Columns the file lacks are
computed: ``length`` from the geometry, ``tts`` from the street type and
speed limit as for a download, ``edge_id`` numbered. The edges must say
which nodes they join (``from_node``/``to_node`` or the usual alternatives
``u``/``v``, ``source``/``target``) and have a geometry; anything else
missing is an error before processing starts.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import geopandas as gpd
    import networkx as nx

logger = logging.getLogger(__name__)

#: Names accepted for the node an edge starts at, ends at, and for the ids.
FROM_COLUMNS = ("from_node", "u", "source", "from", "start_node", "node_start")
TO_COLUMNS = ("to_node", "v", "target", "to", "end_node", "node_end")
NODE_ID_COLUMNS = ("node_id", "osmid", "osm_id", "id")
EDGE_ID_COLUMNS = ("edge_id", "osmid", "osm_id", "way_id", "id")

#: Edge attributes carried into the graph when the file has them. For a
#: multimodalpy output, the raw tags (``hwy_raw``, ``spd_raw``) are the ones
#: the travel time was computed from, so they win over the cleaned ones.
_EDGE_ATTRIBUTES = {
    "highway": ("hwy_raw", "highway"),
    "maxspeed": ("spd_raw", "maxspeed"),
    "lanes": ("lanes",),
    "name": ("name",),
    "oneway": ("oneway",),
    "reversed": ("reversed",),
    "connector": ("connector",),
}

_LAYER_SUFFIXES = (".geojson", ".gpkg", ".shp", ".json")

OSMSource = str | Path | Mapping[str, str | Path]


class OSMNetworkFileError(ValueError):
    """A local OSM network that can't be read or lacks what it needs."""


def _find_column(columns: Any, names: tuple[str, ...]) -> str | None:
    by_lower = {str(column).lower(): column for column in columns}
    for name in names:
        if name in by_lower:
            return by_lower[name]
    return None


def _read_layer(path: Path, layer: str | None = None) -> gpd.GeoDataFrame:
    import geopandas as gpd

    try:
        return gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    except Exception as exc:  # noqa: BLE001 - reported with the file name
        raise OSMNetworkFileError(f"Could not read {path}: {exc}") from exc


def _gpkg_layers(path: Path, label: str) -> tuple[str | None, str]:
    """Find the nodes and edges layers of a GeoPackage for the mode ``label``."""
    import pyogrio

    layers = [str(row[0]) for row in pyogrio.list_layers(path)]
    for nodes, edges in (
        (f"osm_{label}_nodes", f"osm_{label}_edges"),
        (f"{label}_nodes", f"{label}_edges"),
        ("nodes", "edges"),
    ):
        if edges in layers:
            return (nodes if nodes in layers else None), edges
    lines = [
        str(row[0])
        for row in pyogrio.list_layers(path)
        if row[1] and "LINE" in str(row[1]).upper()
    ]
    if len(lines) == 1:
        return None, lines[0]
    raise OSMNetworkFileError(
        f"{path}: no edges layer for '{label}' (looked for osm_{label}_edges, "
        f"{label}_edges and edges, or a single line layer). Its layers: "
        f"{', '.join(layers) or 'none'}."
    )


def _layer_in_folder(folder: Path, stem: str) -> Path | None:
    for suffix in _LAYER_SUFFIXES:
        path = folder / f"{stem}{suffix}"
        if path.exists():
            return path
    return None


def read_osm_layers(
    source: OSMSource, label: str
) -> tuple[gpd.GeoDataFrame | None, gpd.GeoDataFrame]:
    """Read the nodes (or ``None``) and edges layers of a local network.

    ``label`` is the mode (``walking``, ``bike``, ``driving``): it picks the
    layers of a multimodalpy output with several networks.
    """
    if isinstance(source, Mapping):
        if "edges" not in source:
            raise OSMNetworkFileError(
                "A network given as a dict needs an 'edges' file "
                f"({{'edges': ..., 'nodes': ...}}). Received keys: {list(source)}"
            )
        edges_path = Path(source["edges"])
        nodes_path = Path(source["nodes"]) if source.get("nodes") else None
        for path in (edges_path, nodes_path):
            if path is not None and not path.exists():
                raise OSMNetworkFileError(f"File not found: {path}")
        nodes = _read_layer(nodes_path) if nodes_path is not None else None
        return nodes, _read_layer(edges_path)
    path = Path(source)
    if not path.exists():
        raise OSMNetworkFileError(f"File or folder not found: {path}")
    if path.is_dir():
        folder = path / label if (path / label).is_dir() else path
        found = _layer_in_folder(folder, "edges")
        if found is None:
            gpkgs = sorted(folder.glob("*.gpkg"))
            if len(gpkgs) == 1:
                return read_osm_layers(gpkgs[0], label)
            raise OSMNetworkFileError(
                f"{folder}: no edges file (edges.geojson, edges.gpkg or "
                "edges.shp), as multimodalpy writes them."
            )
        nodes_file = _layer_in_folder(folder, "nodes")
        nodes = _read_layer(nodes_file) if nodes_file is not None else None
        return nodes, _read_layer(found)
    if path.suffix.lower() == ".gpkg":
        nodes_layer, edges_layer = _gpkg_layers(path, label)
        nodes = _read_layer(path, nodes_layer) if nodes_layer else None
        return nodes, _read_layer(path, edges_layer)
    return None, _read_layer(path)


def _identifier(value: Any) -> Any:
    """Return an id as osmnx has it: whole numbers written as text as numbers.

    A file column that mixes OSM ids (numbers) with generated ones
    (``walking_topology_node_0000003``) is read back as text; ``"1001"``
    is turned back into ``1001``, while ``"007"`` or ``"a1"`` stay text.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit() and str(int(text)) == text:
            return int(text)
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if hasattr(value, "item"):  # numpy number
        return _identifier(value.item())
    return value


def _edge_value(value: Any) -> Any:
    """``value`` as osmnx keeps edge attributes (``None`` for empty)."""
    try:
        import pandas as pd

        if value is None or (not isinstance(value, (list, tuple)) and pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    return value


def graph_from_layers(
    nodes: gpd.GeoDataFrame | None,
    edges: gpd.GeoDataFrame,
    *,
    label: str,
    source: object = "",
) -> nx.MultiDiGraph:
    """Build an osmnx graph, in EPSG:4326, from a network's nodes and edges.

    Without nodes, or for the nodes the layer misses, they are taken from
    the ends of the edges. Edges repeated with the same nodes and geometry
    (as in multimodalpy's output, one row per OSM way) become one edge with
    the list of their ids, as osmnx keeps a simplified edge.
    """
    import networkx as nx
    import pandas as pd

    from ._osm import _metric_crs

    where = f" ({source})" if source else ""
    if edges.crs is None:
        raise OSMNetworkFileError(f"The {label} edges{where} have no CRS.")
    if edges.empty:
        raise OSMNetworkFileError(f"The {label} edges{where} are empty.")
    from_col = _find_column(edges.columns, FROM_COLUMNS)
    to_col = _find_column(edges.columns, TO_COLUMNS)
    missing = [
        name
        for name, column in (("from_node", from_col), ("to_node", to_col))
        if column is None
    ]
    if missing:
        raise OSMNetworkFileError(
            f"The {label} edges{where} need the nodes each edge joins: "
            f"{' and '.join(missing)} (or {', '.join(FROM_COLUMNS[1:4])} / "
            f"{', '.join(TO_COLUMNS[1:4])}). "
            f"Columns: {', '.join(map(str, edges.columns))}."
        )
    edges = edges[edges.geometry.notna() & ~edges.geometry.is_empty]
    kinds = set(edges.geom_type)
    if not kinds <= {"LineString"}:
        from shapely.ops import linemerge

        edges = edges.copy()
        edges.geometry = [
            linemerge(geom) if geom.geom_type == "MultiLineString" else geom
            for geom in edges.geometry
        ]
        kinds = set(edges.geom_type)
        if not kinds <= {"LineString"}:
            raise OSMNetworkFileError(
                f"The {label} edges{where} must be lines; found "
                f"{', '.join(sorted(kinds - {'LineString'}))}."
            )

    edges = edges.to_crs("EPSG:4326")
    work_crs = _metric_crs(edges) or "EPSG:3857"
    length_col = _find_column(edges.columns, ("length",))
    lengths = (
        pd.to_numeric(edges[length_col], errors="coerce")
        if length_col is not None
        else pd.Series(float("nan"), index=edges.index)
    )
    # Metres along the geometry where the file has no length.
    measured = edges.geometry.to_crs(work_crs).length
    lengths = lengths.where(lengths.notna() & (lengths >= 0), measured)
    tts_col = _find_column(edges.columns, ("tts",))
    tts = (
        pd.to_numeric(edges[tts_col], errors="coerce")
        if tts_col is not None
        else pd.Series(float("nan"), index=edges.index)
    )
    id_col = _find_column(edges.columns, EDGE_ID_COLUMNS)
    edge_ids = (
        [_identifier(value) for value in edges[id_col]]
        if id_col is not None
        else [f"{label}_{number}" for number in range(1, len(edges) + 1)]
    )
    attribute_cols = {
        attribute: _find_column(edges.columns, names)
        for attribute, names in _EDGE_ATTRIBUTES.items()
    }

    # One graph edge per (from, to, geometry), with every id it stands for.
    merged: dict[tuple[Any, Any, bytes], dict[str, Any]] = {}
    for position, (u, v, geom) in enumerate(
        zip(edges[from_col], edges[to_col], edges.geometry, strict=True)
    ):
        if _edge_value(u) is None or _edge_value(v) is None:
            raise OSMNetworkFileError(
                f"The {label} edges{where}: an edge has no {from_col} or {to_col}."
            )
        u, v = _identifier(u), _identifier(v)
        key = (u, v, geom.wkb)
        entry = merged.get(key)
        if entry is not None:
            entry["osmid"].append(edge_ids[position])
            continue
        entry = {
            "u": u,
            "v": v,
            "osmid": [edge_ids[position]],
            "geometry": geom,
            "length": float(lengths.iloc[position]),
        }
        file_tts = tts.iloc[position]
        if pd.notna(file_tts):
            # Kept as given (see build_final_osm_layers): its length is the
            # one it was computed for, if the edge gets split.
            entry["_tts_file"] = float(file_tts)
            entry["_len_file"] = entry["length"]
        for attribute, column in attribute_cols.items():
            if column is not None:
                value = _edge_value(edges[column].iloc[position])
                if value is not None:
                    entry[attribute] = value
        merged[key] = entry

    # Nodes: the layer's, then the ends of the edges for any it lacks.
    points: dict[Any, tuple[float, float]] = {}
    if nodes is not None and not nodes.empty:
        if nodes.crs is None:
            raise OSMNetworkFileError(f"The {label} nodes{where} have no CRS.")
        node_col = _find_column(nodes.columns, NODE_ID_COLUMNS)
        if node_col is None:
            raise OSMNetworkFileError(
                f"The {label} nodes{where} need an id column "
                f"({', '.join(NODE_ID_COLUMNS)})."
            )
        located = nodes[nodes.geometry.notna()].to_crs("EPSG:4326")
        for node_id, geom in zip(located[node_col], located.geometry, strict=True):
            points.setdefault(_identifier(node_id), (geom.x, geom.y))
    for entry in merged.values():
        coords = entry["geometry"].coords
        points.setdefault(entry["u"], coords[0])
        points.setdefault(entry["v"], coords[-1])

    graph = nx.MultiDiGraph(crs="EPSG:4326")
    for node_id, (x, y) in points.items():
        graph.add_node(node_id, x=float(x), y=float(y))
    for entry in merged.values():
        u, v = entry.pop("u"), entry.pop("v")
        if len(entry["osmid"]) == 1:
            entry["osmid"] = entry["osmid"][0]
        graph.add_edge(u, v, **entry)
    return graph


def read_osm_graph(source: OSMSource, label: str) -> nx.MultiDiGraph:
    """Read the osmnx graph, in EPSG:4326, of a local network (see the module)."""
    if not isinstance(source, Mapping) and Path(source).suffix.lower() == ".graphml":
        import osmnx as ox

        path = Path(source)
        if not path.exists():
            raise OSMNetworkFileError(f"File not found: {path}")
        try:
            graph = ox.load_graphml(path)
        except Exception as exc:  # noqa: BLE001 - reported with the file name
            raise OSMNetworkFileError(f"Could not read {path}: {exc}") from exc
        crs = graph.graph.get("crs")
        if crs is None:
            raise OSMNetworkFileError(f"{path} has no CRS (the graph's 'crs').")
        if str(crs).upper() not in {"EPSG:4326", "WGS84"}:
            graph = ox.project_graph(graph, to_crs="EPSG:4326")
        return graph
    nodes, edges = read_osm_layers(source, label)
    return graph_from_layers(nodes, edges, label=label, source=source)


def load_osm_network(
    source: OSMSource,
    mode: str,
    boundary: gpd.GeoDataFrame,
    *,
    buffer_m: float = 0.0,
    output_crs: str = "EPSG:4326",
) -> dict[str, dict[str, Any]]:
    """Read, clip and standardize one local OSM network.

    Returns ``{label: {"nodes", "edges", "graph", "unconnected_parts": []}}``
    like :func:`multimodalpy.download_osm`, ``label`` being the canonical
    name of ``mode`` (``walking``, ``bike``, ``driving``). Raises
    :class:`OSMNetworkFileError` if the source can't be read, lacks what it
    needs, or has no street in the study area.
    """
    import osmnx as ox

    from ._boundaries import study_area_geometry
    from ._osm import _network_type, build_final_osm_layers

    network_type, label = _network_type(mode)
    graph = read_osm_graph(source, label)
    polygon = study_area_geometry(boundary, buffer_m=buffer_m)
    try:
        # As a download with truncate_by_edge: a street with any node in
        # the area is kept whole.
        graph = ox.truncate.truncate_graph_polygon(
            graph, polygon, truncate_by_edge=True
        )
    except ValueError as exc:  # osmnx: "Found no graph nodes within the polygon"
        raise OSMNetworkFileError(
            f"The {label} network ({source}) has no street in the study area."
        ) from exc
    if graph.number_of_edges() == 0:
        raise OSMNetworkFileError(
            f"The {label} network ({source}) has no street in the study area."
        )
    nodes, edges = build_final_osm_layers(
        graph, layer_id=label, mode=network_type, output_crs=output_crs
    )
    logger.info("Read the %s network from %s: %d edges.", label, source, len(edges))
    return {
        label: {
            "nodes": nodes,
            "edges": edges,
            "graph": graph,
            "unconnected_parts": [],
        }
    }
