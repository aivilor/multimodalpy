"""Downloads data from the OSM and GTFS APIs, and calls process_osm / process_gtfs.

This module resolves a study area (municipality) from an administrative
boundaries file and downloads the network layers:

- OSM (via ``osmnx``) for walking / bike / driving modes, normalizing the
  result with :mod:`multimodalpy.process_osm`.
- Official GTFS (via the NAP API, Spain's National Access Point) for bus /
  train, normalizing the result with :mod:`multimodalpy.process_gtfs`.

The functions return in-memory objects (GeoDataFrames / graphs). Writing to
disk in the format chosen by the user is done by
:mod:`multimodalpy.get_network`.

Credentials: no key is stored in the code. The NAP key comes from the
``api_key`` argument, the ``NAP_API_KEY`` environment variable, or a
``.env`` file in the working folder or one of its parents (see
:func:`resolve_nap_api_key`).
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import unicodedata
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from . import process_gtfs, process_osm

if TYPE_CHECKING:
    import geopandas as gpd
    import pandas as pd
    import requests


logger = logging.getLogger(__name__)

NAP_BASE_URL = "https://nap.transportes.gob.es/api/v2"

# Municipal-boundary shapefile. It isn't distributed inside the package or
# the repository (~50 MB); it's downloaded the first time it's needed from
# a GitHub release and cached locally with ``pooch`` (in the OS's standard
# cache directory), so subsequent calls reuse the local copy instead of
# downloading again.
_BOUNDARIES_RELEASE_URL = (
    "https://github.com/aivilor/multimodalpy/releases/download/data-v1/"
    "recintos_municipales_inspire_peninbal_etrs89.zip"
)
# SHA-256 of the release zip, so pooch verifies the file on download.
_BOUNDARIES_RELEASE_HASH: str | None = (
    "sha256:05a872f215bc72bcd84c37810010b2fc391c9066a67fb9dc60abd43c358f56aa"
)


def _default_boundaries_path() -> Path:
    """Download (or retrieves from cache) the municipal-boundary shapefile."""
    import pooch

    extracted = pooch.retrieve(
        url=_BOUNDARIES_RELEASE_URL,
        known_hash=_BOUNDARIES_RELEASE_HASH,
        fname="recintos_municipales_inspire_peninbal_etrs89.zip",
        path=pooch.os_cache("multimodalpy"),
        processor=pooch.Unzip(),
    )
    shp_file = _pick_shapefile(extracted)
    if shp_file is None:
        raise FileNotFoundError(
            "No .shp file found in the resource downloaded from "
            f"{_BOUNDARIES_RELEASE_URL}"
        )
    return shp_file


# Only shapefile or geojson are accepted as the boundaries file (per the
# meeting notes: no geopackage for ``boundaries_path``).
ALLOWED_BOUNDARY_SUFFIXES = {".shp", ".geojson", ".json"}

DEFAULT_NAME_COLUMNS = (
    "nombre",
    "NOMBRE",
    "name",
    "NAME",
    "municipio",
    "MUNICIPIO",
    "mun_name",
    "MUN_NAME",
    "nombre_mun",
    "NOMBRE_MUN",
    "nom_mun",
    "NOM_MUN",
    "provincia",
    "PROVINCIA",
    "comunidad",
    "COMUNIDAD",
    "ccaa",
    "CCAA",
    "texto",
    "TEXTO",
    "rotulo",
    "ROTULO",
    "NAMEUNIT",
)

DEFAULT_CODE_COLUMNS = (
    "NATCODE",
    "natcode",
    "INSPIREID",
    "inspireid",
    "codigo",
    "CODIGO",
    "cod_mun",
    "COD_MUN",
    "ine",
    "INE",
)

# The NAP's ``/conjunto-dato/region/{id}`` endpoint only understands province
# ids: INE codes 1-52. The ``/region`` listing, on the other hand, returns
# ~8300 entries (municipalities, regions and provinces) that share that same
# id space, so searching there by name is a trap:
#
# - Municipality entries (``tipo=3``) carry the 5-digit INE code, always
#   outside the 1-52 range, and the endpoint responds 404.
# - Region/CCAA entries (``tipo=1``) carry the region code (1-19), which
#   does fall inside the province range, so the endpoint silently returns
#   the data for a DIFFERENT province (e.g. the "Madrid" entry has id=13
#   and ends up serving Ciudad Real, which is province 13).
#
# For this reason the province is inferred from the municipality's official
# code, not from comparing names.
NAP_PROVINCE_IDS = frozenset(range(1, 53))

# Columns the province code can be extracted from, in order of preference.
# NATCODE (INSPIRE) has the form
# ``<2 country><2 region><2 province><5 municipality>``.
PROVINCE_CODE_COLUMNS = ("NATCODE", "natcode", "cod_mun", "COD_MUN", "ine", "INE")

# INE province code -> names (first one is the display name). Includes
# the Spanish and co-official forms so either can be passed as
# ``province=...``.
PROVINCE_NAMES: dict[int, tuple[str, ...]] = {
    1: ("Araba/Álava", "Álava", "Araba"),
    2: ("Albacete",),
    3: ("Alicante/Alacant", "Alicante", "Alacant"),
    4: ("Almería",),
    5: ("Ávila",),
    6: ("Badajoz",),
    7: ("Illes Balears", "Islas Baleares", "Baleares", "Balears"),
    8: ("Barcelona",),
    9: ("Burgos",),
    10: ("Cáceres",),
    11: ("Cádiz",),
    12: ("Castellón/Castelló", "Castellón", "Castelló"),
    13: ("Ciudad Real",),
    14: ("Córdoba",),
    15: ("A Coruña", "La Coruña", "Coruña"),
    16: ("Cuenca",),
    17: ("Girona", "Gerona"),
    18: ("Granada",),
    19: ("Guadalajara",),
    20: ("Gipuzkoa", "Guipúzcoa"),
    21: ("Huelva",),
    22: ("Huesca",),
    23: ("Jaén",),
    24: ("León",),
    25: ("Lleida", "Lérida"),
    26: ("La Rioja", "Rioja"),
    27: ("Lugo",),
    28: ("Madrid",),
    29: ("Málaga",),
    30: ("Murcia",),
    31: ("Navarra", "Nafarroa"),
    32: ("Ourense", "Orense"),
    33: ("Asturias",),
    34: ("Palencia",),
    35: ("Las Palmas",),
    36: ("Pontevedra",),
    37: ("Salamanca",),
    38: ("Santa Cruz de Tenerife",),
    39: ("Cantabria",),
    40: ("Segovia",),
    41: ("Sevilla",),
    42: ("Soria",),
    43: ("Tarragona",),
    44: ("Teruel",),
    45: ("Toledo",),
    46: ("Valencia/València", "Valencia", "València"),
    47: ("Valladolid",),
    48: ("Bizkaia", "Vizcaya"),
    49: ("Zamora",),
    50: ("Zaragoza",),
    51: ("Ceuta",),
    52: ("Melilla",),
}

# Supported OSM modes (walking / bike / driving). Several input synonyms
# are accepted (Spanish/English), but the resulting layer and file name
# always use the canonical English label: "walking", "bike", "driving"
# (see ``NETWORK_TYPE_TO_LAYER_LABEL``).
OSM_NETWORK_TYPES = {
    "caminable": "walk",
    "walk": "walk",
    "peatonal": "walk",
    "walking": "walk",
    "bicicleta": "bike",
    "bike": "bike",
    "bicycle": "bike",
    "coche": "drive",
    "car": "drive",
    "drive": "drive",
    "carretera": "drive",
    "driving": "drive",
}

# Canonical layer/file label for each osmnx ``network_type``.
NETWORK_TYPE_TO_LAYER_LABEL = {
    "walk": "walking",
    "bike": "bike",
    "drive": "driving",
}


# ---------------------------------------------------------------------------
# Text / environment utilities
# ---------------------------------------------------------------------------
def find_dotenv(start: str | Path | None = None) -> Path | None:
    """First ``.env`` file in ``start`` (default: the working folder) or a parent.

    Looking in the parent folders too means a ``.env`` at the root of a
    project is found when Python runs from a subfolder (e.g. a notebook
    in ``notebooks/``), not only from the folder that holds it.
    """
    folder = Path(start if start is not None else Path.cwd()).resolve()
    for candidate in (folder, *folder.parents):
        path = candidate / ".env"
        if path.is_file():
            return path
    return None


def load_dotenv(env_path: str | Path | None = None) -> Path | None:
    """Load variables from a ``.env`` file into ``os.environ``.

    ``env_path`` defaults to the first ``.env`` found by
    :func:`find_dotenv`. Variables already set in the environment are not
    overwritten. Returns the file that was read, or ``None`` if there was
    none.
    """
    path = Path(env_path) if env_path is not None else find_dotenv()
    if path is None or not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return path


def resolve_nap_api_key(api_key: str | None = None) -> str | None:
    """Return the NAP API key to use, or ``None`` if there is none.

    In order of priority: the ``api_key`` argument (``--api-key`` in the
    command line), the ``NAP_API_KEY`` environment variable, and a
    ``NAP_API_KEY=...`` line in a ``.env`` file (see :func:`find_dotenv`).
    """
    if api_key:
        return api_key
    if not os.environ.get("NAP_API_KEY"):
        load_dotenv()
    return os.environ.get("NAP_API_KEY") or None


def missing_nap_key_message() -> str:
    """Explain how to provide the NAP key, naming the folders searched."""
    return (
        "Missing NAP API key (only needed for bus/train). Pass api_key=... "
        "(--api-key in the command line), set the NAP_API_KEY environment "
        "variable, or put NAP_API_KEY=... in a .env file in "
        f"{Path.cwd()} or one of its parent folders."
    )


def normalize_name(value: object) -> str:
    """Normalize text for accent-/case-insensitive comparisons."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def levenshtein_distance(left: str, right: str) -> int:
    """Levenshtein edit distance between two normalized strings."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (left_char != right_char)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def levenshtein_similarity(left: str, right: str) -> float:
    """0-1 similarity score based on the Levenshtein distance."""
    max_length = max(len(left), len(right))
    if max_length == 0:
        return 1.0
    return 1 - (levenshtein_distance(left, right) / max_length)


def slugify(value: str) -> str:
    """Generate a filesystem-safe slug from value."""
    slug = normalize_name(value).replace(" ", "_")
    return slug or "area"


def province_code_from_natcode(value: object) -> int | None:
    """Extract the INE province code (1-52) from an INSPIRE NATCODE.

    ``NATCODE`` has the form ``<2 country><2 region><2 province><5
    municipality>`` (e.g. ``34074040136`` -> province 40, Segovia). The two
    province digits are repeated at the start of the municipality code, so
    both positions are tried and the first one that falls in the valid
    range is accepted. Returns ``None`` if the code doesn't allow the
    province to be inferred.
    """
    digits = re.sub(r"\D", "", "" if value is None else str(value))
    if len(digits) >= 8:
        candidates: tuple[str, ...] = (digits[4:6], digits[6:8])
    elif len(digits) == 5:
        # Standalone INE municipality code (e.g. "40136").
        candidates = (digits[0:2],)
    else:
        return None
    for candidate in candidates:
        try:
            code = int(candidate)
        except ValueError:
            continue
        if code in NAP_PROVINCE_IDS:
            return code
    return None


def _find_province_code(gdf: gpd.GeoDataFrame) -> int | None:
    """Look up the province code in the boundary's code columns."""
    for column in _existing_columns(gdf, PROVINCE_CODE_COLUMNS):
        for raw_value in gdf[column].dropna().tolist():
            code = province_code_from_natcode(raw_value)
            if code is not None:
                return code
    return None


