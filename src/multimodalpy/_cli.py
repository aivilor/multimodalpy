"""Command line: ``multimodalpy --area Valencia --modes walking bike``.

The study area is a municipality (``--area``) or your own polygon
(``--boundaries metro_area.geojson``).

``multimodalpy ...`` downloads (:func:`multimodalpy.download_network`);
``multimodalpy build ...`` builds the networks from files you already have,
without downloading anything (:func:`multimodalpy.build_network`). Also runs
as ``python -m multimodalpy``. Every option maps to an argument of those
functions; ``multimodalpy --help`` and ``multimodalpy build --help`` list
them.

Private module.
"""

from __future__ import annotations

import argparse
import re

from . import network
from .network import FORMATS


def _add_area_options(parser: argparse.ArgumentParser) -> None:
    """Add the study area options, as in download_network and build_network."""
    parser.add_argument(
        "--area",
        help=(
            "Municipality name. With --boundaries, only the output label "
            "(defaults to the file name)."
        ),
    )
    parser.add_argument(
        "--boundaries",
        help=(
            "Shapefile/GeoJSON/GeoPackage with your own study area (any CRS); "
            "replaces the search by name."
        ),
    )
    parser.add_argument(
        "--area-code",
        help="Municipality's 5-digit INE code or INSPIRE NATCODE (optional).",
    )
    parser.add_argument(
        "--province",
        help="Province name or INE code, to choose between same-name towns.",
    )


def _add_output_options(parser: argparse.ArgumentParser, *, what: str) -> None:
    """Add where and how the networks are written, and the clipping buffer."""
    parser.add_argument(
        "--output-dir", default="output", help="Folder to write to (default: output)."
    )
    parser.add_argument(
        "--crs", default="EPSG:4326", help="Output CRS (defaults to WGS84)."
    )
    parser.add_argument(
        "--formats",
        default=["geojson"],
        nargs="+",
        choices=FORMATS,
        help="Output format(s); accepts several in a single pass.",
    )
    parser.add_argument(
        "--buffer-m",
        type=float,
        default=0.0,
        help=f"Widen the {what} area by this many metres (default 0).",
    )


def _add_gtfs_options(parser: argparse.ArgumentParser) -> None:
    """Add how the GTFS feeds are summarized."""
    parser.add_argument(
        "--gtfs-hour-band-size",
        type=int,
        default=1,
        help="Hours per band of the GTFS edges' hourly summary (default 1).",
    )
    parser.add_argument(
        "--gtfs-hour-range",
        type=int,
        nargs=2,
        metavar=("START", "END"),
        help=(
            "Keep only the GTFS trips leaving in this hour window, e.g. "
            "--gtfs-hour-range 7 9; it may cross midnight (22 2)."
        ),
    )
    parser.add_argument(
        "--gtfs-peak-period",
        action="append",
        metavar="NAME=START-END",
        help=(
            "A period of the day for the GTFS per-period columns, e.g. "
            "morning=7-9 (it may cross midnight: night=22-6). Repeat it for "
            "several; they replace the default peak_am 7-9 and peak_pm 17-20."
        ),
    )
    parser.add_argument(
        "--gtfs-service-date",
        metavar="YYYY-MM-DD",
        help="Count only the GTFS trips that run on this day.",
    )
    parser.add_argument(
        "--write-schedule",
        action="store_true",
        help="Also write the detailed schedule table for each GTFS feed.",
    )


def _gtfs_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict:
    """Return the GTFS keyword arguments of download_network/build_network."""
    try:
        peak_periods = _peak_periods_argument(args.gtfs_peak_period)
    except ValueError as exc:
        parser.error(str(exc))
    return {
        "gtfs_hour_band_size": args.gtfs_hour_band_size,
        "gtfs_hour_range": (
            tuple(args.gtfs_hour_range) if args.gtfs_hour_range else None
        ),
        "gtfs_peak_periods": peak_periods,
        "gtfs_service_date": args.gtfs_service_date,
        "write_schedule": args.write_schedule,
    }


