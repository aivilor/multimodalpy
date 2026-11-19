"""Municipal boundaries: the boundaries file and the study-area lookup.

Private module. The public entry point is ``multimodalpy.area``.
"""

from __future__ import annotations

import logging
import re
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import geopandas as gpd
    import pandas as pd

from ._text import _typo_similarity, normalize_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Boundaries file
# ---------------------------------------------------------------------------
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


# Formats accepted as the boundaries file: Shapefile, GeoJSON and, since
# 0.5.0, GeoPackage (see ``_boundaries_layer`` for files with several layers).
ALLOWED_BOUNDARY_SUFFIXES = {".shp", ".geojson", ".json", ".gpkg"}


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

# ---------------------------------------------------------------------------
# Provinces
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Study-area resolution (geometry -> provinces and municipalities)
# ---------------------------------------------------------------------------
# Equal-area CRS (ETRS89-LAEA Europe) for the overlap shares, so a hectare
# counts the same in Girona and in Huelva.
_AREA_CRS = "EPSG:3035"

#: Share of the study area a province must hold for its GTFS feeds to be
#: downloaded. A study area drawn along municipal lines, but with another
#: precision than the packaged file, grazes the neighbouring municipalities
#: and provinces: without a minimum, each of those slivers would add a
#: province to the download.
MIN_PROVINCE_SHARE = 0.05

#: A municipality is listed when at least this share of it is inside the
#: study area, or it holds at least this share of the study area.
MIN_MUNICIPALITY_SHARE = 0.05

_COVERAGE_NOTE = (
    "multimodalpy covers peninsular Spain, the Balearic Islands, Ceuta and "
    "Melilla, but not the Canary Islands"
)


def _boundaries_layer(path: Path) -> str | None:
    """Layer of a GeoPackage to read as the study area.

    A GeoPackage with a single layer is read as it is; with several, the
    only one with polygons. Several polygon layers are ambiguous: the error
    lists them, so the one wanted can be saved on its own.
    """
    import geopandas as gpd

    layers = gpd.list_layers(path)
    if len(layers) <= 1:
        return None
    polygons = layers.loc[
        layers["geometry_type"].astype(str).str.contains("Polygon"), "name"
    ].tolist()
    if len(polygons) == 1:
        return str(polygons[0])
    found = ", ".join(map(str, polygons or layers["name"]))
    raise ValueError(
        f"The GeoPackage {path.name} has several layers ({found}) and it isn't "
        "clear which one is the study area. Save the layer you want on its own "
        "(e.g. as a GeoPackage with only that layer) and pass that file."
    )


def _read_boundaries_file(path: str | Path) -> gpd.GeoDataFrame:
    """Read a boundaries file, checking its format, contents and CRS."""
    path = Path(path)
    if path.suffix.lower() not in ALLOWED_BOUNDARY_SUFFIXES:
        raise ValueError(
            "boundaries_path only accepts Shapefile (.shp), GeoJSON "
            f"(.geojson/.json) or GeoPackage (.gpkg). Received: {path.suffix}"
        )
    if not path.exists():
        raise FileNotFoundError(f"Boundaries file does not exist: {path}")

    import geopandas as gpd

    layer = _boundaries_layer(path) if path.suffix.lower() == ".gpkg" else None
    gdf = gpd.read_file(path, layer=layer)
    if gdf.empty:
        raise ValueError(f"Boundaries file is empty: {path}")
    if gdf.crs is None:
        raise ValueError(
            f"Boundaries file has no CRS defined: {path}. Define it (e.g. the "
            ".prj file of a shapefile, or set_crs() in geopandas and save the "
            "file again) so its coordinates can be placed."
        )
    return gdf


def _ine_code(value: object) -> str | None:
    """5-digit INE municipality code from an INSPIRE NATCODE or INE code."""
    digits = re.sub(r"\D", "", "" if value is None else str(value))
    if len(digits) >= 8:
        return digits[-5:]
    if len(digits) == 5:
        return digits
    return None


def _municipality_rows(gdf: gpd.GeoDataFrame) -> pd.Series:
    """Mask of the rows that are municipalities (not shared territories)."""
    import pandas as pd

    if "NATLEVNAME" not in gdf.columns:
        return pd.Series(True, index=gdf.index)
    return gdf["NATLEVNAME"].astype(str) == "Municipio"


def _municipalities_of(
    rows: gpd.GeoDataFrame, name_column: str
) -> tuple[list[str], list[str]]:
    """INE codes and names of the municipalities among ``rows``, in order.

    A municipality drawn as several polygons appears once. Rows without a
    readable code (or without a code column) are listed by name.
    """
    code_columns = _existing_columns(rows, PROVINCE_CODE_COLUMNS)
    codes: list[str] = []
    names: list[str] = []
    seen: set[str] = set()
    for index in rows.index[_municipality_rows(rows)]:
        name = str(rows.loc[index, name_column])
        code = _ine_code(rows.loc[index, code_columns[0]]) if code_columns else None
        key = code or name
        if key in seen:
            continue
        seen.add(key)
        codes.append(code or "")
        names.append(name)
    return codes, names