# ---------------------------------------------------------------------------
# Study-area resolution (municipality -> polygon)
# ---------------------------------------------------------------------------
class AmbiguousAreaError(ValueError):
    """Raised when a name matches several different municipalities.

    Merging them silently produced study areas spanning several provinces
    (e.g. "San Sebastián" took Donostia, San Sebastián de los Reyes and San
    Sebastián de los Ballesteros) and a GTFS download for the wrong
    province. ``candidates`` lists them as ``"Name (Province, code)"``.
    """

    def __init__(self, area_name: str, candidates: list[str]) -> None:
        self.area_name = area_name
        self.candidates = candidates
        shown = "; ".join(candidates[:15])
        if len(candidates) > 15:
            shown += f"; ... ({len(candidates)} in total)"
        super().__init__(
            f"'{area_name}' matches {len(candidates)} different areas: {shown}. "
            "Pass area_code=... (5-digit INE code or NATCODE) or province=... "
            "to choose one."
        )


# Similarity needed for a fuzzy (typo-tolerant) name match. 0.7 accepted
# unrelated towns: "Las Palmas" -> Las Valeras (0.73), "Santa Cruz de
# Tenerife" -> Santa Cruz de Grío (0.73). Adjacent transpositions count as
# a single edit (see ``_typo_distance``), so real typos such as
# "Valencai" (0.875) or "Sevila" (0.86) are still accepted.
FUZZY_MIN_SIMILARITY = 0.8


