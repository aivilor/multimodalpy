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

Credentials: no key is stored in the code. The NAP key is read from the
``NAP_API_KEY`` environment variable (or a local ``.env`` file).
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from . import process_gtfs, process_osm

if TYPE_CHECKING:
    import geopandas as gpd
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
# TODO: once downloaded successfully, fill in the real hash (see below)
# so pooch can verify the file's integrity on every download.
_BOUNDARIES_RELEASE_HASH: str | None = None


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
    shp_files = [Path(p) for p in extracted if p.endswith(".shp")]
    if not shp_files:
        raise FileNotFoundError(
            "No .shp file found in the resource downloaded from "
            f"{_BOUNDARIES_RELEASE_URL}"
        )
    return shp_files[0]


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
def load_dotenv(env_path: str | Path | None = None) -> None:
    """Load variables from a ``.env`` file into ``os.environ`` (if it exists)."""
    path = Path(env_path or ".env")
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
def _existing_columns(gdf: gpd.GeoDataFrame, requested: Iterable[str]) -> list[str]:
    return [column for column in requested if column in gdf.columns]


def _select_area_rows(
    gdf: gpd.GeoDataFrame,
    area_name: str,
    area_code: str | None = None,
    name_columns: Iterable[str] | None = None,
    code_columns: Iterable[str] | None = None,
) -> tuple[gpd.GeoDataFrame, str, str]:
    if area_code:
        for column in _existing_columns(gdf, code_columns or DEFAULT_CODE_COLUMNS):
            normalized = gdf[column].astype(str).str.strip()
            exact_mask = normalized == str(area_code).strip()
            if exact_mask.any():
                value = str(gdf.loc[exact_mask, column].iloc[0])
                return gdf.loc[exact_mask].copy(), column, value
        raise ValueError(f"No boundary found for code '{area_code}'.")

    target = normalize_name(area_name)
    columns = _existing_columns(gdf, name_columns or DEFAULT_NAME_COLUMNS)
    if not columns:
        raise ValueError(
            "No name column found in the boundaries file. "
            f"Available columns: {list(gdf.columns)}"
        )

    best_rows = best_column = best_value = None
    best_score = 0.0

    for column in columns:
        normalized = gdf[column].map(normalize_name)

        exact_mask = normalized == target
        if exact_mask.any():
            value = str(gdf.loc[exact_mask, column].iloc[0])
            return gdf.loc[exact_mask].copy(), column, value

        contains_mask = normalized.str.contains(target, regex=False, na=False)
        if contains_mask.any():
            value = str(gdf.loc[contains_mask, column].iloc[0])
            return gdf.loc[contains_mask].copy(), column, value

        for value in normalized.dropna().unique().tolist():
            distance = levenshtein_distance(target, value)
            score = levenshtein_similarity(target, value)
            max_allowed_distance = max(2, round(max(len(target), len(value)) * 0.3))
            if (
                distance <= max_allowed_distance
                and score >= 0.70
                and score > best_score
            ):
                mask = normalized == value
                best_rows = gdf.loc[mask].copy()
                best_column = column
                best_value = str(gdf.loc[mask, column].iloc[0])
                best_score = score

    if best_rows is None:
        raise ValueError(f"No boundary found for area '{area_name}'.")
    assert best_column is not None
    assert best_value is not None
    return best_rows, best_column, best_value