def main(argv: list[str] | None = None) -> None:
    """Run download_network (or, with ``build``, build_network) from the terminal."""
    import sys

    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments[:1] == ["build"]:
        _build(arguments[1:])
        return

    parser = argparse.ArgumentParser(
        prog="multimodalpy",
        description=(
            "Download the transport networks of a Spanish municipality, or of "
            "your own study area, and write them to disk. To build them from "
            "files you already have instead: multimodalpy build --help."
        ),
    )
    _add_area_options(parser)
    parser.add_argument(
        "--modes", nargs="+", default=["walking"], help="Transport modes."
    )
    _add_output_options(parser, what="download")
    parser.add_argument(
        "--api-key",
        help=(
            "NAP API key for bus/train. Defaults to NAP_API_KEY or a .env file; "
            "prefer those, since a key typed here stays in the shell history."
        ),
    )
    parser.add_argument(
        "--no-connect-parts",
        dest="connect_parts",
        action="store_false",
        help="Don't add the roads joining the parts of a multi-part municipality.",
    )
    parser.add_argument(
        "--overpass-url",
        help=(
            "Overpass API server for the OpenStreetMap networks "
            "(default: osmnx's, https://overpass-api.de/api)."
        ),
    )
    parser.add_argument(
        "--osm-timeout",
        type=float,
        help="Seconds to wait for each Overpass answer (default: 180).",
    )
    _add_gtfs_options(parser)
    parser.add_argument(
        "--gtfs-feed",
        action="append",
        metavar="[NAME=]SOURCE",
        help=(
            "A GTFS feed from outside the NAP: an http(s) address, optionally "
            "named (my_operator=https://.../gtfs.zip). Repeat it for several "
            "feeds. Local files: multimodalpy build --gtfs-feed."
        ),
    )
    parser.add_argument(
        "--keep-gtfs-zips",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Keep the GTFS zip files downloaded in gtfs_zips/ (the default), so "
            "'multimodalpy build --source-dir' can process them again; "
            "--no-keep-gtfs-zips deletes them."
        ),
    )
    args = parser.parse_args(arguments)
    if not (args.area or args.area_code or args.boundaries):
        parser.error("give the study area: --area, --area-code or --boundaries")

    gtfs_feeds = [_feed_argument(value) for value in args.gtfs_feed or []]
    manifest = network.download_network(
        area_name=args.area,
        modes=args.modes,
        output_dir=args.output_dir,
        boundaries_path=args.boundaries,
        crs=args.crs,
        formats=args.formats,
        area_code=args.area_code,
        api_key=args.api_key,
        province=args.province,
        buffer_m=args.buffer_m,
        connect_parts=args.connect_parts,
        overpass_url=args.overpass_url,
        osm_timeout=args.osm_timeout,
        gtfs_feeds=gtfs_feeds or None,
        keep_gtfs_zips=args.keep_gtfs_zips,
        **_gtfs_arguments(parser, args),
    )
    print(summary(manifest))


