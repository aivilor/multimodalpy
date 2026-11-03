"""Writing the networks to disk in every output format.

Private module: GeoJSON / Shapefile / GeoPackage layers, NetworkX
``graph.json``, ``matrix.npz`` and the GTFS schedule tables.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import geopandas as gpd
    import networkx as nx
    import pandas as pd

from . import _gtfs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layers (GeoJSON, Shapefile, GeoPackage)
# ---------------------------------------------------------------------------
# Extension and OGR driver for the formats written layer by layer.
_FILE_FORMATS = {
    "geojson": (".geojson", "GeoJSON"),
    "shapefile": (".shp", "ESRI Shapefile"),
}


def _relative_name(path: Path, root: Path) -> str:
    """Return path's location relative to root, always using '/' as separator."""
    return path.relative_to(root).as_posix()


def _write_layers(
    layers: dict[str, gpd.GeoDataFrame | None],
    output_dir: Path,
    output_file_type: str,
    *,
    area_slug: str,
    layer_paths: dict[str, str],
) -> list[str]:
    """Write the layers in the requested format.

    In GeoJSON and Shapefile, each layer goes into its own folder by mode
    (``driving/nodes.geojson``, ``bus/<dataset>/edges.shp``), instead of
    leaving dozens of loose files in a single directory; with Shapefile
    that's also five files per layer. This doesn't apply to GeoPackage: it's
    a single file with the layers inside, so it's left at the root and the
    layer names are kept as-is.
    """
    written: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    if output_file_type == "geopackage":
        gpkg_path = output_dir / f"{area_slug}.gpkg"
        for name, gdf in layers.items():
            if gdf is None or gdf.empty:
                continue
            gdf.to_file(gpkg_path, layer=name, driver="GPKG")
            written.append(f"{gpkg_path.name}::{name}")
        return written

    try:
        suffix, driver = _FILE_FORMATS[output_file_type]
    except KeyError:
        raise ValueError(
            f"Unsupported format in _write_layers: {output_file_type}"
        ) from None

    for name, gdf in layers.items():
        if gdf is None or gdf.empty:
            continue
        path = output_dir / f"{layer_paths.get(name, name)}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(path, driver=driver)
        written.append(_relative_name(path, output_dir))
    return written


# ---------------------------------------------------------------------------
# Schedule tables
# ---------------------------------------------------------------------------
def _write_schedule_tables(
    schedule_tables: dict[str, pd.DataFrame | None],
    output_dir: Path,
    output_file_type: str,
    *,
    area_slug: str,
    layer_paths: dict[str, str],
) -> list[str]:
    """Write the schedule tables (one per GTFS dataset) next to the spatial layers.

    - geopackage: added as attribute tables (no geometry) inside the same
      .gpkg, via sqlite3 (a GeoPackage is a SQLite database).
    - geojson / shapefile / networkx / matrix: don't natively support
      non-spatial tables, so they're written as CSV in the dataset's folder.
    """
    written: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    if output_file_type == "geopackage":
        import sqlite3

        gpkg_path = output_dir / f"{area_slug}.gpkg"
        connection = sqlite3.connect(gpkg_path)
        try:
            for name, schedule_df in schedule_tables.items():
                if schedule_df is None or schedule_df.empty:
                    continue
                schedule_df.to_sql(name, connection, if_exists="replace", index=False)
                written.append(f"{gpkg_path.name}::{name}")
        finally:
            connection.close()
        return written

    for name, schedule_df in schedule_tables.items():
        if schedule_df is None or schedule_df.empty:
            continue
        path = output_dir / f"{layer_paths.get(name, name)}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        schedule_df.to_csv(path, index=False)
        written.append(_relative_name(path, output_dir))
    return written


# ---------------------------------------------------------------------------
# NetworkX (node-link JSON)
# ---------------------------------------------------------------------------
# Key under which the edge list is stored in graph.json. Passed explicitly
# so the file does not depend on the installed networkx version: before
# 3.6, node_link_data() wrote "links" by default; from 3.6 it writes "edges".
GRAPH_JSON_EDGES_KEY = "edges"


