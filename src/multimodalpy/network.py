"""Transport networks: download, standardize and write them.

:func:`download_network` is the library's main function: give it a
municipality (or a file with your own study area) and the modes, and it
writes every network in the formats requested::

    import multimodalpy as mmp

    mmp.download_network(
        "Valencia",
        modes=["walking", "bike", "bus"],
        output_dir="output",
        formats=["geojson", "matrix"],
    )
    mmp.download_network(boundaries_path="metro_area.geojson", modes=["bus"])

:func:`download_osm` and :func:`download_gtfs` return the standardized
layers in memory instead, for a study area from :func:`multimodalpy.find_area`.

Modes are split between the two sources: "walking", "bike" and "driving"
come from OpenStreetMap; "bus" and "train" from the official GTFS feeds of
Spain's National Access Point (NAP), which needs a free API key, and from
any feeds of your own (``gtfs_feeds``).
"""

from __future__ import annotations

import datetime
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
from ._boundaries import boundary_for_export, find_area
from ._gtfs import download_gtfs
from ._nap import NAPAuthenticationError, missing_nap_key_message, resolve_nap_api_key
from ._osm import OSM_NETWORK_TYPES, download_osm
from ._overpass import OSMDownloadError
from ._text import slugify
from ._writers import (
    MANIFEST_FILE,
    _gtfs_to_graph_json,
    _osm_layers_to_graph_json,
    _write_layers,
    _write_matrices,
    _write_schedule_tables,
    write_manifest,
)