def _existing_columns(gdf: gpd.GeoDataFrame, requested: Iterable[str]) -> list[str]:
    return [column for column in requested if column in gdf.columns]


def _typo_distance(left: str, right: str) -> int:
    """Edit distance counting adjacent transpositions as one edit (OSA)."""
    if left == right:
        return 0
    rows = len(left) + 1
    cols = len(right) + 1
    dist = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        dist[i][0] = i
    for j in range(cols):
        dist[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            cost = left[i - 1] != right[j - 1]
            dist[i][j] = min(
                dist[i - 1][j] + 1,
                dist[i][j - 1] + 1,
                dist[i - 1][j - 1] + cost,
            )
            if (
                i > 1
                and j > 1
                and left[i - 1] == right[j - 2]
                and left[i - 2] == right[j - 1]
            ):
                dist[i][j] = min(dist[i][j], dist[i - 2][j - 2] + 1)
    return dist[-1][-1]


def _typo_similarity(left: str, right: str) -> tuple[float, int]:
    """0-1 similarity (and distance) based on :func:`_typo_distance`."""
    max_length = max(len(left), len(right))
    if max_length == 0:
        return 1.0, 0
    distance = _typo_distance(left, right)
    return 1 - distance / max_length, distance


def _name_parts(value: object) -> list[str]:
    """Return the normalized whole name and each of its bilingual parts.

    Official names are often bilingual, separated by '/'
    ("Elx/Elche", "Donostia/San Sebastián"); each part is a valid name.
    """
    text = "" if value is None else str(value)
    parts = [normalize_name(text)]
    if "/" in text:
        parts.extend(normalize_name(part) for part in text.split("/"))
    return [part for part in dict.fromkeys(parts) if part]


def _row_province_codes(gdf: gpd.GeoDataFrame) -> pd.Series | None:
    """INE province code of every row, or ``None`` with no code column."""
    columns = _existing_columns(gdf, PROVINCE_CODE_COLUMNS)
    if not columns:
        return None
    return gdf[columns[0]].map(province_code_from_natcode)


def resolve_province_code(province: int | str) -> int:
    """Turn a province name or INE code (1-52) into the INE code.

    Accepts Spanish and co-official names, with or without accents
    ("Valencia", "València", "Gerona", "Girona", "Vizcaya", "Bizkaia").
    """
    text = str(province).strip()
    if text.isdigit():
        code = int(text)
        if code not in NAP_PROVINCE_IDS:
            raise ValueError(
                f"province '{province}' is not valid: INE province codes go "
                "from 1 to 52."
            )
        return code
    target = normalize_name(text)
    for code, names in PROVINCE_NAMES.items():
        if any(target == normalize_name(name) for name in names):
            return code
    raise ValueError(
        f"Unknown province '{province}'. Use the INE code (1-52) or a name "
        "such as 'Valencia', 'Navarra' or 'A Coruña'."
    )


def _area_label(
    gdf: gpd.GeoDataFrame,
    index: object,
    column: str,
    id_column: str | None,
    provinces: pd.Series | None,
) -> str:
    """Readable ``"Name (Province, code)"`` label for one boundary row."""
    details = []
    if provinces is not None:
        code = provinces.loc[index]
        if code is not None and code == code:
            details.append(PROVINCE_NAMES.get(int(code), (str(code),))[0])
    if id_column is not None:
        details.append(str(gdf.loc[index, id_column]))
    name = str(gdf.loc[index, column])
    return f"{name} ({', '.join(details)})" if details else name


def _pick_single_area(
    gdf: gpd.GeoDataFrame,
    mask: pd.Series,
    column: str,
    area_name: str,
    id_column: str | None,
    provinces: pd.Series | None,
) -> tuple[gpd.GeoDataFrame, str, str]:
    """Return the matching rows if they are one area, or raise if several.

    Rows are the same area when they share the official code (a
    municipality split into several polygons). Without a code column, rows
    are grouped by the matched name, as before.
    """
    rows = gdf.loc[mask]
    key = rows[id_column].astype(str) if id_column else rows[column].astype(str)
    if key.nunique() > 1:
        first_rows = rows.loc[~key.duplicated()]
        labels = [
            _area_label(gdf, index, column, id_column, provinces)
            for index in first_rows.index
        ]
        raise AmbiguousAreaError(area_name, labels)
    return rows.copy(), column, str(rows[column].iloc[0])


def _select_area_rows(
    gdf: gpd.GeoDataFrame,
    area_name: str,
    area_code: str | None = None,
    name_columns: Iterable[str] | None = None,
    code_columns: Iterable[str] | None = None,
    province: int | str | None = None,
) -> tuple[gpd.GeoDataFrame, str, str]:
    """Find the boundary rows of exactly one area.

    With ``area_code``, the code must equal a code column's value or, for
    a 5-digit INE municipality code, be the last five digits of an
    INSPIRE NATCODE. Otherwise the name is matched in increasingly loose
    steps, and the first step with a hit decides:

    1. the whole name ("Gijón");
    2. one part of a bilingual name ("Elche" -> "Elx/Elche");
    3. whole words inside a name ("Castellón" -> "Castelló de la
       Plana/Castellón de la Plana", but not "Castellonet");
    4. any substring;
    5. a small typo (similarity >= ``FUZZY_MIN_SIMILARITY``).

    Within each step, name columns are tried in order. If the hit covers
    several different municipalities, :class:`AmbiguousAreaError` is
    raised instead of merging them; ``province`` (name or INE code)
    narrows the search to one province first.
    """
    code_cols = _existing_columns(gdf, code_columns or DEFAULT_CODE_COLUMNS)
    id_column = code_cols[0] if code_cols else None
    provinces = _row_province_codes(gdf)

    if province is not None:
        province_code = resolve_province_code(province)
        if provinces is None:
            raise ValueError(
                "province=... needs a code column to read the province from "
                f"({', '.join(PROVINCE_CODE_COLUMNS)}); none is in the file."
            )
        in_province = provinces == province_code
        if not in_province.any():
            raise ValueError(
                f"No boundary in the file belongs to province {province_code} "
                f"({PROVINCE_NAMES[province_code][0]})."
            )
        gdf = gdf.loc[in_province]
        provinces = provinces.loc[in_province]

    if area_code:
        code = str(area_code).strip()
        for column in code_cols:
            values = gdf[column].astype(str).str.strip()
            exact_mask = values == code
            if exact_mask.any():
                return _pick_single_area(
                    gdf, exact_mask, column, str(area_code), id_column, provinces
                )
        if code.isdigit() and len(code) == 5:
            # INE municipality code: the last five digits of a NATCODE
            # (``<2 country><2 region><2 province><5 municipality>``).
            for column in code_cols:
                values = gdf[column].astype(str).str.strip()
                ine_mask = (values.str.len() >= 8) & values.str.endswith(code)
                if ine_mask.any():
                    return _pick_single_area(
                        gdf, ine_mask, column, str(area_code), id_column, provinces
                    )
        raise ValueError(f"No boundary found for code '{area_code}'.")

    target = normalize_name(area_name)
    columns = _existing_columns(gdf, name_columns or DEFAULT_NAME_COLUMNS)
    if not columns:
        raise ValueError(
            "No name column found in the boundaries file. "
            f"Available columns: {list(gdf.columns)}"
        )
    if not target:
        raise ValueError("area_name is empty.")

    parts_by_column = {column: gdf[column].map(_name_parts) for column in columns}
    word_pattern = re.compile(rf"(?:^| ){re.escape(target)}(?: |$)")
    steps = (
        ("exact", lambda parts: bool(parts) and parts[0] == target),
        ("bilingual part", lambda parts: target in parts),
        ("whole words", lambda parts: any(word_pattern.search(p) for p in parts)),
        ("substring", lambda parts: any(target in p for p in parts)),
    )
    for step, matches in steps:
        for column in columns:
            mask = parts_by_column[column].map(matches).astype(bool)
            if mask.any():
                rows, matched_column, matched_value = _pick_single_area(
                    gdf, mask, column, area_name, id_column, provinces
                )
                # A bilingual part is an official name, as exact as the
                # whole one; only looser steps are worth a warning.
                if step not in {"exact", "bilingual part"}:
                    _warn_inexact_match(area_name, matched_value, step)
                return rows, matched_column, matched_value

    # 5) Fuzzy match: best similarity over every column and name part.
    best_score = 0.0
    best_masks: list[tuple[str, pd.Series]] = []
    for column in columns:
        for raw_value in gdf[column].dropna().unique().tolist():
            score = 0.0
            for part in _name_parts(raw_value):
                max_distance = max(2, round(max(len(target), len(part)) * 0.3))
                if abs(len(target) - len(part)) > max_distance:
                    continue  # cannot be within reach; skip the full DP
                part_score, distance = _typo_similarity(target, part)
                if distance <= max_distance:
                    score = max(score, part_score)
            if score < FUZZY_MIN_SIMILARITY or score < best_score - 1e-9:
                continue
            mask = gdf[column] == raw_value
            if score > best_score + 1e-9:
                best_score, best_masks = score, []
            best_masks.append((column, mask))

    if not best_masks:
        raise ValueError(f"No boundary found for area '{area_name}'.")

    # Every name tied for the best score counts: if they are different
    # municipalities (e.g. "Gerona" is one edit from both Girona and
    # Gerena), that is ambiguous rather than a coin toss.
    column = best_masks[0][0]
    combined = best_masks[0][1].copy()
    for _, mask in best_masks[1:]:
        combined |= mask
    rows, matched_column, matched_value = _pick_single_area(
        gdf, combined, column, area_name, id_column, provinces
    )
    _warn_inexact_match(area_name, matched_value, f"typo, {best_score:.0%} similar")
    return rows, matched_column, matched_value


def _warn_inexact_match(area_name: str, matched_value: str, how: str) -> None:
    """Say which area was picked when the name was not an exact match.

    A ``UserWarning`` rather than a log line, because the package logger is
    silent unless the application configures logging, and a substitution
    such as "Gerona" -> "Gerena" must not go unnoticed.
    """
    warnings.warn(
        f"'{area_name}' matched '{matched_value}' ({how}). If this is not the "
        "intended area, pass area_code=... or province=....",
        UserWarning,
        stacklevel=4,
    )


def _pick_shapefile(paths: Iterable[str | Path]) -> Path | None:
    """First real ``.shp`` among extracted paths, skipping macOS metadata.

    Zips made on macOS carry ``__MACOSX/._name.shp`` stubs (a few bytes of
    resource-fork data) that also end in ``.shp`` but cannot be read.
    """
    for raw in paths:
        path = Path(raw)
        if (
            path.suffix.lower() == ".shp"
            and "__MACOSX" not in path.parts
            and not path.name.startswith("._")
        ):
            return path
    return None


def study_area_geometry(
    boundary: gpd.GeoDataFrame, *, buffer_m: float = 0.0, crs: str = "EPSG:4326"
):
    """Study-area polygon used to download/clip data, optionally buffered.

    ``buffer_m`` widens the area by that many metres (buffered in the local
    UTM zone), so streets and transit routes that cross the municipal
    boundary are not cut at it. The exported ``study_area_boundary`` layer
    is never buffered.
    """
    if buffer_m is None or buffer_m == 0:
        return boundary.to_crs(crs).geometry.iloc[0]
    if buffer_m < 0:
        raise ValueError("buffer_m must be zero or positive (metres).")
    metric = boundary.to_crs(boundary.estimate_utm_crs())
    return metric.geometry.buffer(buffer_m).to_crs(crs).iloc[0]


def find_area_boundary(
    area_name: str,
    boundaries_path: str | Path | None = None,
    *,
    area_code: str | None = None,
    province: int | str | None = None,
    name_columns: Iterable[str] | None = None,
    code_columns: Iterable[str] | None = None,
    target_crs: str = "EPSG:4326",
) -> gpd.GeoDataFrame:
    """Return a single (dissolved) polygon for the given municipality.

    ``boundaries_path`` must be a shapefile (.shp) or a GeoJSON
    (.geojson/.json). If not given, the packaged municipal-boundary
    shapefile is used (peninsular Spain, Balearic Islands, Ceuta and
    Melilla; not the Canary Islands).

    ``area_code`` takes a 5-digit INE municipality code ("46250") or the
    full INSPIRE NATCODE ("34104646250"). ``province`` (name or INE code)
    picks between municipalities that share a name, e.g.
    ``find_area_boundary("Castejón", province="Navarra")``. Name matching
    is tolerant to accents, bilingual names and small typos; see
    :func:`_select_area_rows`. If a name matches several municipalities,
    :class:`AmbiguousAreaError` lists them.
    """
    using_default_file = boundaries_path is None
    if boundaries_path is None:
        boundaries_path = _default_boundaries_path()
    boundaries_path = Path(boundaries_path)

    if boundaries_path.suffix.lower() not in ALLOWED_BOUNDARY_SUFFIXES:
        raise ValueError(
            "boundaries_path only accepts shapefile (.shp) or geojson "
            f"(.geojson/.json). Received: {boundaries_path.suffix}"
        )
    if not boundaries_path.exists():
        raise FileNotFoundError(f"Boundaries file does not exist: {boundaries_path}")

    import geopandas as gpd

    gdf = gpd.read_file(boundaries_path)
    if gdf.empty:
        raise ValueError(f"Boundaries file is empty: {boundaries_path}")
    if gdf.crs is None:
        raise ValueError("Boundaries file has no CRS defined.")

    try:
        rows, matched_column, matched_value = _select_area_rows(
            gdf,
            area_name,
            area_code=area_code,
            name_columns=name_columns,
            code_columns=code_columns,
            province=province,
        )
    except AmbiguousAreaError:
        raise
    except ValueError as exc:
        if using_default_file and str(exc).startswith("No boundary found"):
            raise ValueError(
                f"{exc} The default boundaries file covers peninsular Spain, "
                "the Balearic Islands, Ceuta and Melilla, but not the Canary "
                "Islands; for other areas pass boundaries_path=...."
            ) from None
        raise

    if "NATLEVNAME" in rows.columns:
        levels = set(rows["NATLEVNAME"].dropna().astype(str))
        if levels and levels != {"Municipio"}:
            warnings.warn(
                f"'{matched_value}' is not a municipality "
                f"(NATLEVNAME={', '.join(sorted(levels))}); it belongs to no "
                "province, so no GTFS data can be downloaded for it.",
                UserWarning,
                stacklevel=2,
            )

    rows = rows.to_crs(target_crs)
    geometry = rows.geometry.union_all()

    return gpd.GeoDataFrame(
        [
            {
                "area_name": area_name,
                "matched_column": matched_column,
                "matched_value": matched_value,
                "matched_rows": len(rows),
                "area_code": area_code,
                # INE province code (1-52); this is the region id understood
                # by the NAP API. Can be None if the boundaries file doesn't
                # include any recognizable code column.
                "province_code": _find_province_code(rows),
                "source_path": str(boundaries_path),
            }
        ],
        geometry=[geometry],
        crs=target_crs,
    )


# ---------------------------------------------------------------------------
# Multi-part study areas (e.g. Donostia/San Sebastián with its exclaves)
# ---------------------------------------------------------------------------
# Extra margin around the convex hull of the parts when looking for the
# roads that connect them. A connecting route that leaves this area is not
# found, and the part is reported as unconnected.
CONNECT_MARGIN_M = 1000.0


def _area_parts(polygon) -> list:
    """Polygons of a (multi)polygon, largest first (part 1 is the main one)."""
    import geopandas as gpd

    if polygon.geom_type != "MultiPolygon":
        return [polygon]
    parts = gpd.GeoSeries(list(polygon.geoms), crs="EPSG:4326")
    areas = parts.to_crs(parts.estimate_utm_crs()).area
    return [parts.iloc[i] for i in areas.sort_values(ascending=False).index]


def _connection_area(polygon, margin_m: float = CONNECT_MARGIN_M):
    """Convex hull of all parts plus ``margin_m``, where connecting roads are sought."""
    import geopandas as gpd

    hull = gpd.GeoSeries([polygon.convex_hull], crs="EPSG:4326")
    metric = hull.to_crs(hull.estimate_utm_crs())
    return metric.buffer(margin_m).to_crs("EPSG:4326").iloc[0]


def _shortest_edge_key(graph, u, v) -> object:
    """Key of the shortest of the parallel edges between ``u`` and ``v``."""
    edges = graph.get_edge_data(u, v)
    return min(edges, key=lambda key: edges[key].get("length", 0.0))


def _connect_parts(graph, area_graph, parts: list) -> tuple[object, list[int]]:
    """Add to ``area_graph`` the shortest paths joining every part to the main one.

    ``graph`` is the network over the whole connection area and
    ``area_graph`` the same network cut to the study area. For each part
    other than the main one (part 1, the largest), the shortest path by
    ``length`` from the main part to it and the one back are looked up in
    ``graph``; both are needed because of one-way streets. Edges of those
    paths that are not already in ``area_graph`` are added with
    ``connector=True``; every other edge gets ``connector=False``.

    Returns the connected graph and the numbers (1-based, largest part
    first) of the parts that have streets but could not be reached in both
    directions. Parts with no streets at all (e.g. a small islet) are
    skipped.
    """
    import networkx as nx
    import numpy as np
    import shapely

    connected = area_graph.copy()
    nx.set_edge_attributes(connected, False, "connector")

    node_ids = np.array(list(area_graph.nodes))
    xs = np.array([area_graph.nodes[n]["x"] for n in node_ids], dtype=float)
    ys = np.array([area_graph.nodes[n]["y"] for n in node_ids], dtype=float)
    part_nodes = [
        set(node_ids[shapely.contains_xy(part, xs, ys)].tolist()) if len(xs) else set()
        for part in parts
    ]
    main = next((nodes for nodes in part_nodes if nodes), None)
    if main is None:
        return connected, []

    # One search from the whole main part in each direction covers every
    # other part.
    forward_m, forward = nx.multi_source_dijkstra(graph, main, weight="length")
    backward_m, backward = nx.multi_source_dijkstra(
        graph.reverse(copy=False), main, weight="length"
    )

    def closest_path(nodes, distances, paths):
        reached = [n for n in nodes if n in distances]
        if not reached:
            return None
        return paths[min(reached, key=lambda n: distances[n])]

    unconnected: list[int] = []
    for number, nodes in enumerate(part_nodes, start=1):
        if not nodes or nodes is main:
            continue
        to_part = closest_path(nodes, forward_m, forward)
        from_part = closest_path(nodes, backward_m, backward)
        if to_part is None or from_part is None:
            unconnected.append(number)
            continue
        steps = list(zip(to_part, to_part[1:], strict=False))
        # ``backward`` paths run main -> part on the reversed graph, so in
        # the real network they go part -> main.
        steps += [(v, u) for u, v in zip(from_part, from_part[1:], strict=False)]
        for u, v in steps:
            key = _shortest_edge_key(graph, u, v)
            if connected.has_edge(u, v, key):
                continue
            for node in (u, v):
                if node not in connected:
                    connected.add_node(node, **graph.nodes[node])
            connected.add_edge(
                u, v, key, **{**graph.edges[u, v, key], "connector": True}
            )
    return connected, unconnected


def _download_connected_graph(
    polygon,
    *,
    network_type: str,
    simplify: bool,
    retain_all: bool,
    truncate_by_edge: bool,
):
    """OSM graph of a multi-part area, with the roads that join its parts.

    The network is downloaded once over the convex hull of the parts (plus
    ``CONNECT_MARGIN_M``) and then cut to the study area exactly as
    ``osmnx.graph_from_polygon`` does (simplify over the larger area, then
    truncate), so the streets inside the area are the same as before. Only
    the shortest connecting paths are added back (see ``_connect_parts``).
    """
    import osmnx as ox

    graph = ox.graph_from_polygon(
        _connection_area(polygon),
        network_type=network_type,
        simplify=simplify,
        retain_all=True,
        truncate_by_edge=truncate_by_edge,
    )
    area_graph = ox.truncate.truncate_graph_polygon(
        graph, polygon, truncate_by_edge=truncate_by_edge
    )
    connected, unconnected = _connect_parts(graph, area_graph, _area_parts(polygon))
    if not retain_all:
        connected = ox.truncate.largest_component(connected, strongly=False)
    return connected, unconnected


# ---------------------------------------------------------------------------
# OSM download (osmnx) + standardization (process_osm)
# ---------------------------------------------------------------------------
def download_osm_layers(
    boundary: gpd.GeoDataFrame,
    *,
    modes: Iterable[str] = ("caminable",),
    output_crs: str = "EPSG:4326",
    simplify: bool = True,
    retain_all: bool = True,
    buffer_m: float = 0.0,
    truncate_by_edge: bool = True,
    connect_parts: bool = True,
) -> dict[str, dict[str, object]]:
    """Download OSM network layers for the given modes and standardizes them.

    Returns ``{mode: {"nodes": gdf, "edges": gdf, "graph": osmnx_graph,
    "unconnected_parts": list[int]}}``,
    where ``mode`` is always the canonical English label ("walking", "bike",
    "driving"), regardless of the synonym used in ``modes`` (e.g. "coche",
    "car", "drive" and "driving" all produce the "driving" key).

    ``nodes``/``edges`` are already the final layers, ready to export (see
    :func:`multimodalpy.process_osm.build_final_osm_layers`):

    - ``nodes``: ``node_id``, ``node_role``, ``geometry``.
    - ``edges``: ``edge_id``, ``from_node``, ``to_node``, ``highway``,
      ``hwy_raw``, ``lanes``, ``maxspeed``, ``spd_raw``, ``name``, ``oneway``,
      ``reversed``, ``length``,
      ``tts`` (travel time in seconds: free-flow speed + stop penalty based
      on the mode), ``geometry``.

    Travel time is computed automatically based on the network mode
    (``walking``: constant 5 km/h with no penalty; ``bike``: speed capped
    at 25 km/h with a reduced stop penalty; ``driving``: speed from
    ``maxspeed``/road type with a stop penalty based on the arrival node).
    ``travel_speed_kmh`` no longer needs to be specified.

    ``buffer_m`` widens the download area by that many metres (see
    :func:`study_area_geometry`). ``truncate_by_edge=True`` keeps streets
    that cross the area's edge (at least one end inside) instead of
    dropping them, so the network is not disconnected at the boundary.

    ``connect_parts=True`` (the default) joins the parts of a municipality
    made of several separate polygons (e.g. Donostia/San Sebastián and its
    exclaves): the shortest roads between them, which run through
    neighbouring municipalities, are added to the network and marked with
    ``connector=True`` in the edges layer. Parts are numbered from 1, the
    largest; any part with streets that cannot be reached is listed in
    ``unconnected_parts`` and reported with a ``UserWarning``. A
    single-polygon area is downloaded exactly as before.
    """
    try:
        import osmnx as ox
    except ImportError as exc:
        raise ImportError(
            "Install osmnx to download OSM networks: pip install osmnx"
        ) from exc

    polygon = study_area_geometry(boundary, buffer_m=buffer_m)
    results: dict[str, dict[str, object]] = {}

    for mode in modes:
        input_key = normalize_name(mode)
        network_type = OSM_NETWORK_TYPES.get(input_key)
        if network_type is None:
            valid = ", ".join(sorted(OSM_NETWORK_TYPES))
            raise ValueError(f"Unknown OSM mode '{mode}'. Valid values: {valid}")
        layer_key = NETWORK_TYPE_TO_LAYER_LABEL[network_type]

        unconnected: list[int] = []
        if connect_parts and polygon.geom_type == "MultiPolygon":
            graph, unconnected = _download_connected_graph(
                polygon,
                network_type=network_type,
                simplify=simplify,
                retain_all=retain_all,
                truncate_by_edge=truncate_by_edge,
            )
            if unconnected:
                warnings.warn(
                    f"No {layer_key} route was found to part(s) "
                    f"{', '.join(map(str, unconnected))} of the study area "
                    f"(numbered from 1, the largest); they stay disconnected.",
                    UserWarning,
                    stacklevel=2,
                )
        else:
            graph = ox.graph_from_polygon(
                polygon,
                network_type=network_type,
                simplify=simplify,
                retain_all=retain_all,
                truncate_by_edge=truncate_by_edge,
            )
        nodes, edges = process_osm.build_final_osm_layers(
            graph,
            layer_id=layer_key,
            mode=network_type,
            output_crs=output_crs,
        )
        results[layer_key] = {
            "nodes": nodes,
            "edges": edges,
            "graph": graph,
            "unconnected_parts": unconnected,
        }

    return results


# ---------------------------------------------------------------------------
# GTFS download (NAP API) + standardization (process_gtfs)
# ---------------------------------------------------------------------------
class NAPAuthenticationError(ValueError):
    """The NAP API rejected the key (HTTP 401): missing, invalid or expired."""


def _nap_get(url: str, headers: dict, timeout: int = 60) -> requests.Response:
    """GET request to the NAP API with clear handling of network/auth errors.

    Explicitly handles:
    - ``ConnectionError``: no internet connection or the NAP server is down.
    - ``Timeout``: the NAP API didn't respond in time.
    - HTTP 401: missing, invalid or expired API key (plain text).
    - HTTP 500: internal NAP server error.
    """
    import requests

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise ConnectionError(
            f"Could not connect to the NAP API ({url}). "
            "Check your internet connection or try again later."
        ) from None
    except requests.exceptions.Timeout:
        raise TimeoutError(
            f"The NAP API did not respond within {timeout}s ({url}). "
            "The server may be overloaded; try again later."
        ) from None

    if resp.status_code == 401:
        raise NAPAuthenticationError(
            f"Authentication error from the NAP API: {resp.text.strip()}. "
            "Check that NAP_API_KEY is valid and hasn't expired."
        )

    if resp.status_code == 500:
        try:
            server_msg = resp.json().get("message", resp.text)
        except Exception:
            server_msg = resp.text

        raise RuntimeError(
            f"Internal NAP server error (HTTP 500) at {url}: {server_msg}. "
            "Try again later."
        )

    return resp


def download_gtfs_nap_zips(
    area_name: str,
    output_dir: str | Path,
    *,
    modes: Iterable[int] = (1, 2),
    api_key: str | None = None,
    base_url: str = NAP_BASE_URL,
    fail_fast: bool = False,
    province_code: int | None = None,
) -> list[Path]:
    """Download the NAP GTFS zip files for the study area's province.

    NAP mode ids: 1 bus, 2 rail, 3 maritime, 4 air.
    The key is taken from ``api_key``, the ``NAP_API_KEY`` environment
    variable or a ``.env`` file (see :func:`resolve_nap_api_key`). A key
    the NAP rejects raises :class:`NAPAuthenticationError`, also when it
    happens while downloading the files.

    ``province_code`` is the INE province code (1-52), which is exactly
    the region id understood by the API. This is the reliable path, and
    the one used by :func:`get_network.main`, which gets it from the
    boundary. If not given, ``area_name`` is compared against the names of
    the 52 province entries in the ``/region`` listing; never against the
    municipality or region (CCAA) entries, which return 404 or another
    province's data (see ``NAP_PROVINCE_IDS``).

    If the province has no published dataset, the API responds with 404:
    that isn't an error, so it's logged and an empty list is returned
    instead of raising an exception.
    """
    import pandas as pd
    import requests

    api_key = resolve_nap_api_key(api_key)
    if not api_key:
        raise ValueError(missing_nap_key_message())

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    headers = {"ApiKey": api_key}

    if province_code is not None and int(province_code) not in NAP_PROVINCE_IDS:
        raise ValueError(
            f"province_code '{province_code}' is not valid: must be an "
            "INE province code between 1 and 52."
        )

    region_id = int(province_code) if province_code is not None else None
    if region_id is None:
        regions_response = _nap_get(f"{base_url}/region", headers=headers)
        regions_response.raise_for_status()
        regions = regions_response.json().get("data", [])
        if not regions:
            raise ValueError("The NAP API did not return any region.")

        # Only province entries (``tipo == "0"``) have an id that the
        # datasets endpoint knows how to interpret.
        provinces = [item for item in regions if str(item.get("tipo")) == "0"]
        if not provinces:
            raise ValueError("The NAP API did not return any province region (tipo=0).")

        target = normalize_name(area_name)
        best_region = max(
            provinces,
            key=lambda item: difflib.SequenceMatcher(
                None, target, normalize_name(item.get("nombre", ""))
            ).ratio(),
        )
        region_id = int(best_region["id"])
        logger.warning(
            "Could not infer the province for '%s' from the boundaries "
            "file; using province '%s' based on name similarity, which "
            "may not be correct.",
            area_name,
            best_region.get("nombre"),
        )

    datasets_response = _nap_get(
        f"{base_url}/conjunto-dato/region/{region_id}",
        headers=headers,
    )

    if datasets_response.status_code == 404:
        try:
            error_data = datasets_response.json()
            error_message = error_data.get("message", "")
        except Exception:
            error_message = ""

        # Do NOT translate this string: it's matched against the NAP API's
        # own (Spanish-language) error response text, not our own message.
        if "no se ha encontrado ningun conjunto de datos" in normalize_name(
            error_message
        ):
            logger.info(
                "NAP has no published datasets for region %s; no GTFS "
                "will be downloaded.",
                region_id,
            )
            return []

        # Unexpected 404 (broken endpoint, etc.)
        logger.error(
            "Unexpected 404 at %s: %s",
            datasets_response.url,
            error_message or datasets_response.text,
        )

    datasets_response.raise_for_status()
    datasets = datasets_response.json().get("data", [])
    mode_set = {int(mode) for mode in modes}

    downloaded: list[Path] = []
    report_rows: list[dict[str, object]] = []
    for dataset in datasets:
        dataset_modes = {int(item["id"]) for item in dataset.get("tiposTransporte", [])}
        if mode_set and dataset_modes.isdisjoint(mode_set):
            continue

        for file_info in dataset.get("ficheros", []):
            file_id = file_info.get("id")
            if file_id is None:
                continue
            row = {
                "dataset_id": dataset.get("id"),
                "dataset_name": dataset.get("nombre"),
                "file_id": file_id,
                "status": "pending",
                "file_name": None,
                "path": None,
                "error": None,
            }
            try:
                # Through _nap_get, like the other NAP calls, so a rejected
                # key raises a clear NAPAuthenticationError instead of
                # becoming one more "failed" row in the report.
                link_response = _nap_get(
                    f"{base_url}/fichero/{file_id}/descarga", headers=headers
                )
                link_response.raise_for_status()
                payload = link_response.json().get("data", {})
                download_url = payload.get("enlaceDescarga")
                file_name = payload.get("nombreFichero") or f"nap_gtfs_{file_id}.zip"
                row["file_name"] = file_name
                if not download_url:
                    raise ValueError("The NAP response did not include enlaceDescarga")

                zip_response = requests.get(download_url, timeout=120)
                zip_response.raise_for_status()
                path = output_dir / file_name
                path.write_bytes(zip_response.content)
                downloaded.append(path)
                row["status"] = "downloaded"
                row["path"] = str(path)
            except Exception as exc:  # noqa: BLE001 - logged and execution continues
                row["status"] = "failed"
                row["error"] = str(exc)
                # A rejected key fails every file the same way: stop and
                # report it, as with fail_fast.
                if fail_fast or isinstance(exc, NAPAuthenticationError):
                    report_rows.append(row)
                    pd.DataFrame(report_rows).to_csv(
                        output_dir / "nap_download_report.csv", index=False
                    )
                    raise
            report_rows.append(row)

    if report_rows:
        pd.DataFrame(report_rows).to_csv(
            output_dir / "nap_download_report.csv", index=False
        )

    return downloaded


def download_gtfs_layers(
    area_name: str,
    boundary: gpd.GeoDataFrame,
    zip_dir: str | Path,
    *,
    modes: Iterable[int],
    output_crs: str = "EPSG:4326",
    clip_to_boundary: bool = True,
    api_key: str | None = None,
    hour_band_size: int = 1,
    hour_range: tuple[int, int] | None = None,
    peak_periods: dict[str, tuple[int, int]] | None = None,
    include_schedule_table: bool = True,
    province_code: int | None = None,
    buffer_m: float = 0.0,
) -> dict[str, dict[str, object]]:
    """Download NAP GTFS data and returns normalized layers per dataset.

    Returns ``{dataset: {"nodes_stops": gdf, "edges": gdf,
    "edges_shapes_reference": gdf, "schedule": DataFrame | None}}``.

    ``province_code`` is the area's INE province code; if not given, it's
    taken from the ``boundary``'s ``province_code`` column (filled in by
    :func:`find_area_boundary`). See :func:`download_gtfs_nap_zips`.

    ``hour_band_size`` / ``hour_range`` / ``peak_periods`` /
    ``include_schedule_table`` are forwarded to
    ``process_gtfs.normalize_gtfs_feed`` (see that function for details on
    options A/B/C).

    ``buffer_m`` widens the clipping area by that many metres (see
    :func:`study_area_geometry`), so stops just across the municipal
    boundary are kept.
    """
    if province_code is None and "province_code" in getattr(boundary, "columns", []):
        value = boundary["province_code"].iloc[0]
        province_code = None if value is None or value != value else int(value)

    zips = download_gtfs_nap_zips(
        area_name,
        zip_dir,
        modes=modes,
        api_key=api_key,
        province_code=province_code,
    )

    filter_geometry = None
    if clip_to_boundary and not boundary.empty:
        filter_geometry = study_area_geometry(boundary, buffer_m=buffer_m)

    results: dict[str, dict[str, object]] = {}
    for zip_path in zips:
        dataset_name = slugify(Path(zip_path).stem)
        try:
            stops, stop_edges, schedule = process_gtfs.normalize_gtfs_feed(
                zip_path,
                dataset_name=dataset_name,
                layer_id=dataset_name,
                filter_geometry=filter_geometry,
                target_crs=output_crs,
                hour_band_size=hour_band_size,
                hour_range=hour_range,
                peak_periods=peak_periods,
                include_schedule_table=include_schedule_table,
            )
        except Exception as exc:  # noqa: BLE001 - a corrupt feed shouldn't break everything
            logger.warning("Could not normalize feed %s: %s", zip_path.name, exc)
            continue
        results[dataset_name] = {
            "nodes": stops,
            "edges": stop_edges,
            "schedule": schedule,
            # "bus" or "train", inferred from the feed's route_type. Used
            # by get_network to decide the dataset's output folder.
            "mode": process_gtfs.infer_transport_mode(zip_path),
        }
    return results
