"""Downloads data locally according to what the user requested.

Contains the library's main function, ``main()``, which orchestrates the
whole pipeline with the minimal parameters the user needs::

    main(
        area_name,          # str  : municipality to download
        modes,              # list : transport modes
        output_path,        # Path : local download folder
        boundaries_path=None,   # Path: boundaries shapefile/geojson (optional)
        crs="EPSG:4326",        # str : output CRS (defaults to universal WGS84)
        output_file_type="geojson",  # geopackage | geojson | shapefile | networkx
                                     # (or a list of several of them)
    )

Modes are automatically split between OSM and GTFS::

    OSM  -> "walking", "bike", "driving"
    GTFS -> "bus", "trains"
"""

from __future__ import annotations

import json
import logging
import shutil
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import geopandas as gpd
    import networkx as nx
    import pandas as pd

from . import get_area, process_gtfs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Short column names (Shapefile's 10-character limit)
# ---------------------------------------------------------------------------
# Shapefile truncates longer names and resolves collisions with numeric
# suffixes, so the same column ended up named differently depending on the
# format. The short names are applied to all three formats alike.
# The full mapping is in DATA_SCHEMA.md.
_GTFS_PERIOD_ABBR = {"peak_am": "pam", "peak_pm": "ppm", "rest_of_day": "rod"}
_GTFS_DAY_TYPE_ABBR = {"weekday": "wd", "weekend": "we"}


def _build_gtfs_edge_rename() -> dict[str, str]:
    """Build the long-to-short name map for the GTFS edges layer."""
    rename = {
        "route_short_name": "route_sh",
        "route_long_name": "route_ln",
        "trip_count": "trips",
        "days_of_week_summary": "days_week",
        "hourly_travel_times": "h_tts",
        "hourly_trip_counts": "h_trips",
        "travel_time_seconds_mean": "tts_m",
        "travel_time_seconds_median": "tts_md",
        "travel_time_seconds_min": "tts_mn",
        "travel_time_seconds_max": "tts_mx",
    }
    for period, short_period in _GTFS_PERIOD_ABBR.items():
        rename[f"travel_time_seconds_mean_{period}"] = f"tts_m_{short_period}"
        rename[f"travel_time_seconds_median_{period}"] = f"tts_md_{short_period}"
        rename[f"trip_count_{period}"] = f"trips_{short_period}"
    for day_type, short_day in _GTFS_DAY_TYPE_ABBR.items():
        rename[f"travel_time_seconds_mean_{day_type}"] = f"tts_m_{short_day}"
        rename[f"trip_count_{day_type}"] = f"trips_{short_day}"
    # Period x day-type is abbreviated further (tm_/trp_) because fitting
    # all four parts doesn't work within ten characters any other way.
    for period, short_period in _GTFS_PERIOD_ABBR.items():
        for day_type, short_day in _GTFS_DAY_TYPE_ABBR.items():
            suffix = f"{short_period}_{short_day}"
            rename[f"travel_time_seconds_mean_{period}_{day_type}"] = f"tm_{suffix}"
            rename[f"trip_count_{period}_{day_type}"] = f"trp_{suffix}"
    return rename


GTFS_EDGE_RENAME: dict[str, str] = _build_gtfs_edge_rename()


# User mode -> download backend mapping. Any synonym recognized by
# ``get_area.OSM_NETWORK_TYPES`` is accepted (e.g. "driving", "coche",
# "car", "drive" are equivalent); the resulting layer always uses the
# canonical English label ("walking", "bike", "driving").
OSM_MODES = set(get_area.OSM_NETWORK_TYPES)
# NAP: 1 = bus, 2 = rail.
GTFS_MODE_TO_NAP = {
    "bus": 1,
    "train": 2,
}

VALID_MODES = OSM_MODES | set(GTFS_MODE_TO_NAP)
VALID_OUTPUT_TYPES = {"geopackage", "geojson", "shapefile", "networkx"}

# Extension and OGR driver for the formats written layer by layer.
_FILE_FORMATS = {
    "geojson": (".geojson", "GeoJSON"),
    "shapefile": (".shp", "ESRI Shapefile"),
}


# ---------------------------------------------------------------------------
# Export to NetworkX (node-link JSON)
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

def read_graph_json(
    path: str | Path, *, parse_geometry: bool = True
) -> nx.MultiDiGraph:
    """Read a ``graph.json`` written by the networkx export.

    Accepts files with the edge list under ``"edges"`` (current) or
    ``"links"`` (files written with networkx < 3.6 before the key was
    fixed). With ``parse_geometry=True`` each edge's ``geometry`` WKT text
    is turned into a shapely geometry. The CRS of the node ``x`` / ``y``
    and the edge geometries is in ``graph.graph["crs"]``.

    Examples
    --------
    >>> G = read_graph_json("output/walking/graph.json")  # doctest: +SKIP
    >>> G.graph["crs"]  # doctest: +SKIP
    'EPSG:4326'
    """
    from networkx.readwrite import json_graph

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    edges_key = GRAPH_JSON_EDGES_KEY if GRAPH_JSON_EDGES_KEY in data else "links"
    graph = json_graph.node_link_graph(data, edges=edges_key)

    if parse_geometry:
        from shapely import wkt

        for _u, _v, attrs in graph.edges(data=True):
            geom = attrs.get("geometry")
            if isinstance(geom, str):
                attrs["geometry"] = wkt.loads(geom)
    return graph

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
    columns (``process_osm.STANDARD_EDGE_COLUMNS``) as attributes, with
    ``geometry`` as WKT. Graph nodes are the layer's ``node_id`` with
    ``x``, ``y`` and ``node_role``.
    """
    import networkx as nx
    from networkx.readwrite import json_graph

    from .process_osm import STANDARD_EDGE_COLUMNS

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
    graphs. ``main()`` passes the columns kept in the GTFS edge layers, so
    the graph holds the same data as the other formats, under the long
    column names (JSON has no name length limit).
    """
    import networkx as nx
    from networkx.readwrite import json_graph

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


def _relative_name(path: Path, root: Path) -> str:
    """Return path's location relative to root, always using '/' as separator."""
    return path.relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# Writing the schedule table (option B, flat table with no geometry)
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
    - geojson / shapefile / networkx: don't natively support non-spatial
      tables, so they're written as CSV in the dataset's folder.
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
# Writing layers in the requested format
# ---------------------------------------------------------------------------
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
# Mode classification
# ---------------------------------------------------------------------------
def _split_modes(modes: Sequence[str]) -> tuple[list[str], set[int]]:
    osm_modes: list[str] = []
    nap_modes: set[int] = set()
    for mode in modes:
        key = str(mode).strip().lower()
        if key in OSM_MODES:
            osm_modes.append(key)
        elif key in GTFS_MODE_TO_NAP:
            nap_modes.add(GTFS_MODE_TO_NAP[key])
        else:
            valid = ", ".join(sorted(VALID_MODES))
            raise ValueError(f"Unknown mode '{mode}'. Valid modes: {valid}")
    return osm_modes, nap_modes


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------
def main(
    area_name: str,
    modes: Sequence[str] = ("walking",),
    output_path: str | Path = "output",
    *,
    boundaries_path: str | Path | None = None,
    crs: str = "EPSG:4326",
    output_file_type: str | Sequence[str] = "geojson",
    # Optional extras (not essential for basic use):
    area_code: str | None = None,
    api_key: str | None = None,
    gtfs_hour_band_size: int = 1,
    gtfs_hour_range: tuple[int, int] | None = None,
    gtfs_peak_periods: dict[str, tuple[int, int]] | None = None,
    multimodal: bool = False,
    schedule: bool = False,
    gtfs_zips: bool = False,
    province: int | str | None = None,
    buffer_m: float = 0.0,
) -> dict:
    """Download and standardize a municipality's multimodal network.

    Parameters
    ----------
    area_name : str
        Name of the municipality to download.
    modes : list[str]
        Transport modes to download. Options: "walking", "bike", "driving",
        "bus", "train".
    output_path : Path
        Local download folder.
    boundaries_path : Path, optional
        Shapefile (.shp) or GeoJSON (.geojson) with the geometries to select
        from. If not given, the packaged boundaries shapefile is used.
    crs : str
        Output CRS. Defaults to "EPSG:4326" (WGS84, universal). Set another
        one (e.g. "EPSG:25830") to project the network.
    output_file_type : str | list[str]
        Download format(s): "geopackage", "geojson", "shapefile" or
        "networkx". Accepts a list to write several in a single pass (e.g.
        ["geojson", "shapefile", "geopackage"]): the download and
        normalization only happen once, and only the writing step is
        repeated, instead of redoing the whole pipeline per format.
    NOTE on the OSM layers (walking/bike/driving): travel time (``tts``, in
        seconds) is computed automatically based on the mode (free-flow
        speed + stop penalty; see ``process_osm.add_mode_travel_time``), so
        a constant speed parameter is no longer accepted for OSM.
    gtfs_hour_band_size : int, optional
        If set (e.g. 1), GTFS edges are computed per one-hour band (the
        ``hour_band`` column), instead of a single aggregated weight.
    gtfs_hour_range : (int, int), optional
        If set (e.g. (7, 9)), only GTFS trips whose departure falls in that
        hour window are considered (useful for peak vs off-peak).
    gtfs_peak_periods : dict, optional
        Peak periods for the per-column breakdown of the GTFS edges layer
        (option A). Defaults to: peak_am 07-09, peak_pm 17-20, rest_of_day
        for everything else.
    multimodal : bool
        Reserved for the multimodal network, not implemented yet. Only
        accepts False; True raises ``NotImplementedError``.
    schedule : bool
        Also write the trip-by-trip schedule table for each GTFS feed.
        Disabled by default, since it's by far the heaviest part of a
        download.
    gtfs_zips : bool
        Keep the zip files downloaded from the NAP in ``gtfs_zips/``.
        Disabled by default: they're just the raw material for the layers
        and can be downloaded again.
    province : str | int, optional
        Province name or INE code (1-52), to choose between municipalities
        that share a name (e.g. "Castejón" exists in Cuenca and Navarra).
    buffer_m : float
        Widen the download area by this many metres so streets and transit
        routes crossing the municipal boundary are not cut at it. The
        exported ``study_area_boundary`` stays the exact boundary.
        Defaults to 0 (no buffer).

    Returns
    -------
    dict
        Manifest with the area, the modes, the folder and the files
        written.
    """
    if multimodal:
        raise NotImplementedError(
            "The multimodal network isn't implemented yet; main() only "
            "accepts multimodal=False."
        )
    if isinstance(modes, str):
        modes = [modes]

    # ``output_file_type`` accepts a single string ("geojson") or several
    # (["geojson", "shapefile"]). With several, steps 1-3 run only once and
    # only the writing step is repeated.
    single_output_type = isinstance(output_file_type, str)
    requested_types = (
        [output_file_type] if single_output_type else list(output_file_type)
    )
    if not requested_types:
        raise ValueError("output_file_type cannot be empty.")

    output_file_types: list[str] = []
    for file_type in requested_types:
        normalized = str(file_type).strip().lower()
        if normalized not in VALID_OUTPUT_TYPES:
            raise ValueError(
                f"output_file_type '{file_type}' is not valid. "
                f"Options: {', '.join(sorted(VALID_OUTPUT_TYPES))}"
            )
        if normalized not in output_file_types:  # ignore duplicates
            output_file_types.append(normalized)

    osm_modes, nap_modes = _split_modes(modes)
    output_dir = Path(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    area_slug = get_area.slugify(area_name)

    # 1) Resolve the study area (always in WGS84 for osmnx / GTFS clipping).
    boundary = get_area.find_area_boundary(
        area_name,
        boundaries_path,
        area_code=area_code,
        province=province,
        target_crs="EPSG:4326",
    )

    layers: dict[str, gpd.GeoDataFrame | None] = {
        "study_area_boundary": boundary.to_crs(crs)
    }
    # Relative path, without extension, of each layer inside the output folder.
    layer_paths: dict[str, str] = {"study_area_boundary": "study_area_boundary"}
    osm_results: dict[str, dict[str, object]] = {}
    gtfs_results: dict[str, dict[str, Any]] = {}
    # Edge columns kept in each GTFS dataset's layers, reused by the
    # networkx export so the graph carries the same attributes.
    gtfs_edge_columns: dict[str, list[str]] = {}

    # 2) OSM.
    if osm_modes:
        osm_results = get_area.download_osm_layers(
            boundary,
            modes=osm_modes,
            output_crs=crs,
            buffer_m=buffer_m,
        )
        for mode, out in osm_results.items():
            layers[f"osm_{mode}_nodes"] = out["nodes"]
            layers[f"osm_{mode}_edges"] = out["edges"]
            layer_paths[f"osm_{mode}_nodes"] = f"{mode}/nodes"
            layer_paths[f"osm_{mode}_edges"] = f"{mode}/edges"

    # 3) GTFS (bus / train) via NAP.
    schedule_tables: dict[str, pd.DataFrame | None] = {}
    gtfs_error: str | None = None
    gtfs_zips_dir = output_dir / "gtfs_zips"
    if nap_modes:
        # A failure here (NAP 404, corrupt feed, network outage) shouldn't
        # throw away the OSM layers already built: it's logged, noted in
        # the manifest, and whatever could be obtained is written anyway.
        try:
            gtfs_results = get_area.download_gtfs_layers(
                area_name,
                boundary,
                gtfs_zips_dir,
                modes=nap_modes,
                output_crs=crs,
                api_key=api_key,
                hour_band_size=gtfs_hour_band_size,
                hour_range=gtfs_hour_range,
                peak_periods=gtfs_peak_periods,
                include_schedule_table=schedule,
                buffer_m=buffer_m,
            )
        except Exception as exc:  # noqa: BLE001 - logged and execution continues
            gtfs_error = f"{type(exc).__name__}: {exc}"
            gtfs_results = {}
            logger.warning(
                "Could not download GTFS data: %s. "
                "Only the available layers will be written.",
                gtfs_error,
            )
    if gtfs_results:
        GTFS_EDGES_COLUMNS = [
            "edge_id",
            "from_node",
            "to_node",
            "route_id",
            "route_short_name",
            "route_long_name",
            "trip_count",
            "travel_time_seconds_mean",
            "travel_time_seconds_median",
            "travel_time_seconds_min",
            "travel_time_seconds_max",
            "travel_time_seconds_mean_peak_am",
            "travel_time_seconds_mean_peak_pm",
            "travel_time_seconds_mean_rest_of_day",
            "travel_time_seconds_median_peak_am",
            "travel_time_seconds_median_peak_pm",
            "travel_time_seconds_median_rest_of_day",
            "trip_count_peak_am",
            "trip_count_peak_pm",
            "trip_count_rest_of_day",
            "travel_time_seconds_mean_weekday",
            "trip_count_weekday",
            "travel_time_seconds_mean_weekend",
            "trip_count_weekend",
            "days_of_week_summary",
            "hourly_travel_times",
            "hourly_trip_counts",
            "geometry",
        ]
        # Peak-period x weekday/weekend combination (e.g.
        # "travel_time_seconds_mean_peak_am_weekday"). Only covers the
        # default period names (peak_am/peak_pm/rest_of_day); if a custom
        # ``gtfs_peak_periods`` is passed with other names, its combined
        # columns aren't filtered here automatically.
        for period_name in list(process_gtfs.DEFAULT_PEAK_PERIODS.keys()) + [
            "rest_of_day"
        ]:
            for day_type_name in ("weekday", "weekend"):
                GTFS_EDGES_COLUMNS.append(
                    f"travel_time_seconds_mean_{period_name}_{day_type_name}"
                )
                GTFS_EDGES_COLUMNS.append(f"trip_count_{period_name}_{day_type_name}")

        GTFS_NODES_COLUMNS = ["node_id", "stop_name", "geometry"]

        for dataset, gtfs_out in gtfs_results.items():
            nodes_gdf = gtfs_out["nodes"]
            keep_node_cols = [c for c in GTFS_NODES_COLUMNS if c in nodes_gdf.columns]
            gtfs_mode = gtfs_out.get("mode") or "bus"

            layers[f"gtfs_{dataset}_nodes"] = nodes_gdf[keep_node_cols]
            layer_paths[f"gtfs_{dataset}_nodes"] = f"{gtfs_mode}/{dataset}/nodes"

            edges_gdf = gtfs_out["edges"]
            keep_cols = [c for c in GTFS_EDGES_COLUMNS if c in edges_gdf.columns]
            gtfs_edge_columns[dataset] = keep_cols
            edges_gdf = edges_gdf[keep_cols].rename(columns=GTFS_EDGE_RENAME)

            layers[f"gtfs_{dataset}_edges"] = edges_gdf
            layer_paths[f"gtfs_{dataset}_edges"] = f"{gtfs_mode}/{dataset}/edges"

            if schedule and gtfs_out.get("schedule") is not None:
                schedule_tables[f"gtfs_{dataset}_schedule"] = gtfs_out["schedule"]
                layer_paths[f"gtfs_{dataset}_schedule"] = f"{gtfs_mode}/{dataset}/schedule"
                

    if nap_modes and not gtfs_zips:
        shutil.rmtree(gtfs_zips_dir, ignore_errors=True)

    # 4) Local write, once per requested format. The download and
    #    normalization (steps 1-3) already happened only once, so
    #    requesting several formats only costs the writing step, not
    #    redoing the whole pipeline.
    written: list[str] = []
    written_by_format: dict[str, list[str]] = {}
    for file_type in output_file_types:
        if file_type == "networkx":
            files_here: list[str] = []
            # The boundary and the GTFS shapes layers are saved as
            # supporting GeoJSON.
            boundary.to_crs(crs).to_file(
                output_dir / "study_area_boundary.geojson", driver="GeoJSON"
            )
            files_here.append("study_area_boundary.geojson")
            for mode, out in osm_results.items():
                relative = f"{mode}/graph.json"
                _osm_layers_to_graph_json(
                    out["nodes"], out["edges"], output_dir / relative
                )
                files_here.append(relative)
            for dataset, gtfs_out in gtfs_results.items():
                relative = f"{gtfs_out.get('mode') or 'bus'}/{dataset}/graph.json"
                _gtfs_to_graph_json(
                    gtfs_out["nodes"],
                    gtfs_out["edges"],
                    output_dir / relative,
                    edge_columns=gtfs_edge_columns.get(dataset),
                )
                files_here.append(relative)
            files_here.extend(
                _write_schedule_tables(
                    schedule_tables,
                    output_dir,
                    file_type,
                    area_slug=area_slug,
                    layer_paths=layer_paths,
                )
            )
        else:
            files_here = _write_layers(
                layers,
                output_dir,
                file_type,
                area_slug=area_slug,
                layer_paths=layer_paths,
            )
            files_here.extend(
                _write_schedule_tables(
                    schedule_tables,
                    output_dir,
                    file_type,
                    area_slug=area_slug,
                    layer_paths=layer_paths,
                )
            )
        written_by_format[file_type] = files_here
        written.extend(files_here)

    manifest = {
        "area_name": area_name,
        "modes": list(modes),
        "crs": crs,
        "buffer_m": buffer_m,
        # Returned as it was requested: str if a single format string was
        # passed, list if several were requested.
        "output_file_type": output_file_types[0]
        if single_output_type
        else output_file_types,
        "output_path": str(output_dir),
        "osm_modes": osm_modes,
        "gtfs_datasets": list(gtfs_results),
        "files": written,
        **({} if single_output_type else {"files_by_format": written_by_format}),
    }
    if nap_modes and not gtfs_results:
        # Distinguish "no published datasets" from "the download failed".
        manifest["gtfs_status"] = gtfs_error or "no published datasets"
        # The failure is also logged, but the package logger is silent
        # unless the application configures logging, so without a warning
        # the user just gets a network with no bus/train and no reason.
        warnings.warn(
            "No bus/train layers were written: "
            f"{manifest['gtfs_status']}. See manifest['gtfs_status'].",
            UserWarning,
            stacklevel=2,
        )
    return manifest


# ---------------------------------------------------------------------------
# Optional CLI: python -m multimodalpy.get_network --area "Valencia" ...
# ---------------------------------------------------------------------------
def _cli(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Download a municipality's multimodal network."
    )
    parser.add_argument("--area", required=True, help="Municipality name.")
    parser.add_argument(
        "--modes", nargs="+", default=["walking"], help="Transport modes."
    )
    parser.add_argument("--output", default="output", help="Local download folder.")
    parser.add_argument("--boundaries", help="Boundaries shapefile/GeoJSON (optional).")
    parser.add_argument(
        "--crs", default="EPSG:4326", help="Output CRS (defaults to WGS84)."
    )
    parser.add_argument(
        "--output-file-type",
        default=["geojson"],
        nargs="+",
        choices=sorted(VALID_OUTPUT_TYPES),
        help="Output format(s); accepts several in a single pass.",
    )
    parser.add_argument(
        "--area-code",
        help="Municipality's 5-digit INE code or INSPIRE NATCODE (optional).",
    )
    parser.add_argument(
        "--province",
        help="Province name or INE code, to choose between same-name towns.",
    )
    parser.add_argument(
        "--buffer-m",
        type=float,
        default=0.0,
        help="Widen the download area by this many metres (default 0).",
    )
    parser.add_argument(
        "--clean-gtfs", action="store_true", help="Apply bus/train cleanup."
    )
    parser.add_argument(
        "--gtfs-hour-band-size",
        type=int,
        default=1,
        help=("Hour band (in hours) for the packed GTFS edges summary (option C)."),
    )
    parser.add_argument(
        "--gtfs-hour-range",
        type=int,
        nargs=2,
        metavar=("START", "END"),
        help="Filter GTFS trips by hour window, e.g. --gtfs-hour-range 7 9",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Also write the detailed schedule table for each GTFS feed.",
    )
    parser.add_argument(
        "--gtfs-zips",
        action="store_true",
        help=(
            "Keep the GTFS zip files downloaded from the NAP instead of deleting them."
        ),
    )
    args = parser.parse_args(argv)

    main(
        area_name=args.area,
        modes=args.modes,
        output_path=args.output,
        boundaries_path=args.boundaries,
        crs=args.crs,
        output_file_type=args.output_file_type,
        area_code=args.area_code,
        province=args.province,
        buffer_m=args.buffer_m,
        gtfs_hour_band_size=args.gtfs_hour_band_size,
        gtfs_hour_range=tuple(args.gtfs_hour_range) if args.gtfs_hour_range else None,
        schedule=args.schedule,
        gtfs_zips=args.gtfs_zips,
    )


if __name__ == "__main__":
    _cli()