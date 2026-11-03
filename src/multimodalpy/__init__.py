"""multimodalpy: transport networks for Spanish municipalities.

Download the walking, bike and driving networks (OpenStreetMap) and the bus
and train networks (official GTFS feeds) of a municipality, standardized and
written to disk, and analyse them::

    import multimodalpy as mmp

    mmp.download_network("Valencia", modes=["walking", "bike"], output_dir="output")
    areas = mmp.isochrones("output", origins=(-0.3763, 39.4699), cutoffs=[5, 10],
                           mode="walking")

Public modules:
    - network  : download_network() and the in-memory downloads.
    - area     : find_area(), the municipality's boundary.
    - io       : read_matrix() and read_graph(), to read the files written.
    - analysis : isochrones() and the other network analyses.

The most used functions are also available from the package itself.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from . import analysis, area, io, network
from .analysis import isochrones
from .area import find_area
from .io import read_graph, read_matrix
from .network import download_network

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
    "__version__",
    "analysis",
    "area",
    "download_network",
    "find_area",
    "io",
    "isochrones",
    "network",
    "read_graph",
    "read_matrix",
]
