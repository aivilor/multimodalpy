"""Transport networks: download, standardize and write them.

:func:`download_network` is the library's main function: give it a
municipality and the modes, and it writes every network in the formats
requested::

    import multimodalpy as mmp

    mmp.download_network(
        "Valencia",
        modes=["walking", "bike", "bus"],
        output_dir="output",
        formats=["geojson", "matrix"],
    )

:func:`download_osm` and :func:`download_gtfs` return the standardized
layers in memory instead, for a study area from :func:`multimodalpy.find_area`.

Modes are split between the two sources: "walking", "bike" and "driving"
come from OpenStreetMap; "bus" and "train" from the official GTFS feeds of
Spain's National Access Point (NAP), which needs a free API key.
"""

from __future__ import annotations

import logging
import shutil
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import geopandas as gpd
    import pandas as pd

from . import _gtfs
from ._boundaries import find_area
from ._gtfs import GTFS_EDGE_RENAME, download_gtfs
from ._nap import NAPAuthenticationError, missing_nap_key_message, resolve_nap_api_key
from ._osm import OSM_NETWORK_TYPES, download_osm
from ._text import slugify
from ._writers import (
    _gtfs_to_graph_json,
    _osm_layers_to_graph_json,
    _write_layers,
    _write_matrices,
    _write_schedule_tables,
)

__all__ = [
    "FORMATS",
    "NAPAuthenticationError",
    "download_gtfs",
    "download_network",
    "download_osm",
]

logger = logging.getLogger(__name__)


# User mode -> download backend mapping. Any synonym recognized by
# ``_osm.OSM_NETWORK_TYPES`` is accepted (e.g. "driving", "coche",
# "car", "drive" are equivalent); the resulting layer always uses the
# canonical English label ("walking", "bike", "driving").
OSM_MODES = set(OSM_NETWORK_TYPES)
# NAP: 1 = bus, 2 = rail.
GTFS_MODE_TO_NAP = {
    "bus": 1,
    "train": 2,
}

