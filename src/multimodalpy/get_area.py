"""Resolves the study area and downloads its OSM and GTFS network layers.

This module resolves a study area (municipality) from an administrative
boundaries file and downloads the network layers:

- OSM (via ``osmnx``) for walking / bike / driving modes.
- Official GTFS (via the NAP API, Spain's National Access Point) for bus /
  train.

Both are returned already standardized, with the columns described in
``DATA_SCHEMA.md``.

The functions return in-memory objects (GeoDataFrames / graphs). Writing to
disk in the format chosen by the user is done by
:mod:`multimodalpy.get_network`.

Credentials: no key is stored in the code. The NAP key comes from the
``api_key`` argument, the ``NAP_API_KEY`` environment variable, or a
``.env`` file in the working folder or one of its parents (see
:func:`resolve_nap_api_key`).
"""

from __future__ import annotations

from ._boundaries import (
    ALLOWED_BOUNDARY_SUFFIXES,
    DEFAULT_CODE_COLUMNS,
    DEFAULT_NAME_COLUMNS,
    FUZZY_MIN_SIMILARITY,
    PROVINCE_CODE_COLUMNS,
    PROVINCE_NAMES,
    AmbiguousAreaError,
    find_area_boundary,
    province_code_from_natcode,
    resolve_province_code,
    study_area_geometry,
)
from ._gtfs import download_gtfs_layers
from ._nap import (
    NAP_BASE_URL,
    NAP_PROVINCE_IDS,
    NAPAuthenticationError,
    download_gtfs_nap_zips,
    find_dotenv,
    load_dotenv,
    missing_nap_key_message,
    resolve_nap_api_key,
)
from ._osm import (
    CONNECT_MARGIN_M,
    NETWORK_TYPE_TO_LAYER_LABEL,
    OSM_NETWORK_TYPES,
    download_osm_layers,
)
from ._text import (
    levenshtein_distance,
    levenshtein_similarity,
    normalize_name,
    slugify,
)

__all__ = [
    "ALLOWED_BOUNDARY_SUFFIXES",
    "CONNECT_MARGIN_M",
    "DEFAULT_CODE_COLUMNS",
    "DEFAULT_NAME_COLUMNS",
    "FUZZY_MIN_SIMILARITY",
    "NAP_BASE_URL",
    "NAP_PROVINCE_IDS",
    "NETWORK_TYPE_TO_LAYER_LABEL",
    "OSM_NETWORK_TYPES",
    "PROVINCE_CODE_COLUMNS",
    "PROVINCE_NAMES",
    "AmbiguousAreaError",
    "NAPAuthenticationError",
    "download_gtfs_layers",
    "download_gtfs_nap_zips",
    "download_osm_layers",
    "find_area_boundary",
    "find_dotenv",
    "levenshtein_distance",
    "levenshtein_similarity",
    "load_dotenv",
    "missing_nap_key_message",
    "normalize_name",
    "province_code_from_natcode",
    "resolve_nap_api_key",
    "resolve_province_code",
    "slugify",
    "study_area_geometry",
]