def _write_node_link_json(graph: nx.MultiDiGraph, output_path: Path) -> Path:
    """Write ``graph`` as NetworkX node-link JSON (edge list under "edges")."""
    from networkx.readwrite import json_graph

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            json_graph.node_link_data(graph, edges=GRAPH_JSON_EDGES_KEY),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return output_path


def _json_safe(value: object) -> object:
    try:
        if value != value:
            return None
    except Exception:
        pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _graph_value(value: object) -> object:
    """JSON-safe value for a graph attribute (numpy scalars -> Python)."""
    if hasattr(value, "item") and not isinstance(value, (list, tuple, dict)):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    return _json_safe(value)


def _osm_layers_to_graph_json(
    nodes: gpd.GeoDataFrame, edges: gpd.GeoDataFrame, output_path: Path
) -> Path:
    """Write the final OSM layers as NetworkX node-link JSON.

    Built from the same standardized layers written to GeoPackage /
    GeoJSON / Shapefile, so every edge carries exactly the standard edge
    columns (``_osm.STANDARD_EDGE_COLUMNS``) as attributes, with
    ``geometry`` as WKT. Graph nodes are the layer's ``node_id`` with
    ``x``, ``y`` and ``node_role``.
    """
    import networkx as nx

    from ._osm import STANDARD_EDGE_COLUMNS

    def column_values(gdf: gpd.GeoDataFrame, column: str) -> list:
        # Plain lists instead of ``iterrows()``, which builds a pandas
        # Series per row: this runs once per edge of the whole network.
        if column == "geometry":
            return [None if geom is None else geom.wkt for geom in gdf.geometry]
        if column in gdf.columns:
            return gdf[column].tolist()
        return [None] * len(gdf)

    graph = nx.MultiDiGraph(crs=str(edges.crs) if edges.crs is not None else None)
    for geom, node_id, node_role in zip(
        nodes.geometry,
        column_values(nodes, "node_id"),
        column_values(nodes, "node_role"),
        strict=True,
    ):
        graph.add_node(
            _graph_value(node_id),
            x=float(geom.x) if geom is not None else None,
            y=float(geom.y) if geom is not None else None,
            node_role=_graph_value(node_role),
        )
    values = [column_values(edges, column) for column in STANDARD_EDGE_COLUMNS]
    for row in zip(*values, strict=True):
        attrs = {
            column: value if column == "geometry" else _graph_value(value)
            for column, value in zip(STANDARD_EDGE_COLUMNS, row, strict=True)
        }
        graph.add_edge(attrs["from_node"], attrs["to_node"], **attrs)

    return _write_node_link_json(graph, output_path)


def _add_missing_gtfs_endpoint(graph, node, row, end: int, name_column: str) -> None:
    """Give an edge endpoint coordinates when it is not in the stops layer.

    Stops and stop-to-stop edges are clipped separately (each must
    intersect the study area), so an edge crossing the boundary keeps a
    stop that the stops layer dropped. Without this, ``add_edge`` would
    create that node with no ``x`` / ``y``. Its position is taken from the
    matching end of the edge geometry (``end=0`` start, ``end=-1`` end),
    already in the output CRS.
    """
    if node in graph and graph.nodes[node].get("x") is not None:
        return
    geom = row.geometry
    coords = list(geom.coords) if geom is not None and not geom.is_empty else None
    graph.add_node(
        node,
        x=float(coords[end][0]) if coords else None,
        y=float(coords[end][1]) if coords else None,
        stop_name=_json_safe(row.get(name_column)),
    )


