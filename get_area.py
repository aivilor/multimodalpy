"""Descarga de datos desde APIs OSM y GTFS + llamada a process_osm / process_gtfs.

Este modulo resuelve un area de estudio (municipio) a partir de un fichero de
limites administrativos y descarga las capas de red:

- OSM (via ``osmnx``) para modos caminable / bicicleta / coche, normalizando el
  resultado con :mod:`multimodalpy.process_osm`.
- GTFS oficial (via la API del NAP, Punto de Acceso Nacional) para bus / tren,
  normalizando el resultado con :mod:`multimodalpy.process_gtfs`.

Las funciones devuelven objetos en memoria (GeoDataFrames / grafos). La escritura
a disco en el formato elegido por la persona usuaria la realiza
:mod:`multimodalpy.get_network`.

Credenciales: no se guarda ninguna clave en el codigo. La clave del NAP se lee de
la variable de entorno ``NAP_API_KEY`` (o de un fichero ``.env`` local).
"""

from __future__ import annotations

import difflib
import os
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from . import process_gtfs, process_osm

if TYPE_CHECKING:
    import geopandas as gpd


NAP_BASE_URL = "https://nap.transportes.gob.es/api/v2"

# Shapefile de recintos municipales que se distribuye dentro del paquete.
_PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_BOUNDARIES_PATH = (
    _PACKAGE_DIR
    / "data"
    / "recintos_municipales_inspire_peninbal_etrs89"
    / "recintos_municipales_inspire_peninbal_etrs89.shp"
)

# Solo se aceptan shapefile o geojson como fichero de limites (indicacion de la
# reunion: nada de geopackage para ``boundaries_path``).
ALLOWED_BOUNDARY_SUFFIXES = {".shp", ".geojson", ".json"}

DEFAULT_NAME_COLUMNS = (
    "nombre", "NOMBRE", "name", "NAME",
    "municipio", "MUNICIPIO", "mun_name", "MUN_NAME",
    "nombre_mun", "NOMBRE_MUN", "nom_mun", "NOM_MUN",
    "provincia", "PROVINCIA", "comunidad", "COMUNIDAD",
    "ccaa", "CCAA", "texto", "TEXTO", "rotulo", "ROTULO", "NAMEUNIT",
)

DEFAULT_CODE_COLUMNS = (
    "NATCODE", "natcode", "INSPIREID", "inspireid",
    "codigo", "CODIGO", "cod_mun", "COD_MUN", "ine", "INE",
)

# El endpoint ``/conjunto-dato/region/{id}`` del NAP solo entiende ids de
# provincia: los codigos INE 1-52. El listado ``/region``, en cambio, devuelve
# ~8300 entradas (municipios, CCAA y provincias) que comparten ese mismo
# espacio de ids, asi que buscar ahi por nombre es una trampa:
#
# - Las entradas de municipio (``tipo=3``) llevan el codigo INE de 5 digitos,
#   siempre fuera del rango 1-52, y el endpoint responde 404.
# - Las entradas de CCAA (``tipo=1``) llevan el codigo de CCAA (1-19), que si
#   cae dentro del rango provincial, asi que el endpoint devuelve en silencio
#   los datos de OTRA provincia (p. ej. la entrada "Madrid" tiene id=13 y
#   acaba sirviendo Ciudad Real, que es la provincia 13).
#
# Por eso la provincia se deduce del codigo oficial del municipio y no de una
# comparacion de nombres.
NAP_PROVINCE_IDS = frozenset(range(1, 53))

# Columnas de las que se puede extraer el codigo de provincia, en orden de
# preferencia. NATCODE (INSPIRE) tiene la forma
# ``<2 pais><2 ccaa><2 provincia><5 municipio>``.
PROVINCE_CODE_COLUMNS = ("NATCODE", "natcode", "cod_mun", "COD_MUN", "ine", "INE")

# Modos OSM soportados (caminando / bicicleta / coche). Se aceptan varios
# sinonimos de entrada (castellano/ingles), pero la capa resultante y el
# nombre de fichero siempre usan la etiqueta canonica en ingles: "walking",
# "bike", "driving" (ver ``NETWORK_TYPE_TO_LAYER_LABEL``).
OSM_NETWORK_TYPES = {
    "caminable": "walk", "walk": "walk", "peatonal": "walk", "walking": "walk",
    "bicicleta": "bike", "bike": "bike", "bicycle": "bike",
    "coche": "drive", "car": "drive", "drive": "drive", "carretera": "drive",
    "driving": "drive",
}

# Etiqueta canonica de capa/fichero para cada ``network_type`` de osmnx.
NETWORK_TYPE_TO_LAYER_LABEL = {
    "walk": "walking",
    "bike": "bike",
    "drive": "driving",
}


