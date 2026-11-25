"""Command line: ``multimodalpy --area Valencia --modes walking bike``.

The study area is a municipality (``--area``) or your own polygon
(``--boundaries metro_area.geojson``).

Also runs as ``python -m multimodalpy``. Every option maps to an argument of
:func:`multimodalpy.download_network`; ``multimodalpy --help`` lists them.

Private module.
"""

from __future__ import annotations

import re

from . import network
from .network import FORMATS


def main(argv: list[str] | None = None) -> None:
    """Run :func:`multimodalpy.download_network` from the command line."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="multimodalpy",
        description=(
            "Download the transport networks of a Spanish municipality, or of "
            "your own study area, and write them to disk."
        ),
    )
    parser.add_argument(
        "--area",
        help=(
            "Municipality name. With --boundaries, only the output label "
            "(defaults to the file name)."
        ),
    )
    parser.add_argument(
        "--modes", nargs="+", default=["walking"], help="Transport modes."
    )
    parser.add_argument(
        "--output-dir", default="output", help="Folder to write to (default: output)."
    )
    parser.add_argument(
        "--boundaries",
        help=(
            "Shapefile/GeoJSON/GeoPackage with your own study area (any CRS); "
            "replaces "
            "the search by name."
        ),
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
        "--api-key",
        help=(
            "NAP API key for bus/train. Defaults to NAP_API_KEY or a .env file; "
            "prefer those, since a key typed here stays in the shell history."
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
    parser.add_argument(
        "--buffer-m",
        type=float,
        default=0.0,
        help="Widen the download area by this many metres (default 0).",
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
        help=(
            "Keep only the GTFS trips leaving in this hour window, e.g. "
            "--gtfs-hour-range 7 9; it may cross midnight (22 2)."
        ),
    )
    parser.add_argument(
        "--gtfs-feed",
        action="append",
        metavar="[NAME=]SOURCE",
        help=(
            "A GTFS feed from outside the NAP: an http(s) address or a local zip "
            "or folder, optionally named (my_operator=https://.../gtfs.zip). "
            "Repeat it for several feeds."
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
    parser.add_argument(
        "--keep-gtfs-zips",
        action="store_true",
        help=(
            "Keep the GTFS zip files downloaded from the NAP instead of deleting them."
        ),
    )
    args = parser.parse_args(argv)
    if not (args.area or args.area_code or args.boundaries):
        parser.error("give the study area: --area, --area-code or --boundaries")

    gtfs_feeds = [_feed_argument(value) for value in args.gtfs_feed or []]
    network.download_network(
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
        gtfs_hour_band_size=args.gtfs_hour_band_size,
        gtfs_hour_range=tuple(args.gtfs_hour_range) if args.gtfs_hour_range else None,
        gtfs_service_date=args.gtfs_service_date,
        gtfs_feeds=gtfs_feeds or None,
        write_schedule=args.write_schedule,
        keep_gtfs_zips=args.keep_gtfs_zips,
    )


def _feed_argument(value: str) -> str | tuple[str, str]:
    """``--gtfs-feed`` value: ``SOURCE`` or ``NAME=SOURCE``.

    A name is letters, digits, ``_`` or ``-``, so an address (with ``:``
    and ``/`` before any ``=``) is never taken for one.
    """
    match = re.fullmatch(r"([\w-]+)=(.+)", value)
    return (match.group(1), match.group(2)) if match else value