def find_area_boundary(
    area_name: str,
    boundaries_path: str | Path | None = None,
    *,
    area_code: str | None = None,
    name_columns: Iterable[str] | None = None,
    code_columns: Iterable[str] | None = None,
    target_crs: str = "EPSG:4326",
) -> gpd.GeoDataFrame:
    """Return a single (dissolved) polygon for the given municipality.

    ``boundaries_path`` must be a shapefile (.shp) or a GeoJSON
    (.geojson/.json). If not given, the packaged municipal-boundary
    shapefile is used. Name matching is tolerant to accents and typos
    (Levenshtein).
    """
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

    rows, matched_column, matched_value = _select_area_rows(
        gdf,
        area_name,
        area_code=area_code,
        name_columns=name_columns,
        code_columns=code_columns,
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
# OSM download (osmnx) + standardization (process_osm)
# ---------------------------------------------------------------------------
def download_osm_layers(
    boundary: gpd.GeoDataFrame,
    *,
    modes: Iterable[str] = ("caminable",),
    output_crs: str = "EPSG:4326",
    simplify: bool = True,
    retain_all: bool = True,
) -> dict[str, dict[str, object]]:
    """Download OSM network layers for the given modes and standardizes them.

    Returns ``{mode: {"nodes": gdf, "edges": gdf, "graph": osmnx_graph}}``,
    where ``mode`` is always the canonical English label ("walking", "bike",
    "driving"), regardless of the synonym used in ``modes`` (e.g. "coche",
    "car", "drive" and "driving" all produce the "driving" key).

    ``nodes``/``edges`` are already the final layers, ready to export (see
    :func:`multimodalpy.process_osm.build_final_osm_layers`):

    - ``nodes``: ``node_id``, ``node_role``, ``geometry``.
    - ``edges``: ``edge_id``, ``from_node_id``, ``to_node_id``, ``highway``,
      ``lanes``, ``maxspeed``, ``name``, ``oneway``, ``reversed``, ``length``,
      ``tts`` (travel time in seconds: free-flow speed + stop penalty based
      on the mode), ``geometry``.

    Travel time is computed automatically based on the network mode
    (``walking``: constant 5 km/h with no penalty; ``bike``: speed capped
    at 25 km/h with a reduced stop penalty; ``driving``: speed from
    ``maxspeed``/road type with a stop penalty based on the arrival node).
    ``travel_speed_kmh`` no longer needs to be specified.
    """
    try:
        import osmnx as ox
    except ImportError as exc:
        raise ImportError(
            "Install osmnx to download OSM networks: pip install osmnx"
        ) from exc

    area_wgs84 = boundary.to_crs("EPSG:4326")
    polygon = area_wgs84.geometry.iloc[0]
    results: dict[str, dict[str, object]] = {}

    for mode in modes:
        input_key = normalize_name(mode)
        network_type = OSM_NETWORK_TYPES.get(input_key)
        if network_type is None:
            valid = ", ".join(sorted(OSM_NETWORK_TYPES))
            raise ValueError(f"Unknown OSM mode '{mode}'. Valid values: {valid}")
        layer_key = NETWORK_TYPE_TO_LAYER_LABEL[network_type]

        graph = ox.graph_from_polygon(
            polygon,
            network_type=network_type,
            simplify=simplify,
            retain_all=retain_all,
        )
        nodes, edges = process_osm.build_final_osm_layers(
            graph,
            layer_id=layer_key,
            mode=network_type,
            output_crs=output_crs,
        )
        results[layer_key] = {"nodes": nodes, "edges": edges, "graph": graph}

    return results


# ---------------------------------------------------------------------------
# GTFS download (NAP API) + standardization (process_gtfs)
# ---------------------------------------------------------------------------
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
        raise ValueError(
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
    The key is taken from ``api_key`` or the ``NAP_API_KEY`` environment
    variable.

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

    if not api_key and not os.environ.get("NAP_API_KEY"):
        # The key can live in a local .env file (see the module docstring);
        # without this call it was never read. Variables already set in
        # the environment take priority over the file.
        load_dotenv()
    api_key = api_key or os.environ.get("NAP_API_KEY")
    if not api_key:
        raise ValueError(
            "Missing NAP API key. Set NAP_API_KEY in the environment or a "
            ".env file, or pass api_key=... (only needed for public-"
            "transport modes)."
        )

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
                link_response = requests.get(
                    f"{base_url}/fichero/{file_id}/descarga",
                    headers=headers,
                    timeout=60,
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
                if fail_fast:
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
        filter_geometry = boundary.to_crs("EPSG:4326").geometry.iloc[0]

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