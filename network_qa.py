from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd
import shapely

try:
    import geopandas as gpd
except ImportError:  # geopandas es opcional: solo hace falta para
    gpd = None        # leer GeoJSON / Shapefile / GeoPackage.



NODE_COLS_OSM = ["node_id", "node_role", "geometry"]
EDGE_COLS_OSM = ["edge_id", "from_node_id", "to_node_id", "highway", "lanes",
                 "maxspeed", "name", "oneway", "reversed", "length", "tts",
                 "geometry"]

VALID_NODE_ROLES_OSM = {"intersection", "linear_vertex", "through_endpoint", "endpoint"}

KNOWN_HIGHWAY_TAGS = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "cycleway", "footway",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "service", "busway", "pedestrian", "track",
    "road",
}

NODE_COLS_GTFS = ["node_id", "stop_name", "geometry"]
EDGE_COLS_GTFS = [
    "edge_id", "from_node_id", "to_node_id", "route_id", "route_short_name",
    "route_long_name", "trip_count", "tts_mean", "tts_min", "tts_max",
    "tts_mean_peak_am", "tts_mean_peak_pm", "tts_mean_rest_of_day",
    "trip_count_peak_am", "trip_count_peak_pm", "trip_count_rest_of_day",
    "tts_mean_weekday", "trip_count_weekday", "tts_mean_weekend",
    "trip_count_weekend", "days_of_week_summary", "hourly_travel_times",
    "hourly_trip_counts", "tts_mean_peak_am_weekday", "trip_count_peak_am_weekday",
    "tts_mean_peak_am_weekend", "trip_count_peak_am_weekend",
    "tts_mean_peak_pm_weekday", "trip_count_peak_pm_weekday",
    "tts_mean_peak_pm_weekend", "trip_count_peak_pm_weekend",
    "tts_mean_rest_of_day_weekday", "trip_count_rest_of_day_weekday",
    "tts_mean_rest_of_day_weekend", "trip_count_rest_of_day_weekend", "geometry",
]

VALID_DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday",
              "saturday", "sunday"}

COORD_TOL = 0.01       
LENGTH_REL_TOL = 0.02  

# Extensiones de archivo vectorial soportadas ademas de CSV/TSV
VECTOR_EXTENSIONS = {".geojson", ".json", ".shp", ".gpkg"}


