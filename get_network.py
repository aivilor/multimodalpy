"""Descarga de datos en local segun indicaciones de la persona usuaria.

Contiene la funcion principal de la libreria, ``main()``, que orquesta todo el
pipeline con los parametros minimos que necesita la persona usuaria:

    main(
        area_name,          # str  : municipio a descargar
        modes,              # list : modos de transporte
        output_path,        # Path : carpeta local de descarga
        boundaries_path=None,   # Path: shapefile/geojson de limites (opcional)
        crs="EPSG:4326",        # str : CRS de salida (por defecto WGS84 universal)
        output_file_type="geojson",  # geopackage | geojson | shapefile | networkx
    )

Los modos se reparten automaticamente entre OSM y GTFS:

    OSM  -> "walking", "bike", "driving"
    GTFS -> "bus_urbano", "bus_interurbano" (bus)  /  "metro", "cercanias" (tren) #Ahora solo será bus y tren en genérico y descargar todo
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from . import get_area, process_gtfs


# Reparto de modos de usuario -> backend de descarga. Se acepta cualquier
# sinonimo reconocido por ``get_area.OSM_NETWORK_TYPES`` (p. ej. "driving",
# "coche", "car", "drive" son equivalentes); la capa resultante siempre usa
# la etiqueta canonica en ingles ("walking", "bike", "driving").
OSM_MODES = set(get_area.OSM_NETWORK_TYPES)
# NAP: 1 = bus, 2 = ferroviario.
GTFS_MODE_TO_NAP = {
    "bus": 1,
    "train": 2,
}

VALID_MODES = OSM_MODES | set(GTFS_MODE_TO_NAP)
VALID_OUTPUT_TYPES = {"geopackage", "geojson", "shapefile", "networkx"}


# ---------------------------------------------------------------------------
# Exportacion a NetworkX (JSON node-link)
# ---------------------------------------------------------------------------
def _json_safe(value: object) -> object:
    try:
        if value != value:
            return None
    except Exception:
        pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _osm_graph_to_json(graph: object, output_path: Path) -> Path:
    """Escribe un grafo OSMnx como JSON node-link de NetworkX."""
    import networkx as nx
    from networkx.readwrite import json_graph

    safe = nx.MultiDiGraph()
    safe.graph.update({k: _json_safe(v) for k, v in graph.graph.items()})
    for node, attrs in graph.nodes(data=True):
        safe.add_node(node, **{k: _json_safe(v) for k, v in attrs.items()})
    for u, v, key, attrs in graph.edges(keys=True, data=True):
        safe.add_edge(u, v, key=key, **{k: _json_safe(val) for k, val in attrs.items()})

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(json_graph.node_link_data(safe), ensure_ascii=False), encoding="utf-8"
    )
    return output_path


def _gtfs_to_graph_json(stops, edges, output_path: Path) -> Path:
    """Construye un grafo NetworkX de paradas + aristas parada-a-parada y lo escribe.

    Incluye ``travel_time_seconds_mean`` y ``hour_band`` (si existen) como
    atributos de arista, ademas de los ya existentes ``route_id`` / ``trip_count``.
    """
    import networkx as nx
    from networkx.readwrite import json_graph

    graph = nx.MultiDiGraph()
    if stops is not None and not stops.empty:
        for _, row in stops.iterrows():
            node_id = row.get("stop_id") or row.get("node_id")
            geom = row.geometry
            graph.add_node(
                str(node_id),
                x=float(geom.x) if geom is not None else None,
                y=float(geom.y) if geom is not None else None,
                stop_name=_json_safe(row.get("stop_name")),
            )
    if edges is not None and not edges.empty:
        for _, row in edges.iterrows():
            graph.add_edge(
                str(row.get("from_node_id")),
                str(row.get("to_node_id")),
                route_id=_json_safe(row.get("route_id")),
                route_short_name=_json_safe(row.get("route_short_name")),
                trip_count=_json_safe(row.get("trip_count")),
                travel_time_seconds_mean=_json_safe(row.get("travel_time_seconds_mean")),
                hour_band=_json_safe(row.get("hour_band")),
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(json_graph.node_link_data(graph), ensure_ascii=False), encoding="utf-8"
    )
    return output_path


# ---------------------------------------------------------------------------
# Escritura de la tabla de horarios (opcion B, tabla plana sin geometria)
# ---------------------------------------------------------------------------
def _write_schedule_tables(
    schedule_tables: dict[str, object],
    output_dir: Path,
    output_file_type: str,
    *,
    area_slug: str,
) -> list[str]:
    """Escribe las tablas de horario (una por dataset GTFS) junto a las capas espaciales.

    - geopackage: se anaden como tablas de atributos (sin geometria) dentro del
      mismo .gpkg, via sqlite3 (un GeoPackage es una base de datos SQLite).
    - geojson / shapefile / networkx: no admiten tablas no espaciales de forma
      nativa, asi que se escriben como CSV al lado de las demas capas.
    """
    written: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    if output_file_type == "geopackage":
        import sqlite3

        gpkg_path = output_dir / f"{area_slug}.gpkg"
        connection = sqlite3.connect(gpkg_path)
        try:
            for name, schedule_df in schedule_tables.items():
                if schedule_df is None or schedule_df.empty:
                    continue
                schedule_df.to_sql(name, connection, if_exists="replace", index=False)
                written.append(f"{gpkg_path.name}::{name}")
        finally:
            connection.close()
        return written

    for name, schedule_df in schedule_tables.items():
        if schedule_df is None or schedule_df.empty:
            continue
        path = output_dir / f"{name}.csv"
        schedule_df.to_csv(path, index=False)
        written.append(path.name)
    return written


# ---------------------------------------------------------------------------
# Escritura de capas segun el formato solicitado
# ---------------------------------------------------------------------------
def _write_layers(
    layers: dict[str, object],
    output_dir: Path,
    output_file_type: str,
    *,
    area_slug: str,
) -> list[str]:
    written: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    if output_file_type == "geopackage":
        gpkg_path = output_dir / f"{area_slug}.gpkg"
        for name, gdf in layers.items():
            if gdf is None or gdf.empty:
                continue
            gdf.to_file(gpkg_path, layer=name, driver="GPKG")
            written.append(f"{gpkg_path.name}::{name}")
        return written

    for name, gdf in layers.items():
        if gdf is None or gdf.empty:
            continue
        if output_file_type == "geojson":
            path = output_dir / f"{name}.geojson"
            gdf.to_file(path, driver="GeoJSON")
        elif output_file_type == "shapefile":
            path = output_dir / f"{name}.shp"
            gdf.to_file(path, driver="ESRI Shapefile")
        else:
            raise ValueError(f"Formato no soportado en _write_layers: {output_file_type}")
        written.append(path.name)
    return written


# ---------------------------------------------------------------------------
# Clasificacion de modos
# ---------------------------------------------------------------------------
def _split_modes(modes: Sequence[str]) -> tuple[list[str], set[int]]:
    osm_modes: list[str] = []
    nap_modes: set[int] = set()
    for mode in modes:
        key = str(mode).strip().lower()
        if key in OSM_MODES:
            osm_modes.append(key)
        elif key in GTFS_MODE_TO_NAP:
            nap_modes.add(GTFS_MODE_TO_NAP[key])
        else:
            valid = ", ".join(sorted(VALID_MODES))
            raise ValueError(f"Modo desconocido '{mode}'. Modos validos: {valid}")
    return osm_modes, nap_modes


# ---------------------------------------------------------------------------
# Funcion principal
# ---------------------------------------------------------------------------
def main(
    area_name: str,
    modes: Sequence[str] = ("walking",),
    output_path: str | Path = "output",
    *,
    boundaries_path: str | Path | None = None,
    crs: str = "EPSG:4326",
    output_file_type: str = "geojson",
    # Extras opcionales (no imprescindibles para el uso basico):
    area_code: str | None = None,
    api_key: str | None = None,
    gtfs_hour_band_size: int = 1,
    gtfs_hour_range: tuple[int, int] | None = None,
    gtfs_peak_periods: dict[str, tuple[int, int]] | None = None,
    gtfs_include_schedule_table: bool = True,
) -> dict:
    """Descarga y estandariza la red multimodal de un municipio.

    Parametros
    ----------
    area_name : str
        Nombre del municipio sobre el que se realiza la descarga.
    modes : list[str]
        Modos de transporte a descargar. Opciones: "walking", "bike",
        "driving", "bus_urbano", "bus_interurbano", "metro", "cercanias".
        (tambien se aceptan sinonimos en castellano para los modos OSM, p.
        ej. "caminable", "bicicleta", "coche").
    output_path : Path
        Carpeta local de descarga.
    boundaries_path : Path, opcional
        Shapefile (.shp) o GeoJSON (.geojson) con las geometrias a seleccionar.
        Si no se indica, se usa el shapefile de recintos incluido en el paquete.
    crs : str
        CRS de salida. Por defecto "EPSG:4326" (WGS84, universal). Indicar otro
        (p. ej. "EPSG:25830") si se quiere proyectar la red.
    output_file_type : str
        Formato de descarga: "geopackage", "geojson", "shapefile" o "networkx".
    NOTA sobre las capas OSM (walking/bike/driving): el tiempo de viaje
        (``tts``, en segundos) se calcula automaticamente segun el modo
        (velocidad libre + penalizacion de parada; ver
        ``process_osm.add_mode_travel_time``), asi que ya no se acepta un
        parametro de velocidad constante para OSM.
    gtfs_hour_band_size : int, opcional
        Si se indica (p.ej. 1), las aristas GTFS se calculan por franja horaria
        de una hora (columna ``hour_band``), en vez de un unico peso agregado.
    gtfs_hour_range : (int, int), opcional
        Si se indica (p.ej. (7, 9)), solo se consideran los viajes GTFS cuya
        salida cae en esa franja horaria (util para hora punta vs valle).
    gtfs_peak_periods : dict, opcional
        Periodos punta para el desglose por columnas de la capa de aristas
        GTFS (opcion A). Por defecto: punta_manana 07-09, punta_tarde 17-20,
        resto_del_dia el resto.
    gtfs_include_schedule_table : bool
        Si es True (por defecto), ademas de las capas espaciales se escribe
        una tabla plana ``gtfs_{dataset}_schedule`` con el horario completo
        viaje-a-viaje (opcion B): tabla de atributos en el .gpkg si
        ``output_file_type="geopackage"``, o CSV en el resto de formatos.

    Devuelve
    --------
    dict
        Manifiesto con el area, los modos, la carpeta y los ficheros escritos.
    """
    if isinstance(modes, str):
        modes = [modes]
    output_file_type = output_file_type.strip().lower()
    if output_file_type not in VALID_OUTPUT_TYPES:
        raise ValueError(
            f"output_file_type '{output_file_type}' no valido. "
            f"Opciones: {', '.join(sorted(VALID_OUTPUT_TYPES))}"
        )

    osm_modes, nap_modes = _split_modes(modes)
    output_dir = Path(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    area_slug = get_area.slugify(area_name)

    # 1) Resolver el area de estudio (siempre en WGS84 para osmnx / recorte GTFS).
    boundary = get_area.find_area_boundary(
        area_name, boundaries_path, area_code=area_code, target_crs="EPSG:4326",
    )

    layers: dict[str, object] = {"study_area_boundary": boundary.to_crs(crs)}
    osm_results: dict[str, dict[str, object]] = {}
    gtfs_results: dict[str, dict[str, object]] = {}

    # 2) OSM.
    if osm_modes:
        osm_results = get_area.download_osm_layers(
            boundary, modes=osm_modes, output_crs=crs,
        )
        for mode, out in osm_results.items():
            layers[f"osm_{mode}_nodes"] = out["nodes"]
            layers[f"osm_{mode}_edges"] = out["edges"]

    # 3) GTFS (bus / tren) via NAP.
    schedule_tables: dict[str, object] = {}
    gtfs_error: str | None = None
    if nap_modes:
        # Un fallo aqui (404 del NAP, feed corrupto, caida de red) no debe
        # tirar las capas OSM que ya se han construido: se avisa, se anota en
        # el manifiesto y se escribe igualmente lo que si se pudo obtener.
        try:
            gtfs_results = get_area.download_gtfs_layers(
                area_name, boundary, output_dir / "gtfs_zips",
                modes=nap_modes, output_crs=crs, api_key=api_key,
                hour_band_size=gtfs_hour_band_size, hour_range=gtfs_hour_range,
                peak_periods=gtfs_peak_periods, include_schedule_table=gtfs_include_schedule_table,
            )
        except Exception as exc:  # noqa: BLE001 - se informa y se sigue
            gtfs_error = f"{type(exc).__name__}: {exc}"
            gtfs_results = {}
            print(
                f"[aviso] No se pudieron descargar los datos GTFS: {gtfs_error}. "
                "Se escriben solo las capas disponibles."
            )
    if gtfs_results:
        GTFS_EDGES_COLUMNS = ["edge_id",
            "from_node_id", "to_node_id", "route_id", "route_short_name", "route_long_name",
            "trip_count", "travel_time_seconds_mean", "travel_time_seconds_median",
            "travel_time_seconds_min", "travel_time_seconds_max", 
            "travel_time_seconds_mean_peak_am", "travel_time_seconds_mean_peak_pm",
            "travel_time_seconds_mean_rest_of_day",
            "travel_time_seconds_median_peak_am", "travel_time_seconds_median_peak_pm",
            "travel_time_seconds_median_rest_of_day",
            "trip_count_peak_am", "trip_count_peak_pm", "trip_count_rest_of_day",
            "travel_time_seconds_mean_weekday", "trip_count_weekday",
            "travel_time_seconds_mean_weekend", "trip_count_weekend",
            "days_of_week_summary",
            "hourly_travel_times", "hourly_trip_counts", "geometry",
        ]
        # Combinacion periodo x laborables/fin de semana (p.ej.
        # "travel_time_seconds_mean_peak_am_weekday"). Solo cubre los nombres
        # de periodo por defecto (peak_am/peak_pm/rest_of_day); si se pasa un
        # ``gtfs_peak_periods`` propio con otros nombres, sus columnas
        # combinadas no se filtran aqui de forma automatica.
        for period_name in list(process_gtfs.DEFAULT_PEAK_PERIODS.keys()) + ["rest_of_day"]:
            for day_type_name in ("weekday", "weekend"):
                GTFS_EDGES_COLUMNS.append(f"travel_time_seconds_mean_{period_name}_{day_type_name}")
                GTFS_EDGES_COLUMNS.append(f"trip_count_{period_name}_{day_type_name}")

        GTFS_NODES_COLUMNS = ["node_id", "stop_name", "geometry"]

        for dataset, out in gtfs_results.items():
            nodes_gdf = out["nodes"]
            keep_node_cols = [c for c in GTFS_NODES_COLUMNS if c in nodes_gdf.columns]
            layers[f"gtfs_{dataset}_nodes"] = nodes_gdf[keep_node_cols]

            edges_gdf = out["edges"]
            keep_cols = [c for c in GTFS_EDGES_COLUMNS if c in edges_gdf.columns]
            edges_gdf = edges_gdf[keep_cols].rename(
                columns=lambda c: c.replace("travel_time_seconds_", "tts_")
            )
            layers[f"gtfs_{dataset}_edges"] = edges_gdf

            if gtfs_include_schedule_table and out.get("schedule") is not None:
                schedule_tables[f"gtfs_{dataset}_schedule"] = out["schedule"]

    # 4) Escritura local en el formato solicitado.
    if output_file_type == "networkx":
        written: list[str] = []
        # El limite y las capas GTFS shapes se guardan como GeoJSON de apoyo.
        boundary.to_crs(crs).to_file(output_dir / "study_area_boundary.geojson", driver="GeoJSON")
        written.append("study_area_boundary.geojson")
        for mode, out in osm_results.items():
            path = _osm_graph_to_json(out["graph"], output_dir / f"osm_{mode}_graph.json")
            written.append(path.name)
        for dataset, out in gtfs_results.items():
            path = _gtfs_to_graph_json(
                out["nodes"], out["edges"],
                output_dir / f"gtfs_{dataset}_graph.json",
            )
            written.append(path.name)
        written.extend(
            _write_schedule_tables(schedule_tables, output_dir, output_file_type, area_slug=area_slug)
        )
    else:
        written = _write_layers(layers, output_dir, output_file_type, area_slug=area_slug)
        written.extend(
            _write_schedule_tables(schedule_tables, output_dir, output_file_type, area_slug=area_slug)
        )

    manifest = {
        "area_name": area_name,
        "modes": list(modes),
        "crs": crs,
        "output_file_type": output_file_type,
        "output_path": str(output_dir),
        "osm_modes": osm_modes,
        "gtfs_datasets": list(gtfs_results),
        "files": written,
    }
    if nap_modes and not gtfs_results:
        # Distinguir "no hay datos publicados" de "la descarga fallo".
        manifest["gtfs_status"] = gtfs_error or "sin conjuntos de datos publicados"
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest


# ---------------------------------------------------------------------------
# CLI opcional: python -m multimodalpy.get_network --area "Valencia" ...
# ---------------------------------------------------------------------------
def _cli(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Descarga la red multimodal de un municipio.")
    parser.add_argument("--area", required=True, help="Nombre del municipio.")
    parser.add_argument("--modes", nargs="+", default=["walking"], help="Modos de transporte.")
    parser.add_argument("--output", default="output", help="Carpeta local de descarga.")
    parser.add_argument("--boundaries", help="Shapefile/GeoJSON de limites (opcional).")
    parser.add_argument("--crs", default="EPSG:4326", help="CRS de salida (por defecto WGS84).")
    parser.add_argument(
        "--output-file-type", default="geojson",
        choices=sorted(VALID_OUTPUT_TYPES), help="Formato de salida.",
    )
    parser.add_argument("--area-code", help="Codigo oficial del municipio (opcional).")
    parser.add_argument("--clean-gtfs", action="store_true", help="Aplica limpieza de bus/tren.")
    parser.add_argument(
        "--gtfs-hour-band-size", type=int, default=1,
        help="Franja horaria (horas) para el resumen empaquetado de aristas GTFS (opcion C).",
    )
    parser.add_argument(
        "--gtfs-hour-range", type=int, nargs=2, metavar=("START", "END"),
        help="Filtra viajes GTFS por ventana horaria, p.ej. --gtfs-hour-range 7 9",
    )
    parser.add_argument(
        "--no-gtfs-schedule-table", action="store_true",
        help="No escribir la tabla de horario detallado (opcion B) para GTFS.",
    )
    args = parser.parse_args(argv)

    main(
        area_name=args.area,
        modes=args.modes,
        output_path=args.output,
        boundaries_path=args.boundaries,
        crs=args.crs,
        output_file_type=args.output_file_type,
        area_code=args.area_code,
        gtfs_hour_band_size=args.gtfs_hour_band_size,
        gtfs_hour_range=tuple(args.gtfs_hour_range) if args.gtfs_hour_range else None,
        gtfs_include_schedule_table=not args.no_gtfs_schedule_table,
    )


if __name__ == "__main__":
    _cli()