def _province_labels(codes: Iterable[int]) -> list[str]:
    return [PROVINCE_NAMES[code][0] for code in codes]


def _area_from_geometry(
    gdf: gpd.GeoDataFrame,
) -> tuple[list[int], list[str], list[str]]:
    """Provinces and municipalities a study area overlaps.

    The geometry is crossed with the packaged municipal boundaries. Returns
    the INE codes of the provinces holding at least ``MIN_PROVINCE_SHARE``
    of the area (largest share first), and the INE codes and names of the
    municipalities that pass ``MIN_MUNICIPALITY_SHARE`` (by code).
    """
    import geopandas as gpd

    area = gdf.to_crs(_AREA_CRS).geometry.make_valid().union_all()
    if area.is_empty or area.area <= 0:
        raise ValueError(
            "The boundaries file has no polygon with an area: the study area "
            "must be a polygon (points and lines are not accepted)."
        )

    municipalities = gpd.read_file(
        _default_boundaries_path(),
        bbox=gpd.GeoSeries([area], crs=_AREA_CRS),
    )
    municipalities = municipalities.to_crs(_AREA_CRS)
    overlap = municipalities.geometry.make_valid().intersection(area).area
    municipalities = municipalities.assign(_overlap=overlap)
    municipalities = municipalities.loc[municipalities["_overlap"] > 0]

    total = area.area
    covered = municipalities["_overlap"].sum() / total
    provinces = municipalities["NATCODE"].map(province_code_from_natcode)
    is_municipality = _municipality_rows(municipalities)
    shares = (
        municipalities.loc[is_municipality & provinces.notna(), "_overlap"]
        .groupby(provinces.loc[is_municipality & provinces.notna()].astype(int))
        .sum()
        / total
    )
    # Largest share first; equal shares (to 6 decimals) by province code.
    selected = shares.loc[shares >= MIN_PROVINCE_SHARE]
    selected = sorted(
        selected.index, key=lambda code: (-round(selected[code], 6), code)
    )
    if not selected:
        raise ValueError(
            "The study area is outside the area covered by the library: no "
            f"province holds at least {MIN_PROVINCE_SHARE:.0%} of it "
            f"({covered:.0%} of it lies within the municipal boundaries). "
            f"{_COVERAGE_NOTE}."
        )
    if covered < 1 - MIN_PROVINCE_SHARE:
        warnings.warn(
            f"{1 - covered:.0%} of the study area is outside the municipal "
            "boundaries (sea, or outside the covered area): no bus or train "
            f"data is downloaded there. {_COVERAGE_NOTE}.",
            UserWarning,
            stacklevel=3,
        )

    own_share = municipalities["_overlap"] / municipalities.geometry.area
    area_share = municipalities["_overlap"] / total
    listed = municipalities.loc[
        is_municipality
        & (
            (own_share >= MIN_MUNICIPALITY_SHARE)
            | (area_share >= MIN_MUNICIPALITY_SHARE)
        )
    ]
    listed = listed.assign(_code=listed["NATCODE"].map(_ine_code)).sort_values("_code")
    name_column = _existing_columns(listed, ("NAMEUNIT", *DEFAULT_NAME_COLUMNS))[0]
    mun_codes, mun_names = _municipalities_of(listed, name_column)
    return [int(code) for code in selected], mun_codes, mun_names