VALID_MODES = OSM_MODES | set(GTFS_MODE_TO_NAP)
#: Output formats accepted by :func:`download_network`.
FORMATS = ("geojson", "shapefile", "geopackage", "networkx", "matrix")


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
def download_network(
    area_name: str,
    modes: Sequence[str] = ("walking",),
    output_dir: str | Path = "output",
    *,
    boundaries_path: str | Path | None = None,
    crs: str = "EPSG:4326",
    formats: str | Sequence[str] = "geojson",
    # Optional extras (not essential for basic use):
    area_code: str | None = None,
    api_key: str | None = None,
    gtfs_hour_band_size: int = 1,
    gtfs_hour_range: tuple[int, int] | None = None,
    gtfs_peak_periods: dict[str, tuple[int, int]] | None = None,
    write_schedule: bool = False,
    keep_gtfs_zips: bool = False,
    province: int | str | None = None,
    buffer_m: float = 0.0,
    connect_parts: bool = True,
) -> dict:
    """Download a municipality's transport networks and write them to disk.

    Finds the municipality (see :func:`multimodalpy.find_area`), downloads
    each mode (walking, bike and driving from OpenStreetMap, bus and train
    from the NAP), standardizes them to the columns in ``DATA_SCHEMA.md``
    and writes them in every format requested.

    Parameters
    ----------
    area_name : str
        Name of the municipality to download.
    modes : list[str]
        Transport modes to download. Options: "walking", "bike", "driving",
        "bus", "train".
    output_dir : str | Path
        Folder to write to. Created if it does not exist.
    boundaries_path : Path, optional
        Shapefile (.shp) or GeoJSON (.geojson) with the geometries to select
        from. If not given, the packaged boundaries shapefile is used.
    crs : str
        Output CRS. Defaults to "EPSG:4326" (WGS84, universal). Set another
        one (e.g. "EPSG:25830") to project the network.
    formats : str | list[str]
        Output format(s): "geojson" (default), "shapefile", "geopackage",
        "networkx" or "matrix". Accepts a list to write several in a single
        pass (e.g. ["geojson", "matrix"]): the download and normalization
        only happen once, and only the writing step is repeated, instead of
        redoing the whole pipeline per format. "matrix" writes one
        ``matrix.npz`` per network: a directed sparse adjacency matrix
        (CSR) with one vector per weight, for shortest-path calculations
        (see "Matrix" in ``DATA_SCHEMA.md``).
    NOTE on the OSM layers (walking/bike/driving): travel time (``tts``, in
        seconds) is computed automatically based on the mode (free-flow
        speed + stop penalty; see "Travel time" in ``DATA_SCHEMA.md``), so
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
    write_schedule : bool
        Also write the trip-by-trip schedule table for each GTFS feed.
        Disabled by default, since it's by far the heaviest part of a
        download.
    api_key : str, optional
        NAP API key, only needed for "bus" / "train". If not given, it's
        read from the ``NAP_API_KEY`` environment variable or a ``.env``
        file in the working folder or one of its parents. It's checked
        before anything is downloaded: without a key, bus/train are
        skipped with a ``UserWarning`` and the other layers are written.
    keep_gtfs_zips : bool
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
    connect_parts : bool
        For municipalities made of several separate parts (e.g.
        Donostia/San Sebastián and its exclaves), add the shortest roads
        joining the parts to the walking/bike/driving networks, marked
        ``connector=True``, so the network is not split into islands.
        Parts that can't be reached are listed in the manifest's
        ``unconnected_parts``. Defaults to True; has no effect on
        single-polygon municipalities.

    Returns
    -------
    dict
        Manifest with the area, the modes, the folder and the files
        written.
    """
    if isinstance(modes, str):
        modes = [modes]

    # ``formats`` accepts a single string ("geojson") or several
    # (["geojson", "shapefile"]). With several, steps 1-3 run only once and
    # only the writing step is repeated.
    single_output_type = isinstance(formats, str)
    requested_types = [formats] if single_output_type else list(formats)
    if not requested_types:
        raise ValueError("formats cannot be empty.")

    output_formats: list[str] = []
    for file_type in requested_types:
        normalized = str(file_type).strip().lower()
        if normalized not in FORMATS:
            raise ValueError(
                f"Format '{file_type}' is not valid. Options: {', '.join(FORMATS)}"
            )
        if normalized not in output_formats:  # ignore duplicates
            output_formats.append(normalized)

    osm_modes, nap_modes = _split_modes(modes)

    # Check the NAP key before downloading anything: otherwise a missing
    # key only shows up after the OSM step, which can take minutes.
    gtfs_error: str | None = None
    if nap_modes:
        api_key = resolve_nap_api_key(api_key)
        if api_key is None:
            gtfs_error = f"ValueError: {missing_nap_key_message()}"
            warnings.warn(
                f"Bus/train will be skipped. {missing_nap_key_message()}",
                UserWarning,
                stacklevel=2,
            )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    area_slug = slugify(area_name)

    # 1) Resolve the study area (always in WGS84 for osmnx / GTFS clipping).
    boundary = find_area(
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
        osm_results = download_osm(
            boundary,
            modes=osm_modes,
            output_crs=crs,
            buffer_m=buffer_m,
            connect_parts=connect_parts,
        )
        for mode, out in osm_results.items():
            layers[f"osm_{mode}_nodes"] = out["nodes"]
            layers[f"osm_{mode}_edges"] = out["edges"]
            layer_paths[f"osm_{mode}_nodes"] = f"{mode}/nodes"
            layer_paths[f"osm_{mode}_edges"] = f"{mode}/edges"

    # 3) GTFS (bus / train) via NAP.
    schedule_tables: dict[str, pd.DataFrame | None] = {}
    gtfs_zips_dir = output_dir / "gtfs_zips"
    if nap_modes and api_key is not None:
        # A failure here (NAP 404, corrupt feed, network outage) shouldn't
        # throw away the OSM layers already built: it's logged, noted in
        # the manifest, and whatever could be obtained is written anyway.
        try:
            gtfs_results = download_gtfs(
                area_name,
                boundary,
                gtfs_zips_dir,
                modes=nap_modes,
                output_crs=crs,
                api_key=api_key,
                hour_band_size=gtfs_hour_band_size,
                hour_range=gtfs_hour_range,
                peak_periods=gtfs_peak_periods,
                include_schedule_table=write_schedule,
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
        for period_name in list(_gtfs.DEFAULT_PEAK_PERIODS.keys()) + ["rest_of_day"]:
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

            if write_schedule and gtfs_out.get("schedule") is not None:
                schedule_tables[f"gtfs_{dataset}_schedule"] = gtfs_out["schedule"]
                layer_paths[f"gtfs_{dataset}_schedule"] = (
                    f"{gtfs_mode}/{dataset}/schedule"
                )

    if nap_modes and not keep_gtfs_zips:
        shutil.rmtree(gtfs_zips_dir, ignore_errors=True)

    # 4) Local write, once per requested format. The download and
    #    normalization (steps 1-3) already happened only once, so
    #    requesting several formats only costs the writing step, not
    #    redoing the whole pipeline.
    written: list[str] = []
    written_by_format: dict[str, list[str]] = {}
    for file_type in output_formats:
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
        elif file_type == "matrix":
            # As with networkx, the boundary goes alongside as GeoJSON so
            # the folder is self-contained.
            boundary.to_crs(crs).to_file(
                output_dir / "study_area_boundary.geojson", driver="GeoJSON"
            )
            files_here = ["study_area_boundary.geojson"]
            files_here.extend(
                _write_matrices(
                    osm_results,
                    gtfs_results,
                    layers,
                    output_dir,
                    area_name=area_name,
                    gtfs_peak_periods=gtfs_peak_periods,
                    gtfs_hour_range=gtfs_hour_range,
                )
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

    manifest: dict[str, Any] = {
        "area_name": area_name,
        "modes": list(modes),
        "crs": crs,
        "buffer_m": buffer_m,
        "connect_parts": connect_parts,
        # Returned as it was requested: str if a single format string was
        # passed, list if several were requested.
        "formats": output_formats[0] if single_output_type else output_formats,
        "output_dir": str(output_dir),
        "osm_modes": osm_modes,
        "gtfs_datasets": list(gtfs_results),
        "files": written,
        **({} if single_output_type else {"files_by_format": written_by_format}),
    }
    unconnected_parts = {
        mode: out["unconnected_parts"]
        for mode, out in osm_results.items()
        if out.get("unconnected_parts")
    }
    if unconnected_parts:
        manifest["unconnected_parts"] = unconnected_parts
    if nap_modes and not gtfs_results:
        # Distinguish "no published datasets" from "the download failed".
        manifest["gtfs_status"] = gtfs_error or "no published datasets"
        # The failure is also logged, but the package logger is silent
        # unless the application configures logging, so without a warning
        # the user just gets a network with no bus/train and no reason.
        # A missing key was already warned about at the start.
        if api_key is not None:
            warnings.warn(
                "No bus/train layers were written: "
                f"{manifest['gtfs_status']}. See manifest['gtfs_status'].",
                UserWarning,
                stacklevel=2,
            )
    return manifest