# ==========================================================================
# I/O multi-formato: CSV (WKT) / GeoJSON / Shapefile / GeoPackage
# ==========================================================================
def load_table(path: Union[str, Path], layer: Optional[str] = None) -> pd.DataFrame:
    """
    Carga una tabla de nodos/aristas sin importar el formato de origen.

    - .csv / .tsv     -> lectura plana con pandas; la columna `geometry` (si
                          existe) queda como texto WKT y la parsea despues
                          `_parse_geometries`.
    - .geojson/.json  -> geopandas; la columna `geometry` queda como objetos
                          shapely ya parseados.
    - .shp            -> geopandas (requiere los .dbf/.shx/.prj junto al .shp).
                          OJO: ESRI Shapefile trunca nombres de columna a 10
                          caracteres, asi que columnas como "from_node_id"
                          pueden llegar renombradas/perdidas. `load_table` no
                          puede adivinar el nombre original de forma fiable
                          (varias columnas largas de este esquema truncan al
                          mismo prefijo de 10 caracteres), asi que si vas a
                          usar Shapefile como origen, renombra esas columnas
                          antes de pasar la tabla a los checks, o mejor usa
                          GeoJSON/GeoPackage, que no tienen ese limite.
    - .gpkg           -> geopandas; usa `layer=` para elegir la capa cuando el
                          GeoPackage contiene varias (p. ej. nodos y aristas
                          en el mismo .gpkg).

    Devuelve un DataFrame/GeoDataFrame con una columna `geometry`.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in {".csv", ".tsv"}:
        sep = "\t" if suffix == ".tsv" else ","
        return pd.read_csv(path, sep=sep)

    if suffix in VECTOR_EXTENSIONS:
        if gpd is None:
            raise ImportError(
                "geopandas es necesario para leer GeoJSON/Shapefile/GeoPackage. "
                "Instalalo con: pip install geopandas --break-system-packages"
            )
        read_kwargs = {"layer": layer} if (suffix == ".gpkg" and layer) else {}
        gdf = gpd.read_file(path, **read_kwargs)
        if gdf.geometry.name != "geometry":
            gdf = gdf.rename_geometry("geometry")
        return gdf

    raise ValueError(
        f"Extension no soportada '{suffix}' para {path}. "
        "Formatos validos: .csv, .tsv, .geojson, .json, .shp, .gpkg"
    )


def _ensure_table(obj: Union[str, Path, pd.DataFrame],
                   layer: Optional[str] = None) -> pd.DataFrame:
    """Acepta un DataFrame ya cargado, o una ruta que se carga con load_table."""
    if isinstance(obj, (str, Path)):
        return load_table(obj, layer=layer)
    return obj



@dataclass
class CheckResult:
    name: str
    level: str          # "fail" | "warn"
    n_issues: int
    issues: Optional[pd.DataFrame] = None
    note: str = ""



_VERBOSE = True


@contextlib.contextmanager
def _verbosity(verbose: bool):
    global _VERBOSE
    prev, _VERBOSE = _VERBOSE, verbose
    try:
        yield
    finally:
        _VERBOSE = prev


def _vprint(*args, **kwargs):
    if _VERBOSE:
        print(*args, **kwargs)


def _fmt(r: CheckResult) -> str:
    tag = "OK" if r.n_issues == 0 else f"{r.level.upper()} ({r.n_issues})"
    return f"[{tag}] {r.name}" + (f" — {r.note}" if r.note else "")


def _record(results, name, level, subset, note=""):
    r = CheckResult(name=name, level=level, n_issues=len(subset),
                     issues=subset if len(subset) else None, note=note)
    results.append(r)
    _vprint(_fmt(r))
    if r.n_issues and r.issues is not None:
        # `subset` suele ser un DataFrame (tiene .head()), pero check_schema
        # pasa una lista simple de nombres de columna, que no lo tiene.
        if hasattr(r.issues, "head"):
            preview = r.issues.head(5)
            _vprint(preview.to_string() if hasattr(preview, "to_string") else preview)
        else:
            _vprint(list(r.issues)[:5])
        _vprint()
    return r


def _col(df: pd.DataFrame, name: str) -> pd.Series:

    if name in df.columns:
        return df[name]
    return pd.Series([np.nan] * len(df), index=df.index, name=name)


def _missing(df: pd.DataFrame, cols: list) -> list:
    """Lista de columnas de `cols` que NO estan en `df`, preservando orden."""
    return [c for c in cols if c not in df.columns]


def check_schema(df, expected_cols, name, results):
    _vprint(f"--- {name}: schema ---")
    missing = set(expected_cols) - set(df.columns)
    extra = set(df.columns) - set(expected_cols)
    _record(results, f"{name}: expected columns present", "fail",
            sorted(missing))
    if extra:
        _vprint(f"[INFO] {name}: extra/unexpected columns: {sorted(extra)}")
    _vprint()


def _parse_geometries(geom_series: pd.Series) -> np.ndarray:
    """
    Devuelve un array numpy de geometrias shapely a partir de una columna que
    puede contener strings WKT (lectura plana de CSV) u objetos shapely ya
    parseados (p. ej. la columna geometry de un GeoDataFrame leido desde
    GeoJSON/Shapefile/GeoPackage). Las entradas mal formadas o vacias se
    convierten en None.
    """
    values = geom_series.to_numpy()
    is_str = np.array([isinstance(v, str) for v in values])

    out = np.empty(len(values), dtype=object)
    if is_str.any():
        str_vals = np.where(is_str, values, "")
        parsed = shapely.from_wkt(str_vals, on_invalid="ignore")
        out[is_str] = parsed[is_str]

    not_str = ~is_str
    if not_str.any():
        for i in np.where(not_str)[0]:
            v = values[i]
            out[i] = v if isinstance(v, shapely.Geometry) else None

    return out


def _multi_value_bad_mask(series: pd.Series, positive_only: bool) -> pd.Series:
    """
    Tags OSM como lanes/maxspeed pueden venir separados por ';' ("40;50", "3;2").
    Marca la fila como mala solo si no es nula y NO todas las partes separadas
    por ';' parsean como numeros (y, si positive_only, todas > 0).
    """
    def bad(val):
        if pd.isna(val):
            return False
        parts = str(val).split(";")
        nums = pd.to_numeric(parts, errors="coerce")
        if pd.isna(nums).any():
            return True
        if positive_only and (nums <= 0).any():
            return True
        return False
    return series.apply(bad)


def _parse_hourly_string(s) -> dict:
    """'00-01:120|01-02:120|...' -> {'00-01': 120.0, ...}. Dict vacio si NaN/mal formado."""
    if pd.isna(s) or not str(s):
        return {}
    out = {}
    try:
        for part in str(s).split("|"):
            hour, val = part.split(":")
            out[hour] = float(val)
    except (ValueError, IndexError):
        return {}
    return out


def _topology_mismatch_mask(edges: pd.DataFrame, edge_geoms: np.ndarray,
                             node_id_to_geom: dict, ok_geom: np.ndarray) -> np.ndarray:
    """
    Comun a OSM y GTFS: compara los extremos de cada LINESTRING de arista
    contra las coordenadas de sus nodos from/to (dentro de COORD_TOL).
    Las filas cuyo nodo from/to no existe en `node_id_to_geom` se omiten
    (ya quedan cubiertas por el check de integridad referencial).
    """
    mismatch_mask = np.zeros(len(edges), dtype=bool)
    from_ids = _col(edges, "from_node_id").to_numpy()
    to_ids = _col(edges, "to_node_id").to_numpy()
    for i in np.where(ok_geom)[0]:
        from_pt = node_id_to_geom.get(from_ids[i])
        to_pt = node_id_to_geom.get(to_ids[i])
        if from_pt is None or to_pt is None:
            continue
        coords = list(edge_geoms[i].coords)
        start, end = coords[0], coords[-1]
        d_start = ((start[0] - from_pt.x) ** 2 + (start[1] - from_pt.y) ** 2) ** 0.5
        d_end = ((end[0] - to_pt.x) ** 2 + (end[1] - to_pt.y) ** 2) ** 0.5
        if d_start > COORD_TOL or d_end > COORD_TOL:
            mismatch_mask[i] = True
    return mismatch_mask


def _summarize(results: list) -> None:
    n_fail = sum(1 for r in results if r.level == "fail" and r.n_issues)
    n_warn = sum(1 for r in results if r.level == "warn" and r.n_issues)
    n_ok = sum(1 for r in results if r.n_issues == 0)
    print("=== SUMMARY ===")
    print(f"{n_ok} passed, {n_fail} failed, {n_warn} warned "
          f"(out of {len(results)} checks)")


# ==========================================================================
# OSM checks
# ==========================================================================
def check_nodes_osm(nodes: pd.DataFrame, results: list) -> np.ndarray:
    _vprint("=== NODES ===")
    check_schema(nodes, NODE_COLS_OSM, "nodes", results)

    _record(results, "no fully duplicate rows", "fail", nodes[nodes.duplicated()])
    node_id = _col(nodes, "node_id")
    _record(results, "node_id is unique", "fail",
            nodes[node_id.duplicated(keep=False)].sort_values("node_id") if "node_id" in nodes.columns
            else nodes.iloc[0:0])
    _record(results, "node_id not null", "fail", nodes[node_id.isna()])
    geometry = _col(nodes, "geometry")
    _record(results, "geometry not null", "fail", nodes[geometry.isna()])

    if "node_role" in nodes.columns:
        bad_roles = nodes[~nodes["node_role"].isin(VALID_NODE_ROLES_OSM)]
    else:
        bad_roles = nodes  # columna ausente: no se puede validar ninguna fila -> todas fallan
    _record(results, f"node_role in {sorted(VALID_NODE_ROLES_OSM)}", "fail", bad_roles)

    geoms = _parse_geometries(geometry)
    is_bad = np.array([g is None or g.geom_type != "Point" or not g.is_valid
                        for g in geoms])
    _record(results, "geometry parses as valid POINT", "fail", nodes[is_bad])
    _vprint()
    return geoms


def check_edges_osm(edges: pd.DataFrame, nodes: pd.DataFrame, node_geoms: np.ndarray,
                     results: list) -> np.ndarray:
    _vprint("=== EDGES ===")
    check_schema(edges, EDGE_COLS_OSM, "edges", results)

    _record(results, "no fully duplicate rows", "fail", edges[edges.duplicated()])
    _record(results, "edge_id not null", "fail", edges[_col(edges, "edge_id").isna()])
    from_node_id = _col(edges, "from_node_id")
    to_node_id = _col(edges, "to_node_id")
    _record(results, "from_node_id not null", "fail", edges[from_node_id.isna()])
    _record(results, "to_node_id not null", "fail", edges[to_node_id.isna()])

    node_id_to_geom = dict(zip(_col(nodes, "node_id"), node_geoms))
    node_ids = set(node_id_to_geom)
    _record(results, "from_node_id exists in nodes table", "fail",
            edges[~from_node_id.isin(node_ids)])
    _record(results, "to_node_id exists in nodes table", "fail",
            edges[~to_node_id.isin(node_ids)])

    _record(results, "no self-loop edges (from == to)", "fail",
            edges[from_node_id == to_node_id])

    dup_key_cols = _missing(edges, ["edge_id", "from_node_id", "to_node_id"])
    if dup_key_cols:
        _vprint(f"[SKIP] no duplicate (edge_id, from_node_id, to_node_id) rows — "
              f"falta(n) columna(s): {dup_key_cols}")
    else:
        _record(results, "no duplicate (edge_id, from_node_id, to_node_id) rows", "warn",
                edges[edges.duplicated(subset=["edge_id", "from_node_id", "to_node_id"],
                                        keep=False)],
                note="often expected for bidirectional OSM ways sharing one edge_id")

    length = _col(edges, "length")
    tts = _col(edges, "tts")
    _record(results, "length not null", "fail", edges[length.isna()])
    _record(results, "length is non-negative", "fail", edges[length < 0])
    _record(results, "tts (travel time) is non-negative", "fail", edges[tts < 0])

    oneway = _col(edges, "oneway")
    reversed_col = _col(edges, "reversed")
    _record(results, "oneway is boolean dtype", "fail",
            edges if oneway.dtype != bool else edges.iloc[0:0])
    _record(results, "reversed is boolean dtype", "fail",
            edges if reversed_col.dtype != bool else edges.iloc[0:0])

    geometry = _col(edges, "geometry")
    edge_geoms = _parse_geometries(geometry)
    is_bad_line = np.array([g is None or g.geom_type != "LineString" or not g.is_valid
                             for g in edge_geoms])
    _record(results, "geometry parses as valid LINESTRING", "fail", edges[is_bad_line])

    ok_geom = ~is_bad_line
    mismatch_mask = _topology_mismatch_mask(edges, edge_geoms, node_id_to_geom, ok_geom)
    _record(results, "edge geometry endpoints match node coords", "fail",
            edges[mismatch_mask], note=f"tolerance {COORD_TOL} m")

    geom_len = np.array([g.length if g is not None else np.nan for g in edge_geoms])
    with np.errstate(invalid="ignore", divide="ignore"):
        rel_diff = np.abs(geom_len - length.to_numpy()) / geom_len
    len_mismatch = ok_geom & (rel_diff > LENGTH_REL_TOL)
    _record(results, "`length` column matches geometry length", "warn",
            edges[len_mismatch], note=f"tolerance {LENGTH_REL_TOL:.0%}")

    bad_speed = edges[_multi_value_bad_mask(_col(edges, "maxspeed"), positive_only=True)]
    _record(results, "maxspeed numeric (incl. ';'-separated values) when present",
            "fail", bad_speed)

    bad_lanes = edges[_multi_value_bad_mask(_col(edges, "lanes"), positive_only=True)]
    _record(results, "lanes numeric & positive (incl. ';'-separated values) when present",
            "fail", bad_lanes)

    def unknown_highway(val):
        if pd.isna(val):
            return True
        return not set(str(val).split(";")).issubset(KNOWN_HIGHWAY_TAGS)
    bad_hwy = edges[_col(edges, "highway").apply(unknown_highway)]
    _record(results, "highway tag(s) within known OSM vocabulary", "warn", bad_hwy)
    _vprint()
    return edge_geoms


def check_graph_consistency_osm(nodes: pd.DataFrame, edges: pd.DataFrame, results: list):
    _vprint("=== CROSS-TABLE CONSISTENCY ===")
    needed = _missing(edges, ["from_node_id", "to_node_id"]) + _missing(nodes, ["node_id", "node_role"])
    if needed:
        _vprint(f"[SKIP] cross-table consistency — falta(n) columna(s): {sorted(set(needed))}")
        _vprint()
        return

    used_ids = set(edges["from_node_id"]) | set(edges["to_node_id"])
    orphan_nodes = nodes[~nodes["node_id"].isin(used_ids)]
    _vprint(f"[INFO] nodes not referenced by any edge: {len(orphan_nodes)}")

    # Grado topologico: cuenta conexiones DISTINTAS por nodo, no filas crudas.
    # Varias aristas pueden compartir el mismo par (from_node_id, to_node_id)
    # (p. ej. ways bidireccionales de OSM, o el mismo segmento fisico con mas
    # de un edge_id/ruta) y deben contar como UNA sola conexion.
    node_pairs = edges[["from_node_id", "to_node_id"]].to_numpy()
    undirected_pairs = {tuple(sorted(p)) for p in node_pairs}
    unique_edges = pd.DataFrame(undirected_pairs, columns=["a", "b"])
    degree = pd.concat([unique_edges["a"], unique_edges["b"]]).value_counts()

    endpoint_nodes = nodes.loc[nodes["node_role"] == "endpoint"].copy()
    endpoint_nodes["degree"] = endpoint_nodes["node_id"].map(degree).fillna(0).astype(int)
    hi_degree_endpoints = endpoint_nodes[endpoint_nodes["degree"] > 2].sort_values(
        "degree", ascending=False
    )
    _vprint(f"[INFO] 'endpoint' nodes with degree > 2 (unexpected for a dead-end role, "
          f"degree = count of distinct connected nodes): {len(hi_degree_endpoints)}")

    if len(hi_degree_endpoints):
        _vprint(hi_degree_endpoints[["node_id", "node_role", "degree", "geometry"]]
              .to_string(index=False))
    _vprint()


def run_all_checks_osm(nodes: Union[str, Path, pd.DataFrame],
                        edges: Union[str, Path, pd.DataFrame],
                        nodes_layer: Optional[str] = None,
                        edges_layer: Optional[str] = None,
                        verbose: bool = True):
    """
    `nodes`/`edges` pueden ser DataFrames ya cargados, o rutas a archivos
    .csv/.tsv/.geojson/.shp/.gpkg — se cargan automaticamente con
    `load_table`. `nodes_layer`/`edges_layer` solo aplican a GeoPackage.

    `verbose=False` silencia la linea-por-linea de cada check (el resumen
    final se imprime siempre); usa `results_to_frame()`/`style_results()`
    sobre el valor devuelto para revisar los resultados en tabla.
    """
    nodes = _ensure_table(nodes, layer=nodes_layer)
    edges = _ensure_table(edges, layer=edges_layer)

    with _verbosity(verbose):
        results: list[CheckResult] = []
        node_geoms = check_nodes_osm(nodes, results)
        check_edges_osm(edges, nodes, node_geoms, results)
        check_graph_consistency_osm(nodes, edges, results)
    _summarize(results)
    return results


# ==========================================================================
# GTFS checks
# ==========================================================================
def check_nodes_gtfs(nodes: pd.DataFrame, results: list) -> np.ndarray:
    _vprint("=== NODES (stops) ===")
    check_schema(nodes, NODE_COLS_GTFS, "nodes", results)

    _record(results, "no fully duplicate rows", "fail", nodes[nodes.duplicated()])
    node_id = _col(nodes, "node_id")
    _record(results, "node_id is unique", "fail",
            nodes[node_id.duplicated(keep=False)].sort_values("node_id") if "node_id" in nodes.columns
            else nodes.iloc[0:0])
    _record(results, "node_id not null", "fail", nodes[node_id.isna()])
    _record(results, "stop_name not null", "warn", nodes[_col(nodes, "stop_name").isna()])
    geometry = _col(nodes, "geometry")
    _record(results, "geometry not null", "fail", nodes[geometry.isna()])

    geoms = _parse_geometries(geometry)
    is_bad = np.array([g is None or g.geom_type != "Point" or not g.is_valid
                        for g in geoms])
    _record(results, "geometry parses as valid POINT", "fail", nodes[is_bad])
    _vprint()
    return geoms


def check_edges_gtfs(edges: pd.DataFrame, nodes: pd.DataFrame, node_geoms: np.ndarray,
                      results: list) -> np.ndarray:
    _vprint("=== EDGES (route segments) ===")
    check_schema(edges, EDGE_COLS_GTFS, "edges", results)

    _record(results, "edge_id not null", "fail", edges[_col(edges, "edge_id").isna()])
    from_node_id = _col(edges, "from_node_id")
    to_node_id = _col(edges, "to_node_id")
    _record(results, "from_node_id not null", "fail", edges[from_node_id.isna()])
    _record(results, "to_node_id not null", "fail", edges[to_node_id.isna()])
    _record(results, "route_id not null", "fail", edges[_col(edges, "route_id").isna()])

    # `nodes` puede ser un subconjunto espacial de paradas que no incluya
    # todas las que atraviesa una ruta que pasa por la zona -> warning, no fail.
    node_id_to_geom = dict(zip(_col(nodes, "node_id"), node_geoms))
    node_ids = set(node_id_to_geom)
    _record(results, "from_node_id exists in nodes table", "warn",
            edges[~from_node_id.isin(node_ids)],
            note="expected if nodes.csv is a spatial subset of stops")
    _record(results, "to_node_id exists in nodes table", "warn",
            edges[~to_node_id.isin(node_ids)],
            note="expected if nodes.csv is a spatial subset of stops")

    _record(results, "no self-loop edges (from == to)", "fail",
            edges[from_node_id == to_node_id])

    geometry = _col(edges, "geometry")
    edge_geoms = _parse_geometries(geometry)
    is_bad_line = np.array([g is None or g.geom_type != "LineString" or not g.is_valid
                             for g in edge_geoms])
    _record(results, "geometry parses as valid LINESTRING", "fail", edges[is_bad_line])

    ok_geom = ~is_bad_line
    mismatch_mask = _topology_mismatch_mask(edges, edge_geoms, node_id_to_geom, ok_geom)
    _record(results, "edge geometry endpoints match node coords", "fail",
            edges[mismatch_mask], note=f"tolerance {COORD_TOL} m, only checked "
                                        "where both endpoints exist in nodes")

    trip_count = _col(edges, "trip_count")
    tts_mean = _col(edges, "tts_mean")
    tts_min = _col(edges, "tts_min")
    tts_max = _col(edges, "tts_max")
    _record(results, "trip_count is positive", "fail", edges[trip_count <= 0])
    _record(results, "tts_mean is non-negative", "fail", edges[tts_mean < 0])
    _record(results, "tts_min <= tts_mean <= tts_max", "fail",
            edges[~((tts_min <= tts_mean) & (tts_mean <= tts_max))])

    breakdown_cols = ["trip_count_peak_am", "trip_count_peak_pm", "trip_count_rest_of_day"]
    missing_breakdown = _missing(edges, breakdown_cols)
    if missing_breakdown:
        _vprint(f"[SKIP] trip_count == peak_am + peak_pm + rest_of_day breakdown — "
              f"falta(n) columna(s): {missing_breakdown}")
    else:
        parts = edges[breakdown_cols].fillna(0)
        calc = parts.sum(axis=1)
        _record(results, "trip_count == peak_am + peak_pm + rest_of_day breakdown",
                "fail", edges[calc != trip_count])

    hourly_trip_counts = _col(edges, "hourly_trip_counts")
    hourly_sum = hourly_trip_counts.apply(
        lambda s: sum(_parse_hourly_string(s).values()))
    _record(results, "trip_count == sum(hourly_trip_counts)", "fail",
            edges[hourly_sum != trip_count])

    hourly_travel_times = _col(edges, "hourly_travel_times")

    def bad_hourly_pair(i):
        tt = _parse_hourly_string(hourly_travel_times.iloc[i])
        tc = _parse_hourly_string(hourly_trip_counts.iloc[i])
        return (not tt) or (not tc) or (set(tt) != set(tc))
    bad_hourly_mask = pd.Series([bad_hourly_pair(i) for i in range(len(edges))], index=edges.index)
    _record(results, "hourly_travel_times / hourly_trip_counts parse & align",
            "fail", edges[bad_hourly_mask])

    def bad_days(val):
        if pd.isna(val):
            return True
        return not set(str(val).split("|")).issubset(VALID_DAYS)
    _record(results, "days_of_week_summary uses valid day names", "fail",
            edges[_col(edges, "days_of_week_summary").apply(bad_days)])

    _vprint()
    return edge_geoms


def check_route_consistency_gtfs(edges: pd.DataFrame, results: list):
    _vprint("=== ROUTE-LEVEL CONSISTENCY ===")
    needed = _missing(edges, ["route_id", "route_short_name", "route_long_name"])
    if needed:
        _vprint(f"[SKIP] route-level consistency — falta(n) columna(s): {needed}")
        _vprint()
        return

    grp = edges.groupby("route_id")[["route_short_name", "route_long_name"]].nunique()
    inconsistent_routes = grp[(grp["route_short_name"] > 1) | (grp["route_long_name"] > 1)]
    _vprint(f"[INFO] route_id values with inconsistent short/long name across rows: "
          f"{len(inconsistent_routes)}")
    if len(inconsistent_routes):
        _vprint(inconsistent_routes.to_string())
    _vprint()

    seg_counts = edges.groupby("route_id").size()
    _vprint(f"[INFO] route_id count: {edges['route_id'].nunique()}; "
          f"segments per route — min {seg_counts.min()}, "
          f"median {int(seg_counts.median())}, max {seg_counts.max()}")
    _vprint()


def run_all_checks_gtfs(nodes: Union[str, Path, pd.DataFrame],
                         edges: Union[str, Path, pd.DataFrame],
                         nodes_layer: Optional[str] = None,
                         edges_layer: Optional[str] = None,
                         verbose: bool = True):
    """
    `nodes`/`edges` pueden ser DataFrames ya cargados, o rutas a archivos
    .csv/.tsv/.geojson/.shp/.gpkg — se cargan automaticamente con
    `load_table`. `nodes_layer`/`edges_layer` solo aplican a GeoPackage.

    `verbose=False` silencia la linea-por-linea de cada check (el resumen
    final se imprime siempre); usa `results_to_frame()`/`style_results()`
    sobre el valor devuelto para revisar los resultados en tabla.
    """
    nodes = _ensure_table(nodes, layer=nodes_layer)
    edges = _ensure_table(edges, layer=edges_layer)

    with _verbosity(verbose):
        results: list[CheckResult] = []
        node_geoms = check_nodes_gtfs(nodes, results)
        check_edges_gtfs(edges, nodes, node_geoms, results)
        check_route_consistency_gtfs(edges, results)
    _summarize(results)
    return results


# ==========================================================================
# Presentacion tabular de resultados (mas facil de escanear que el log
# linea-por-linea, sobre todo con muchos modos/formatos a la vez)
# ==========================================================================
def results_to_frame(results: list[CheckResult]) -> pd.DataFrame:
    """
    Convierte una lista de CheckResult en una tabla compacta: una fila por
    check, sin los DataFrames de detalle (eso se consulta aparte con
    `show_issues`). Pensada para mostrarse directamente en un notebook.
    """
    return pd.DataFrame([
        {"check": r.name, "level": r.level, "n_issues": r.n_issues, "note": r.note}
        for r in results
    ])


def style_results(df: pd.DataFrame) -> "pd.io.formats.style.Styler":
    """
    Colorea una tabla de `results_to_frame`: rojo = fail con problemas,
    ambar = warn con problemas, verde = todo OK. Pensado para notebooks
    (se muestra solo, sin necesidad de `print`).
    """
    def _row_color(row):
        if row["n_issues"] == 0:
            color = "background-color: #d9f2d9"   # verde suave
        elif row["level"] == "fail":
            color = "background-color: #f8d7da"   # rojo suave
        else:
            color = "background-color: #fff3cd"   # ambar suave
        return [color] * len(row)

    return (
        df.style
        .apply(_row_color, axis=1)
        .format({"n_issues": "{:d}"})
    )


def summary_matrix(results_by_key: dict) -> pd.DataFrame:
    """
    Junta los resultados de varias corridas (p. ej. una por modo, o por
    modo+formato) en una sola matriz check x clave, con el numero de
    problemas en cada celda. Pensada para ver TODO de un vistazo antes de
    entrar al detalle de una celda en particular.

    results_by_key: dict como {"driving": [CheckResult, ...], "walking": [...]}
                     (las claves pueden ser tuplas, p. ej. ("geojson", "bus"))
    """
    frames = []
    for key, results in results_by_key.items():
        s = pd.Series({r.name: r.n_issues for r in results}, name=key)
        frames.append(s)
    return pd.concat(frames, axis=1)


def style_summary_matrix(matrix: pd.DataFrame) -> "pd.io.formats.style.Styler":
    """Colorea `summary_matrix`: 0 = verde, >0 = rojo, mas oscuro cuantos mas casos."""
    def _color(val):
        if pd.isna(val):
            return "background-color: #eeeeee"  # check no aplicable / no corrido
        if val == 0:
            return "background-color: #d9f2d9"
        return "background-color: #f8d7da"
    styler = matrix.style
    # pandas >= 2.1 renombro Styler.applymap a Styler.map; soportamos ambas.
    map_fn = styler.map if hasattr(styler, "map") else styler.applymap
    return map_fn(_color).format("{:.0f}", na_rep="—")


def show_issues(results: list[CheckResult], check_name: str, n: int = 10) -> Optional[pd.DataFrame]:
    """
    Muestra las primeras `n` filas problematicas de un check concreto por
    nombre (tal como aparece en la columna `check` de `results_to_frame`).
    Devuelve None si el check no tuvo problemas o no se encontro.
    """
    for r in results:
        if r.name == check_name:
            if r.issues is None:
                print(f"'{check_name}' no tiene filas problematicas (OK).")
                return None
            issues = r.issues
            return issues.head(n) if hasattr(issues, "head") else issues[:n]
    print(f"No se encontro un check llamado '{check_name}'.")
    return None