def boundary_for_export(boundary: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Return the study-area layer with its list columns joined as ``"a; b"``.

    GeoJSON, Shapefile and GeoPackage store no lists, so ``prov_codes``,
    ``prov_names``, ``mun_codes`` and ``mun_names`` are written as text.
    """
    out = boundary.copy()
    for column in out.columns:
        if column == out.geometry.name:
            continue
        if out[column].map(lambda value: isinstance(value, list)).any():
            out[column] = out[column].map(
                lambda value: (
                    "; ".join(str(item) for item in value)
                    if isinstance(value, list)
                    else value
                )
            )
    return out


def find_area(
    area_name: str | None = None,
    boundaries_path: str | Path | None = None,
    *,
    area_code: str | None = None,
    province: int | str | None = None,
    target_crs: str = "EPSG:4326",
) -> gpd.GeoDataFrame:
    """Find the study area: a municipality by name, or your own polygon.

    There are two ways to give the area:

    - **By name** (``area_name`` or ``area_code``): the municipality is
      looked up in the packaged municipal boundaries.
    - **By geometry** (``boundaries_path``): the polygons in the file
      (several are merged into one) are the study area, whatever their
      columns. They are crossed with the packaged municipal boundaries to
      know which provinces and municipalities the area overlaps; a
      province counts when it holds at least 5% of the area
      (``MIN_PROVINCE_SHARE``). Its GTFS feeds are then downloaded, and
      everything is clipped to your polygon.

    Parameters
    ----------
    area_name : str, optional
        Municipality name. Matching ignores accents and case, accepts either
        part of a bilingual name (``"Elche"`` finds ``Elx/Elche``) and
        tolerates small typos; looser matches emit a ``UserWarning`` naming
        the area picked. With ``boundaries_path`` it is only the area's
        label, and defaults to the file name (``"pateval"`` for
        ``pateval.geojson``).
    boundaries_path : str | Path, optional
        Shapefile (``.shp``), GeoJSON (``.geojson`` / ``.json``) or
        GeoPackage (``.gpkg``; with several layers, the only one with
        polygons) with the study area. It must have a CRS (any one: it's
        converted); the area
        must overlap the covered territory: peninsular Spain, the Balearic
        Islands, Ceuta and Melilla (not the Canary Islands).
    area_code : str, optional
        The municipality's 5-digit INE code (``"46250"``) or its full
        INSPIRE NATCODE (``"34104646250"``), to pick it exactly. Only by
        name.
    province : str | int, optional
        Province name or INE code (1-52), to choose between municipalities
        that share a name. Only by name.
    target_crs : str
        CRS of the result. Defaults to ``"EPSG:4326"``.

    Returns
    -------
    geopandas.GeoDataFrame
        One row with the study area's polygon and:

        - ``area_name``: the label used for the output files;
        - ``prov_codes`` / ``prov_names``: INE codes and names of the
          provinces, whose GTFS feeds are downloaded;
        - ``mun_codes`` / ``mun_names``: INE codes and names of the
          municipalities in the area;
        - ``match_col``, ``match_val``, ``match_rows`` and
          ``area_code``: how the name was found (empty by geometry, except
          ``match_rows``, the number of polygons merged);
        - ``source``: the boundaries file read.

    Raises
    ------
    AmbiguousAreaError
        If the name matches several different municipalities; the error
        lists them, so ``area_code`` or ``province`` can choose one.
    ValueError
        If no municipality matches; if ``boundaries_path`` is not a
        Shapefile, GeoJSON or GeoPackage, has no CRS or lies outside the covered
        territory; or if ``area_code`` or ``province`` are given with
        ``boundaries_path``.

    Examples
    --------
    >>> from multimodalpy import find_area  # doctest: +SKIP
    >>> valencia = find_area("Valencia")  # doctest: +SKIP
    >>> castejon = find_area("Castejón", province="Navarra")  # doctest: +SKIP
    >>> metro = find_area(boundaries_path="pateval.geojson")  # doctest: +SKIP
    >>> metro[["prov_names", "mun_names"]]  # doctest: +SKIP
    """
    import geopandas as gpd

    if boundaries_path is not None:
        if area_code is not None or province is not None:
            raise ValueError(
                "area_code and province only apply to a search by name; with "
                "boundaries_path the study area is the file's geometry."
            )
        boundaries_path = Path(boundaries_path)
        gdf = _read_boundaries_file(boundaries_path)
        prov_codes, mun_codes, mun_names = _area_from_geometry(gdf)
        geometry = gdf.to_crs(target_crs).geometry.make_valid().union_all()
        label = area_name or boundaries_path.stem
        return gpd.GeoDataFrame(
            [
                {
                    "area_name": label,
                    "prov_codes": prov_codes,
                    "prov_names": _province_labels(prov_codes),
                    "mun_codes": mun_codes,
                    "mun_names": mun_names,
                    "match_col": None,
                    "match_val": None,
                    "match_rows": len(gdf),
                    "area_code": None,
                    "source": str(boundaries_path),
                }
            ],
            geometry=[geometry],
            crs=target_crs,
        )

    if not area_name and not area_code:
        raise ValueError(
            "Give the study area: area_name (or area_code) to look a "
            "municipality up, or boundaries_path with your own polygon."
        )

    default_path = _default_boundaries_path()
    gdf = _read_boundaries_file(default_path)
    try:
        rows, matched_column, matched_value = _select_area_rows(
            gdf,
            area_name or "",
            area_code=area_code,
            province=province,
        )
    except AmbiguousAreaError:
        raise
    except ValueError as exc:
        if str(exc).startswith("No boundary found"):
            raise ValueError(
                f"{exc} {_COVERAGE_NOTE}; for an area of your own pass "
                "boundaries_path=...."
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

    name_columns = _existing_columns(rows, DEFAULT_NAME_COLUMNS)
    name_column = name_columns[0] if name_columns else matched_column
    mun_codes, mun_names = _municipalities_of(rows, name_column)
    row_provinces = _row_province_codes(rows)
    prov_codes = (
        []
        if row_provinces is None
        else sorted({int(code) for code in row_provinces.dropna()})
    )

    rows = rows.to_crs(target_crs)
    geometry = rows.geometry.union_all()
    label = area_name or (mun_names[0] if mun_names else str(area_code))

    return gpd.GeoDataFrame(
        [
            {
                "area_name": label,
                # INE province codes (1-52): the region ids understood by
                # the NAP API.
                "prov_codes": prov_codes,
                "prov_names": _province_labels(prov_codes),
                "mun_codes": mun_codes,
                "mun_names": mun_names,
                "match_col": matched_column,
                "match_val": matched_value,
                "match_rows": len(rows),
                "area_code": area_code,
                "source": str(default_path),
            }
        ],
        geometry=[geometry],
        crs=target_crs,
    )