__all__ = [
    "FORMATS",
    "NAPAuthenticationError",
    "OSMDownloadError",
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
#: Columns of ``gtfs_report`` (manifest and ``gtfs_report.csv``), in order.
_GTFS_REPORT_COLUMNS = (
    "source",
    "province_code",
    "dataset_id",
    "dataset_name",
    "transport_types",
    "file_id",
    "file_name",
    "status",
    "dataset",
    "mode",
    "routes",
    "stops",
    "edges",
    "error",
)


def _gtfs_report_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the report rows with the public columns, in order, JSON-ready."""
    clean = []
    for row in rows:
        out = {}
        for column in _GTFS_REPORT_COLUMNS:
            value = row.get(column)
            if value is not None and value != value:  # NaN
                value = None
            out[column] = value
        clean.append(out)
    return clean


def _unusable_gtfs_datasets(rows: list[dict[str, object]]) -> list[str]:
    """List the NAP datasets that gave nothing: every file or processing failed.

    A dataset that got at least one file is fine even if others failed (the
    NAP lists files it no longer serves, e.g. some of Cercanías Renfe's).
    """
    by_dataset: dict[object, list[dict[str, object]]] = {}
    for row in rows:
        key = row.get("dataset_id") or row.get("dataset_name") or row.get("file_name")
        by_dataset.setdefault(key, []).append(row)
    unusable = []
    for dataset_rows in by_dataset.values():
        statuses = {row["status"] for row in dataset_rows}
        failed = statuses & {"failed", "failed_processing"}
        if failed and not statuses & {"written", "nothing_in_area", "skipped_mode"}:
            row = next(r for r in dataset_rows if r["status"] in failed)
            name = row.get("dataset_name") or row.get("dataset") or row.get("file_name")
            error = str(row.get("error") or row["status"])
            unusable.append(f"{name} ({error[:120]})")
    return unusable


def download_network(
    area_name: str | None = None,
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
    gtfs_service_date: str | datetime.date | None = None,
    gtfs_feeds: _gtfs.GTFSFeeds | None = None,
    write_schedule: bool = False,
    keep_gtfs_zips: bool = False,
    province: int | str | None = None,
    buffer_m: float = 0.0,
    connect_parts: bool = True,
    overpass_url: str | None = None,
    osm_timeout: float | None = None,
) -> dict:
    """Download a study area's transport networks and write them to disk.

    Finds the study area (see :func:`multimodalpy.find_area`): a
    municipality by name, or the polygon in ``boundaries_path``. Then it
    downloads each mode (walking, bike and driving from OpenStreetMap, bus
    and train from the NAP), standardizes them to the columns in
    ``DATA_SCHEMA.md`` and writes them in every format requested.

    Parameters
    ----------
    area_name : str, optional
        Name of the municipality to download. With ``boundaries_path`` it is
        only the label of the output files (``<area_name>.gpkg``) and the
        manifest, and defaults to the file name.
    modes : list[str]
        Transport modes to download. Options: "walking", "bike", "driving",
        "bus", "train".
    output_dir : str | Path
        Folder to write to. Created if it does not exist.
    boundaries_path : str | Path, optional
        Shapefile (.shp), GeoJSON (.geojson) or GeoPackage (.gpkg) with
        your own study area: its polygons (merged into one) are the area,
        whatever their columns, in any CRS (the file must define one). The
        provinces holding at least 5% of it give the bus and train feeds;
        everything is clipped to the polygon. It must overlap peninsular
        Spain, the Balearic Islands, Ceuta or Melilla. Not combined with
        ``area_code`` or ``province``.
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
    gtfs_hour_band_size : int
        Width in hours of the bands of the GTFS edges' hourly summary
        (``h_tts`` and ``h_trips``, e.g. ``"07-08:300;08-09:420"``).
        Defaults to 1; one of 1, 2, 3, 4, 6, 8, 12 or 24, so the bands
        split the day evenly. It changes only that summary: the other
        columns and the matrix weights are the same whatever the band size.
    gtfs_hour_range : (int, int), optional
        If set (e.g. (7, 9)), only GTFS trips whose departure falls in that
        hour window are considered (useful for peak vs off-peak): from the
        start hour (0 to 23) up to, not including, the end hour (0 to 24).
        A range may cross midnight: ``(22, 2)`` keeps the departures from
        22:00 to 01:59. Times past midnight in the feed (``25:10:00``)
        count as that hour of the day (01).
    gtfs_peak_periods : dict, optional
        Peak periods for the per-column breakdown of the GTFS edges layer
        (option A). Defaults to: peak_am 07-09, peak_pm 17-20, rest_of_day
        for everything else. Periods with names of your own, e.g.
        ``{"morning": (6, 9), "evening": (18, 21)}``, get a three-letter
        code in the short column names (``tts_m_mor``, ``tm_eve_wd``; or
        ``p1``, ``p2``... if the first three letters clash), listed in the
        manifest's ``gtfs_period_codes``. Hours as in ``gtfs_hour_range``
        (a period may cross midnight, ``{"night": (22, 6)}``); periods
        cannot overlap.
    gtfs_service_date : str | datetime.date, optional
        Count the GTFS trips of this day only (``"2026-11-25"``,
        ``"20261125"`` or a ``datetime.date``): the trips whose service runs
        that day, as the feed's ``calendar.txt`` and ``calendar_dates.txt``
        say. Then ``trips`` and the per-period columns are that day's
        frequencies, and the weekday/weekend columns are filled for its type
        of day only. By default every trip of the feed counts once, whatever
        days it runs, so a feed with several timetables (winter and summer,
        Monday-Thursday and Friday) adds them up. A feed with no trip that
        day is not written (``no_service_on_date`` in ``gtfs_report``, with
        the dates its calendar covers) and raises a ``UserWarning``.
    gtfs_feeds : dict | list, optional
        GTFS feeds from outside the NAP, processed like the NAP's for the
        "bus"/"train" modes asked for: ``{name: source}`` (the name is the
        output folder, ``bus/<name>/``), or a list of sources, named after
        their files. A source is an http(s) address, downloaded into
        ``gtfs_zips/``, or a local GTFS zip or folder, e.g.
        ``{"my_operator": "https://example.org/gtfs.zip"}``. Check that a
        feed is not on the NAP already, or its routes appear twice.
        Without a NAP API key, the NAP is skipped with a ``UserWarning`` and
        only these feeds are used. Each one has a row in ``gtfs_report``
        with ``source="user"``.
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
    area_code : str, optional
        The municipality's 5-digit INE code or INSPIRE NATCODE, to pick it
        exactly (then ``area_name`` is not needed).
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
    overpass_url : str, optional
        Overpass API server for the OpenStreetMap networks (all the modes
        asked for are downloaded with a single query): its ``.../api``
        address (e.g. ``"https://overpass.private.coffee/api"``; see the
        list of public instances in the OpenStreetMap wiki). Defaults to
        ``osmnx.settings.overpass_url`` (the public ``overpass-api.de``,
        often at capacity). A temporary failure is retried four times
        (10, 20, 40 and 80 s) before giving up; there's no automatic switch
        to another server.
    osm_timeout : float, optional
        Seconds to wait for each Overpass answer, also the time limit the
        server is asked to respect. Defaults to osmnx's 180 s; a large area
        may need more.

    Returns
    -------
    dict
        Manifest with the area (with its ``provinces`` and
        ``municipalities``), the modes, the folder and the files written,
        also saved as ``manifest.json`` in ``output_dir`` (its
        ``manifest_file``), with the ``multimodalpy_version`` that wrote it.

        ``gtfs_report`` (also written as ``gtfs_report.csv``) says what
        happened to every NAP dataset of the area's provinces and to every
        feed of ``gtfs_feeds`` (``source`` ``"nap"`` or ``"user"``): ``written``
        (with its ``routes``, ``stops`` and ``edges`` in the area),
        ``nothing_in_area``, ``skipped_mode``, ``not_requested_type`` (its
        declared ``transport_types`` are not among the modes asked for),
        ``no_service_on_date`` (with ``gtfs_service_date``), ``failed``
        (download) or ``failed_processing``, with the ``error``.
        A dataset that could not be used at all raises a ``UserWarning``.

        A GTFS feed whose routes are of a mode that wasn't requested
        (the NAP declares some feeds as both bus and rail) is skipped and
        listed in the manifest's ``skipped_datasets`` (``{dataset: mode}``).

        If an OpenStreetMap network can't be downloaded (see
        ``overpass_url``), the other networks are still downloaded and
        written: the failure is listed in the manifest's ``failed_modes``
        and reported with a ``UserWarning``. The walking, bike and driving
        networks come from a single Overpass query, so when the server
        fails it, all of them are listed.

    Raises
    ------
    OSMDownloadError
        If no network at all could be downloaded because the Overpass
        server failed (at capacity, timeout, lost connection).
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
    # Wrong hour options fail before anything is downloaded.
    _gtfs.check_hour_options(gtfs_hour_range, gtfs_hour_band_size, gtfs_peak_periods)
    # So does a malformed date.
    service_date = (
        None
        if gtfs_service_date is None
        else _gtfs.parse_service_date(gtfs_service_date)
    )
    # So do a missing local feed and gtfs_feeds without bus or train.
    feed_sources = _gtfs.user_feed_sources(gtfs_feeds)
    if feed_sources and not nap_modes:
        raise ValueError(
            "gtfs_feeds needs 'bus' or 'train' in modes: only the feeds of the "
            "modes asked for are processed."
        )

    # Check the NAP key before downloading anything: otherwise a missing
    # key only shows up after the OSM step, which can take minutes.
    gtfs_error: str | None = None
    if nap_modes:
        api_key = resolve_nap_api_key(api_key)
        if api_key is None:
            gtfs_error = f"ValueError: {missing_nap_key_message()}"
            skipped_what = (
                "The NAP will be skipped and only gtfs_feeds used."
                if feed_sources
                else "Bus/train will be skipped."
            )
            warnings.warn(
                f"{skipped_what} {missing_nap_key_message()}",
                UserWarning,
                stacklevel=2,
            )

    # 1) Resolve the study area (always in WGS84 for osmnx / GTFS clipping),
    #    before creating anything: a wrong area fails without side effects.
    boundary = find_area(
        area_name,
        boundaries_path,
        area_code=area_code,
        province=province,
        target_crs="EPSG:4326",
    )
    # The label find_area settled on: the name given, the municipality's
    # name (with area_code alone) or the boundaries file's name.
    area_name = str(boundary["area_name"].iloc[0])

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    osm_results: dict[str, dict[str, Any]] = {}
    gtfs_results: dict[str, dict[str, Any]] = {}

    # 2) OSM, every mode with a single Overpass query. A network that can't
    #    be downloaded (after the retries) is recorded instead of raised: the
    #    others, and the GTFS, are still written, and the failure is
    #    reported at the end and in the manifest.
    osm_failures: dict[str, OSMDownloadError] = {}
    if osm_modes:
        osm_results.update(
            download_osm(
                boundary,
                modes=osm_modes,
                output_crs=crs,
                buffer_m=buffer_m,
                connect_parts=connect_parts,
                overpass_url=overpass_url,
                osm_timeout=osm_timeout,
                failures=osm_failures,
            )
        )
        for exc in dict.fromkeys(osm_failures.values()):
            logger.warning("%s The other networks are still downloaded.", exc)

    # 3) GTFS (bus / train) via NAP.
    gtfs_zips_dir = output_dir / "gtfs_zips"
    skipped_datasets: dict[str, str] = {}
    gtfs_report: list[dict[str, object]] = []
    if nap_modes and (api_key is not None or feed_sources):
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
                skipped=skipped_datasets,
                report=gtfs_report,
                service_date=service_date,
                feeds=gtfs_feeds,
                use_nap=api_key is not None,
            )
        except Exception as exc:  # noqa: BLE001 - logged and execution continues
            gtfs_error = f"{type(exc).__name__}: {exc}"
            gtfs_results = {}
            logger.warning(
                "Could not download GTFS data: %s. "
                "Only the available layers will be written.",
                gtfs_error,
            )
    # Nothing at all could be downloaded: there is nothing to write, so the
    # (first) OSM failure is raised instead of an empty result.
    if osm_failures and not osm_results and not gtfs_results:
        raise next(iter(osm_failures.values()))

    # Never delete a feed of the user's own: gtfs_feeds may point at a zip
    # kept from an earlier run (keep_gtfs_zips=True) in this same folder.
    own_feed_inside = any(
        Path(source).resolve().is_relative_to(gtfs_zips_dir.resolve())
        for _, source in feed_sources
        if not _gtfs._is_url(source)
    )
    if nap_modes and not keep_gtfs_zips and not own_feed_inside:
        shutil.rmtree(gtfs_zips_dir, ignore_errors=True)

    return _write_networks(
        boundary,
        output_dir,
        output_formats=output_formats,
        single_output_type=single_output_type,
        crs=crs,
        osm_results=osm_results,
        gtfs_results=gtfs_results,
        gtfs_report=gtfs_report,
        gtfs_peak_periods=gtfs_peak_periods,
        gtfs_hour_range=gtfs_hour_range,
        service_date=service_date,
        write_schedule=write_schedule,
        manifest_head={
            "boundaries_path": None
            if boundaries_path is None
            else str(boundaries_path),
            "modes": list(modes),
            "buffer_m": buffer_m,
            "connect_parts": connect_parts,
        },
        osm_modes=osm_modes,
        osm_failures=osm_failures,
        gtfs_requested=bool(nap_modes),
        gtfs_error=gtfs_error,
        warn_no_gtfs=api_key is not None,
        feed_sources=feed_sources,
        skipped_datasets=skipped_datasets,
    )