# ---------------------------------------------------------------------------
# Utilidades de texto / entorno
# ---------------------------------------------------------------------------
def load_dotenv(env_path: str | Path | None = None) -> None:
    """Carga variables de un fichero ``.env`` en ``os.environ`` (si existe)."""
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
    """Normaliza texto para comparaciones insensibles a acentos/mayusculas."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def levenshtein_distance(left: str, right: str) -> int:
    """Distancia de edicion de Levenshtein entre dos cadenas normalizadas."""
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
    """Similitud 0-1 basada en la distancia de Levenshtein."""
    max_length = max(len(left), len(right))
    if max_length == 0:
        return 1.0
    return 1 - (levenshtein_distance(left, right) / max_length)


def slugify(value: str) -> str:
    slug = normalize_name(value).replace(" ", "_")
    return slug or "area"


def province_code_from_natcode(value: object) -> int | None:
    """Extrae el codigo INE de provincia (1-52) de un NATCODE INSPIRE.

    ``NATCODE`` tiene la forma ``<2 pais><2 ccaa><2 provincia><5 municipio>``
    (p. ej. ``34074040136`` -> provincia 40, Segovia). Los dos digitos de
    provincia se repiten al inicio del codigo de municipio, asi que se prueban
    las dos posiciones y se acepta la primera que caiga en el rango valido.
    Devuelve ``None`` si el codigo no permite deducir la provincia.
    """
    digits = re.sub(r"\D", "", "" if value is None else str(value))
    if len(digits) >= 8:
        candidates = (digits[4:6], digits[6:8])
    elif len(digits) == 5:
        # Codigo INE de municipio suelto (p. ej. "40136").
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


def _find_province_code(gdf: "gpd.GeoDataFrame") -> int | None:
    """Busca el codigo de provincia en las columnas de codigo del boundary."""
    for column in _existing_columns(gdf, PROVINCE_CODE_COLUMNS):
        for raw_value in gdf[column].dropna().tolist():
            code = province_code_from_natcode(raw_value)
            if code is not None:
                return code
    return None


# ---------------------------------------------------------------------------
# Resolucion del area de estudio (municipio -> poligono)
# ---------------------------------------------------------------------------
def _existing_columns(gdf: "gpd.GeoDataFrame", requested: Iterable[str]) -> list[str]:
    return [column for column in requested if column in gdf.columns]


def _select_area_rows(
    gdf: "gpd.GeoDataFrame",
    area_name: str,
    area_code: str | None = None,
    name_columns: Iterable[str] | None = None,
    code_columns: Iterable[str] | None = None,
) -> tuple["gpd.GeoDataFrame", str, str]:
    if area_code:
        for column in _existing_columns(gdf, code_columns or DEFAULT_CODE_COLUMNS):
            normalized = gdf[column].astype(str).str.strip()
            exact_mask = normalized == str(area_code).strip()
            if exact_mask.any():
                value = str(gdf.loc[exact_mask, column].iloc[0])
                return gdf.loc[exact_mask].copy(), column, value
        raise ValueError(f"No se encontro ningun limite para el codigo '{area_code}'.")

    target = normalize_name(area_name)
    columns = _existing_columns(gdf, name_columns or DEFAULT_NAME_COLUMNS)
    if not columns:
        raise ValueError(
            "No se encontro ninguna columna de nombre en el fichero de limites. "
            f"Columnas disponibles: {list(gdf.columns)}"
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
            if distance <= max_allowed_distance and score >= 0.70 and score > best_score:
                mask = normalized == value
                best_rows = gdf.loc[mask].copy()
                best_column = column
                best_value = str(gdf.loc[mask, column].iloc[0])
                best_score = score

    if best_rows is None:
        raise ValueError(f"No se encontro ningun limite para el area '{area_name}'.")
    return best_rows, best_column, best_value


def find_area_boundary(
    area_name: str,
    boundaries_path: str | Path | None = None,
    *,
    area_code: str | None = None,
    name_columns: Iterable[str] | None = None,
    code_columns: Iterable[str] | None = None,
    target_crs: str = "EPSG:4326",
) -> "gpd.GeoDataFrame":
    """Devuelve un unico poligono (disuelto) para el municipio indicado.

    ``boundaries_path`` debe ser un shapefile (.shp) o un GeoJSON (.geojson/.json).
    Si no se indica, se usa el shapefile de recintos municipales incluido en el
    paquete. La busqueda por nombre es tolerante a acentos y erratas (Levenshtein).
    """
    if boundaries_path is None:
        boundaries_path = DEFAULT_BOUNDARIES_PATH
    boundaries_path = Path(boundaries_path)

    if boundaries_path.suffix.lower() not in ALLOWED_BOUNDARY_SUFFIXES:
        raise ValueError(
            "boundaries_path solo admite shapefile (.shp) o geojson (.geojson/.json). "
            f"Se recibio: {boundaries_path.suffix}"
        )
    if not boundaries_path.exists():
        raise FileNotFoundError(f"No existe el fichero de limites: {boundaries_path}")

    import geopandas as gpd

    gdf = gpd.read_file(boundaries_path)
    if gdf.empty:
        raise ValueError(f"El fichero de limites esta vacio: {boundaries_path}")
    if gdf.crs is None:
        raise ValueError("El fichero de limites no tiene CRS definido.")

    rows, matched_column, matched_value = _select_area_rows(
        gdf, area_name, area_code=area_code,
        name_columns=name_columns, code_columns=code_columns,
    )
    rows = rows.to_crs(target_crs)
    geometry = rows.geometry.union_all()

    return gpd.GeoDataFrame(
        [{
            "area_name": area_name,
            "matched_column": matched_column,
            "matched_value": matched_value,
            "matched_rows": len(rows),
            "area_code": area_code,
            # Codigo INE de provincia (1-52); es el id de region que entiende
            # la API del NAP. Puede ser None si el fichero de limites no trae
            # ninguna columna de codigo reconocible.
            "province_code": _find_province_code(rows),
            "source_path": str(boundaries_path),
        }],
        geometry=[geometry],
        crs=target_crs,
    )


# ---------------------------------------------------------------------------
# Descarga OSM (osmnx) + estandarizacion (process_osm)
# ---------------------------------------------------------------------------
def download_osm_layers(
    boundary: "gpd.GeoDataFrame",
    *,
    modes: Iterable[str] = ("caminable",),
    output_crs: str = "EPSG:4326",
    simplify: bool = True,
    retain_all: bool = True,
) -> dict[str, dict[str, object]]:
    """Descarga capas de red OSM para los modos indicados y las estandariza.

    Devuelve ``{modo: {"nodes": gdf, "edges": gdf, "graph": grafo_osmnx}}``,
    donde ``modo`` es siempre la etiqueta canonica en ingles ("walking",
    "bike", "driving"), independientemente del sinonimo usado en ``modes``
    (p. ej. "coche", "car", "drive" y "driving" producen todos la clave
    "driving").

    ``nodes``/``edges`` ya son las capas finales listas para exportar (ver
    :func:`multimodalpy.process_osm.build_final_osm_layers`):

    - ``nodes``: ``node_id``, ``node_role``, ``geometry``.
    - ``edges``: ``edge_id``, ``from_node_id``, ``to_node_id``, ``highway``,
      ``lanes``, ``maxspeed``, ``name``, ``oneway``, ``reversed``, ``length``,
      ``tts`` (tiempo de viaje en segundos: velocidad libre + penalizacion de
      parada segun el modo), ``geometry``.

    El tiempo de viaje se calcula automaticamente segun el modo de red
    (``walking``: 5 km/h constante sin penalizacion; ``bike``: velocidad
    limitada a 25 km/h con penalizacion de parada reducida; ``driving``:
    velocidad por ``maxspeed``/tipo de via con penalizacion de parada segun
    el nodo de llegada). Ya no hace falta indicar ``travel_speed_kmh``.
    """
    try:
        import osmnx as ox
    except ImportError as exc:
        raise ImportError("Instala osmnx para descargar redes OSM: pip install osmnx") from exc

    area_wgs84 = boundary.to_crs("EPSG:4326")
    polygon = area_wgs84.geometry.iloc[0]
    results: dict[str, dict[str, object]] = {}

    for mode in modes:
        input_key = normalize_name(mode)
        network_type = OSM_NETWORK_TYPES.get(input_key)
        if network_type is None:
            valid = ", ".join(sorted(OSM_NETWORK_TYPES))
            raise ValueError(f"Modo OSM desconocido '{mode}'. Valores validos: {valid}")
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
# Descarga GTFS (API NAP) + estandarizacion (process_gtfs)
# ---------------------------------------------------------------------------
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
    """Descarga los ZIP GTFS del NAP para la provincia del area de estudio.

    IDs de modo del NAP: 1 bus, 2 ferroviario, 3 maritimo, 4 aereo.
    La clave se toma de ``api_key`` o de la variable de entorno ``NAP_API_KEY``.

    ``province_code`` es el codigo INE de provincia (1-52), que es justo el id
    de region que entiende la API. Es la via fiable y la que usa
    :func:`get_network.main`, que lo obtiene del boundary. Si no se indica, se
    recurre a comparar ``area_name`` con los nombres de las 52 entradas de
    provincia del listado ``/region``; nunca con las de municipio o CCAA, que
    devuelven 404 o los datos de otra provincia (ver ``NAP_PROVINCE_IDS``).

    Si la provincia no tiene ningun conjunto de datos publicado, la API
    responde 404: eso no es un error, asi que se avisa y se devuelve una lista
    vacia en vez de lanzar una excepcion.
    """
    import pandas as pd
    import requests

    api_key = api_key or os.environ.get("NAP_API_KEY")
    if not api_key:
        raise ValueError(
            "Falta la clave del NAP. Define NAP_API_KEY en el entorno o en un .env, "
            "o pasa api_key=... (solo necesaria para modos de transporte publico)."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    headers = {"ApiKey": api_key}

    if province_code is not None and int(province_code) not in NAP_PROVINCE_IDS:
        raise ValueError(
            f"province_code '{province_code}' no valido: debe ser un codigo INE "
            "de provincia entre 1 y 52."
        )

    region_id = int(province_code) if province_code is not None else None
    if region_id is None:
        regions_response = requests.get(f"{base_url}/region", headers=headers, timeout=60)
        regions_response.raise_for_status()
        regions = regions_response.json().get("data", [])
        if not regions:
            raise ValueError("La API del NAP no devolvio ninguna region.")

        # Solo las entradas de provincia (``tipo == "0"``) tienen un id que el
        # endpoint de conjuntos de datos sepa interpretar.
        provinces = [item for item in regions if str(item.get("tipo")) == "0"]
        if not provinces:
            raise ValueError(
                "La API del NAP no devolvio ninguna region de provincia (tipo=0)."
            )

        target = normalize_name(area_name)
        best_region = max(
            provinces,
            key=lambda item: difflib.SequenceMatcher(
                None, target, normalize_name(item.get("nombre", ""))
            ).ratio(),
        )
        region_id = int(best_region["id"])
        print(
            f"[aviso] No se pudo deducir la provincia de '{area_name}' desde el "
            f"fichero de limites; se usa la provincia '{best_region.get('nombre')}' "
            "por parecido de nombre, que puede no ser la correcta."
        )

    datasets_response = requests.get(
        f"{base_url}/conjunto-dato/region/{region_id}", headers=headers, timeout=60,
    )
    if datasets_response.status_code == 404:
        print(
            f"[aviso] El NAP no tiene conjuntos de datos publicados para la region "
            f"{region_id}; no se descargara ningun GTFS."
        )
        return []
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
                    f"{base_url}/fichero/{file_id}/descarga", headers=headers, timeout=60,
                )
                link_response.raise_for_status()
                payload = link_response.json().get("data", {})
                download_url = payload.get("enlaceDescarga")
                file_name = payload.get("nombreFichero") or f"nap_gtfs_{file_id}.zip"
                row["file_name"] = file_name
                if not download_url:
                    raise ValueError("La respuesta del NAP no incluyo enlaceDescarga")

                zip_response = requests.get(download_url, timeout=120)
                zip_response.raise_for_status()
                path = output_dir / file_name
                path.write_bytes(zip_response.content)
                downloaded.append(path)
                row["status"] = "downloaded"
                row["path"] = str(path)
            except Exception as exc:  # noqa: BLE001 - se registra y se continua
                row["status"] = "failed"
                row["error"] = str(exc)
                if fail_fast:
                    report_rows.append(row)
                    pd.DataFrame(report_rows).to_csv(output_dir / "nap_download_report.csv", index=False)
                    raise
            report_rows.append(row)

    if report_rows:
        pd.DataFrame(report_rows).to_csv(output_dir / "nap_download_report.csv", index=False)

    return downloaded


def download_gtfs_layers(
    area_name: str,
    boundary: "gpd.GeoDataFrame",
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
    """Descarga GTFS del NAP y devuelve capas normalizadas por dataset.

    Devuelve ``{dataset: {"nodes_stops": gdf, "edges": gdf,
    "edges_shapes_reference": gdf, "schedule": DataFrame | None}}``.

    ``province_code`` es el codigo INE de provincia del area; si no se indica,
    se toma de la columna ``province_code`` del ``boundary`` (la rellena
    :func:`find_area_boundary`). Ver :func:`download_gtfs_nap_zips`.

    ``hour_band_size`` / ``hour_range`` / ``peak_periods`` /
    ``include_schedule_table`` se reenvian a ``process_gtfs.normalize_gtfs_feed``
    (ver esa funcion para el detalle de las opciones A/B/C).
    """
    if province_code is None and "province_code" in getattr(boundary, "columns", []):
        value = boundary["province_code"].iloc[0]
        province_code = None if value is None or value != value else int(value)

    zips = download_gtfs_nap_zips(
        area_name, zip_dir, modes=modes, api_key=api_key, province_code=province_code,
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
        except Exception as exc:  # noqa: BLE001 - un feed corrupto no debe romper todo
            print(f"[aviso] No se pudo normalizar el feed {zip_path.name}: {exc}")
            continue
        results[dataset_name] = {
            "nodes": stops,
            "edges": stop_edges,
            "schedule": schedule,
        }
    return results