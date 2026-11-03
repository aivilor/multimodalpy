"""Command line: ``multimodalpy --area Valencia --modes walking bike``.

Also runs as ``python -m multimodalpy``. Every option maps to an argument of
:func:`multimodalpy.download_network`; ``multimodalpy --help`` lists them.

Private module.
"""

from __future__ import annotations

from . import network
from .network import FORMATS


def main(argv: list[str] | None = None) -> None:
    """Run :func:`multimodalpy.download_network` from the command line."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="multimodalpy",
        description=(
            "Download a Spanish municipality's transport networks and write them "
            "to disk."
        ),
    )
    parser.add_argument("--area", required=True, help="Municipality name.")
    parser.add_argument(
        "--modes", nargs="+", default=["walking"], help="Transport modes."
    )
    parser.add_argument(
        "--output-dir", default="output", help="Folder to write to (default: output)."
    )
    parser.add_argument("--boundaries", help="Boundaries shapefile/GeoJSON (optional).")
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
        gtfs_hour_band_size=args.gtfs_hour_band_size,
        gtfs_hour_range=tuple(args.gtfs_hour_range) if args.gtfs_hour_range else None,
        write_schedule=args.write_schedule,
        keep_gtfs_zips=args.keep_gtfs_zips,
    )