def _gtfs_to_graph_json(
    stops, edges, output_path: Path, edge_columns: Sequence[str] | None = None
) -> Path:
    """Build a NetworkX graph of stops + stop-to-stop edges and write it.

    Every edge carries ``edge_columns`` as attributes (by default, every
    column of ``edges``), with ``geometry`` as WKT, the same as the OSM
    graphs. ``download_network()`` passes the columns kept in the GTFS edge layers, so
    the graph holds the same data as the other formats, under the long
    column names (JSON has no name length limit).
    """
    import networkx as nx

    # Same graph attribute as the OSM graphs, so readers know the CRS of
    # the node ``x`` / ``y`` without having to guess it.
    crs = next(
        (
            layer.crs
            for layer in (stops, edges)
            if layer is not None and getattr(layer, "crs", None) is not None
        ),
        None,
    )
    graph = nx.MultiDiGraph(crs=str(crs) if crs is not None else None)

    if stops is not None and not stops.empty:
        for _, row in stops.iterrows():
            node_id = row.get("stop_id") or row.get("node_id")
            geom = row.geometry
            graph.add_node(
                str(node_id),
                x=float(geom.x) if geom is not None else None,
                y=float(geom.y) if geom is not None else None,
                stop_name=_json_safe(row.get("stop_name")),
            )
    if edges is not None and not edges.empty:
        columns = list(edges.columns if edge_columns is None else edge_columns)
        for _, row in edges.iterrows():
            attrs = {}
            for column in columns:
                if column == "geometry":
                    geom = row.geometry
                    attrs[column] = geom.wkt if geom is not None else None
                else:
                    attrs[column] = _graph_value(row.get(column))
            u, v = str(row.get("from_node")), str(row.get("to_node"))
            _add_missing_gtfs_endpoint(graph, u, row, 0, "from_stop_name")
            _add_missing_gtfs_endpoint(graph, v, row, -1, "to_stop_name")
            graph.add_edge(u, v, **attrs)

    return _write_node_link_json(graph, output_path)


# ---------------------------------------------------------------------------
# Sparse matrix (CSR, matrix.npz)
# ---------------------------------------------------------------------------
def _write_matrices(
    osm_results: dict[str, dict[str, Any]],
    gtfs_results: dict[str, dict[str, Any]],
    layers: dict[str, gpd.GeoDataFrame | None],
    output_dir: Path,
    *,
    area_name: str,
    gtfs_peak_periods: dict[str, tuple[int, int]] | None,
    gtfs_hour_range: tuple[int, int] | None,
) -> list[str]:
    """Write one ``matrix.npz`` per OSM mode and per GTFS dataset.

    Built from the same final layers as the other formats: OSM from the
    ``download_osm`` output, GTFS from the filtered and renamed edge
    layers, so the weights have the short column names (``tts_m``, ...).
    Networks with no edges are skipped. Returns the paths written, relative
    to ``output_dir``.
    """
    # Imported here so ``import multimodalpy`` does not load numpy / pandas.
    from . import _matrix

    written: list[str] = []
    for mode, out in osm_results.items():
        edges = out.get("edges")
        if edges is None or edges.empty:
            continue
        matrix = _matrix.osm_network_matrix(
            out["nodes"], edges, mode=mode, meta={"area": area_name}
        )
        relative = f"{mode}/matrix.npz"
        _matrix.write_matrix(matrix, output_dir / relative)
        written.append(relative)

    periods = (
        gtfs_peak_periods
        if gtfs_peak_periods is not None
        else _gtfs.DEFAULT_PEAK_PERIODS
    )
    for dataset, gtfs_out in gtfs_results.items():
        stops = layers.get(f"gtfs_{dataset}_nodes")
        edges = layers.get(f"gtfs_{dataset}_edges")
        if stops is None or edges is None or edges.empty:
            continue
        mode = gtfs_out.get("mode") or "bus"
        matrix = _matrix.gtfs_network_matrix(
            stops,
            edges,
            mode=mode,
            dataset=dataset,
            periods=periods,
            meta={
                "area": area_name,
                "gtfs_hour_range": (
                    list(gtfs_hour_range) if gtfs_hour_range is not None else None
                ),
            },
        )
        relative = f"{mode}/{dataset}/matrix.npz"
        _matrix.write_matrix(matrix, output_dir / relative)
        written.append(relative)
    return written