def _build(arguments: list[str]) -> None:
    """``multimodalpy build``: :func:`multimodalpy.build_network`."""
    parser = argparse.ArgumentParser(
        prog="multimodalpy build",
        description=(
            "Build the networks of a study area from files you already have "
            "(a folder written by multimodalpy, street networks of your own, "
            "local GTFS feeds), without downloading anything."
        ),
    )
    parser.add_argument(
        "--source-dir",
        help="A folder written by multimodalpy (its networks and kept GTFS zips).",
    )
    parser.add_argument(
        "--osm-network",
        action="append",
        metavar="MODE=PATH",
        help=(
            "A street network of your own: walking, bike or driving, and a "
            "multimodalpy folder or GeoPackage, an edges file or an osmnx "
            "GraphML, e.g. walking=streets.gpkg. Repeat it for several."
        ),
    )
    parser.add_argument(
        "--gtfs-feed",
        action="append",
        metavar="[NAME=]PATH",
        help="A local GTFS zip or folder, optionally named. Repeat it for several.",
    )
    _add_area_options(parser)
    _add_output_options(parser, what="clipping")
    _add_gtfs_options(parser)
    args = parser.parse_args(arguments)
    if not (args.area or args.area_code or args.boundaries):
        parser.error("give the study area: --area, --area-code or --boundaries")
    osm_networks: dict[str, network.OSMSource] = {}
    for value in args.osm_network or []:
        match = re.fullmatch(r"([\w-]+)=(.+)", value)
        if not match:
            parser.error(f"--osm-network: expected MODE=PATH, got {value!r}")
        osm_networks[match.group(1)] = match.group(2)
    gtfs_feeds = [_feed_argument(value) for value in args.gtfs_feed or []]
    if not (osm_networks or gtfs_feeds or args.source_dir):
        parser.error("give --source-dir, --osm-network or --gtfs-feed")
    manifest = network.build_network(
        osm_networks or None,
        gtfs_feeds or None,
        source_dir=args.source_dir,
        area_name=args.area,
        boundaries_path=args.boundaries,
        area_code=args.area_code,
        province=args.province,
        output_dir=args.output_dir,
        crs=args.crs,
        formats=args.formats,
        buffer_m=args.buffer_m,
        **_gtfs_arguments(parser, args),
    )
    print(summary(manifest))


def _feed_argument(value: str) -> str | tuple[str, str]:
    """``--gtfs-feed`` value: ``SOURCE`` or ``NAME=SOURCE``.

    A name is letters, digits, ``_`` or ``-``, so an address (with ``:``
    and ``/`` before any ``=``) is never taken for one.
    """
    match = re.fullmatch(r"([\w-]+)=(.+)", value)
    return (match.group(1), match.group(2)) if match else value


def _peak_periods_argument(
    values: list[str] | None,
) -> dict[str, tuple[int, int]] | None:
    """``--gtfs-peak-period`` values (``NAME=START-END``) as a dict, or ``None``."""
    if not values:
        return None
    periods: dict[str, tuple[int, int]] = {}
    for value in values:
        match = re.fullmatch(r"([\w-]+)=(\d{1,2})-(\d{1,2})", value.strip())
        if not match:
            raise ValueError(
                f"--gtfs-peak-period: expected NAME=START-END, e.g. morning=7-9; "
                f"got {value!r}"
            )
        name, start, end = match.groups()
        if name in periods:
            raise ValueError(f"--gtfs-peak-period: {name!r} is given twice")
        periods[name] = (int(start), int(end))
    return periods


def summary(manifest: dict) -> str:
    """Summarize in a few lines what download_network or build_network wrote."""
    from ._osm import _network_type

    out = manifest.get("output_dir", ".")
    details = manifest.get("manifest_file", "manifest.json")
    lines = [f"Written to {out}/ (details in {details})"]
    failed = manifest.get("failed_modes", {})
    streets = []
    for mode in manifest.get("osm_modes", []):
        label = _network_type(mode)[1]
        if label not in failed and label not in streets:
            streets.append(label)
    if streets:
        lines.append(f"  streets: {', '.join(streets)}")
    datasets = manifest.get("gtfs_datasets") or []
    if datasets:
        lines.append(f"  bus/train: {len(datasets)} feed(s): {', '.join(datasets)}")
    for mode in failed:
        lines.append(f"  not written: {mode} (see failed_modes)")
    if manifest.get("gtfs_status"):
        lines.append(f"  no bus/train: {manifest['gtfs_status']}")
    unused = [
        str(row.get("dataset") or row.get("dataset_name"))
        for row in manifest.get("gtfs_report", [])
        if row.get("status")
        in {"failed", "failed_processing", "no_service_on_date", "duplicate"}
    ]
    if unused:
        lines.append(f"  GTFS not used: {', '.join(unused)} (see gtfs_report.csv)")
    return "\n".join(lines)
