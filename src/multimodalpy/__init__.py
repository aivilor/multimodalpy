"""multimodalpy - library to download and standardize multimodal networks.

Basic usage:

    from multimodalpy import main

    main("Valencia", modes=["walking", "bike"], output_path="output")

Public modules:
    - get_network    : main() entry point; downloads, standardizes and writes
                       the networks per the user's settings.
    - get_area       : study-area boundary lookup and OSM / GTFS downloads.

OSM and GTFS standardization happen in private modules (``_process_osm``,
``_process_gtfs``) that are not part of the public API.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from . import get_area, get_network
from .get_area import find_area_boundary
from .get_network import main, read_graph_json

# A library shouldn't write to the terminal on its own: without this
# handler, Python would print WARNING-level alerts to stderr even if the
# application hasn't configured logging. Enable them with
# logging.basicConfig(level=...).
logging.getLogger(__name__).addHandler(logging.NullHandler())

try:
    __version__ = version("multimodalpy")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "main",
    "read_graph_json",
    "find_area_boundary",
    "get_network",
    "get_area",
    "__version__",
]