def _write_networks(
    boundary: gpd.GeoDataFrame,
    output_dir: Path,
    *,
    output_formats: list[str],
    single_output_type: bool,
    crs: str,
    osm_results: dict[str, dict[str, Any]],
    gtfs_results: dict[str, dict[str, Any]],
    gtfs_report: list[dict[str, object]],
    gtfs_peak_periods: dict[str, tuple[int, int]] | None,
    gtfs_hour_range: tuple[int, int] | None,
    service_date: datetime.date | None,
    write_schedule: bool,
    manifest_head: dict[str, Any],
    osm_modes: list[str],
    osm_failures: dict[str, OSMDownloadError],
    gtfs_requested: bool,
    gtfs_error: str | None,
    warn_no_gtfs: bool,
    feed_sources: list[tuple[str | None, str]],
    skipped_datasets: dict[str, str],
) -> dict:
    """Write the networks in every format, with the report and the manifest.

    The part of :func:`download_network` that comes after getting the
    data, shared by every way of getting it. ``osm_results`` and
    ``gtfs_results`` are the standardized layers (``{"nodes", "edges"}``
    per OSM mode; ``{"nodes", "edges", "schedule", "mode"}`` per GTFS
    dataset), ``boundary`` the study area as :func:`find_area` returns it
    and ``manifest_head`` the manifest entries that depend on how the data
    was obtained. Returns the manifest, also saved as ``manifest.json``.
    The warnings point at the caller of the public function.
    """
    import pandas as pd

    area_name = str(boundary["area_name"].iloc[0])
    area_slug = slugify(area_name)
    # The study area as written: its list columns (provinces and
    # municipalities) joined as text, which every format can store.
    boundary_layer = boundary_for_export(boundary).to_crs(crs)
    layers: dict[str, gpd.GeoDataFrame | None] = {"study_area_boundary": boundary_layer}
    # Relative path, without extension, of each layer inside the output folder.
    layer_paths: dict[str, str] = {"study_area_boundary": "study_area_boundary"}
    # Edge columns kept in each GTFS dataset's layers, reused by the
    # networkx export so the graph carries the same attributes.
    gtfs_edge_columns: dict[str, list[str]] = {}
    schedule_tables: dict[str, pd.DataFrame | None] = {}
    for mode, out in osm_results.items():
        layers[f"osm_{mode}_nodes"] = out["nodes"]
        layers[f"osm_{mode}_edges"] = out["edges"]
        layer_paths[f"osm_{mode}_nodes"] = f"{mode}/nodes"
        layer_paths[f"osm_{mode}_edges"] = f"{mode}/edges"

    if gtfs_results:
        # Every column written, including the per-period ones of the
        # periods actually used (``gtfs_peak_periods`` or the defaults).
        GTFS_EDGES_COLUMNS = _gtfs.gtfs_edge_columns(gtfs_peak_periods)
        gtfs_rename = _gtfs.gtfs_edge_rename(gtfs_peak_periods)

        GTFS_NODES_COLUMNS = [
            "node_id",
            "stop_name",
            "station_id",
            "in_area",
            "geometry",
        ]

        for dataset, gtfs_out in gtfs_results.items():
            nodes_gdf = gtfs_out["nodes"]
            keep_node_cols = [c for c in GTFS_NODES_COLUMNS if c in nodes_gdf.columns]
            gtfs_mode = gtfs_out.get("mode") or "bus"

            layers[f"gtfs_{dataset}_nodes"] = nodes_gdf[keep_node_cols]
            layer_paths[f"gtfs_{dataset}_nodes"] = f"{gtfs_mode}/{dataset}/nodes"

            edges_gdf = gtfs_out["edges"]
            keep_cols = [c for c in GTFS_EDGES_COLUMNS if c in edges_gdf.columns]
            gtfs_edge_columns[dataset] = keep_cols
            edges_gdf = edges_gdf[keep_cols].rename(columns=gtfs_rename)

            layers[f"gtfs_{dataset}_edges"] = edges_gdf
            layer_paths[f"gtfs_{dataset}_edges"] = f"{gtfs_mode}/{dataset}/edges"

            if write_schedule and gtfs_out.get("schedule") is not None:
                schedule_tables[f"gtfs_{dataset}_schedule"] = gtfs_out["schedule"]
                layer_paths[f"gtfs_{dataset}_schedule"] = (
                    f"{gtfs_mode}/{dataset}/schedule"
                )

    # What happened to every NAP dataset, next to the layers: the copy the
    # NAP client writes in gtfs_zips/ is gone unless keep_gtfs_zips=True.
    gtfs_report_rows = _gtfs_report_rows(gtfs_report)
    if gtfs_report_rows:
        pd.DataFrame(gtfs_report_rows, columns=list(_GTFS_REPORT_COLUMNS)).to_csv(
            output_dir / "gtfs_report.csv", index=False
        )

    # 4) Local write, once per requested format. The download and
    #    normalization (steps 1-3) already happened only once, so
    #    requesting several formats only costs the writing step, not
    #    redoing the whole pipeline.
    written: list[str] = []
    written_by_format: dict[str, list[str]] = {}
    for file_type in output_formats:
        if file_type == "networkx":
            files_here: list[str] = []
            # The boundary is saved as supporting GeoJSON.
            boundary_layer.to_file(
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
            boundary_layer.to_file(
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
                    gtfs_service_date=service_date,
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

    area_row = boundary.iloc[0]
    from . import __version__

    manifest: dict[str, Any] = {
        "multimodalpy_version": __version__,
        "area_name": area_name,
        "boundaries_path": manifest_head.pop("boundaries_path", None),
        "provinces": [
            {"code": int(code), "name": name}
            for code, name in zip(
                area_row.get("prov_codes") or [],
                area_row.get("prov_names") or [],
                strict=True,
            )
        ],
        "municipalities": [
            {"code": code, "name": name}
            for code, name in zip(
                area_row.get("mun_codes") or [],
                area_row.get("mun_names") or [],
                strict=True,
            )
        ],
        "modes": manifest_head.pop("modes"),
        "crs": crs,
        **manifest_head,
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
    if service_date is not None and gtfs_requested:
        manifest["gtfs_service_date"] = service_date.isoformat()
    if feed_sources:
        manifest["gtfs_feeds"] = [
            {"name": name, "source": source} for name, source in feed_sources
        ]
    if gtfs_results:
        # Code of each period in the short column names (tts_m_pam, tm_mor_wd).
        manifest["gtfs_period_codes"] = _gtfs.period_codes(gtfs_peak_periods)
    if gtfs_report_rows:
        manifest["gtfs_report"] = gtfs_report_rows
        unusable = _unusable_gtfs_datasets(gtfs_report_rows)
        if unusable:
            warnings.warn(
                f"{len(unusable)} GTFS dataset(s) could not be used: "
                + "; ".join(unusable)
                + ". See manifest['gtfs_report'] or gtfs_report.csv.",
                UserWarning,
                stacklevel=3,
            )
        duplicates = [
            f"{row['dataset']} ({row['error']})"
            for row in gtfs_report_rows
            if row["status"] == "duplicate"
        ]
        if duplicates:
            warnings.warn(
                f"{len(duplicates)} GTFS feed(s) repeat another one and were not "
                "written: " + "; ".join(duplicates) + ".",
                UserWarning,
                stacklevel=3,
            )
        no_service = [
            f"{row['dataset']} ({row['error']})"
            for row in gtfs_report_rows
            if row["status"] == "no_service_on_date"
        ]
        if no_service:
            warnings.warn(
                f"{len(no_service)} GTFS dataset(s) have no trips on "
                f"{manifest['gtfs_service_date']} and were not written: "
                + "; ".join(no_service)
                + ".",
                UserWarning,
                stacklevel=3,
            )
    if skipped_datasets:
        # Feeds the NAP returned for a requested mode whose routes turned
        # out to be of another one (e.g. Cercanías when asking for bus).
        manifest["skipped_datasets"] = skipped_datasets
    if osm_failures:
        manifest["failed_modes"] = {
            mode: str(exc) for mode, exc in osm_failures.items()
        }
        first = next(iter(osm_failures.values()))
        warnings.warn(
            f"The {', '.join(osm_failures)} network(s) could not be downloaded "
            f"and were not written: {first} See manifest['failed_modes'].",
            UserWarning,
            stacklevel=3,
        )
    if gtfs_requested and not gtfs_results:
        # Distinguish "no published datasets" from "the download failed".
        manifest["gtfs_status"] = gtfs_error or "no published datasets"
        # The failure is also logged, but the package logger is silent
        # unless the application configures logging, so without a warning
        # the user just gets a network with no bus/train and no reason.
        # A missing key was already warned about at the start.
        if warn_no_gtfs:
            warnings.warn(
                "No bus/train layers were written: "
                f"{manifest['gtfs_status']}. See manifest['gtfs_status'].",
                UserWarning,
                stacklevel=3,
            )
    # Saved next to the data, so what the call did is still there after it
    # (from the command line there is no returned dict to look at).
    manifest["manifest_file"] = MANIFEST_FILE
    write_manifest(manifest, output_dir)
    return manifest
