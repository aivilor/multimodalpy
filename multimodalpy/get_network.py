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

    OSM  -> "caminable", "bicicleta", "coche"
    GTFS -> "bus_urbano", "bus_interurbano" (bus)  /  "metro", "cercanias" (tren)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from . import get_area, process_gtfs


# Reparto de modos de usuario -> backend de descarga.
OSM_MODES = {"caminable", "bicicleta", "coche"}
# NAP: 1 = bus, 2 = ferroviario.
GTFS_MODE_TO_NAP = {
    "bus_urbano": 1,
    "bus_interurbano": 1,
    "metro": 2,
    "cercanias": 2,
    "cercanías": 2,
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


def _gtfs_to_graph_json(stops, stop_edges, output_path: Path) -> Path:
    """Construye un grafo NetworkX de paradas + aristas parada-a-parada y lo escribe."""
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
    if stop_edges is not None and not stop_edges.empty:
        for _, row in stop_edges.iterrows():
            graph.add_edge(
                str(row.get("from_stop_id")),
                str(row.get("to_stop_id")),
                route_id=_json_safe(row.get("route_id")),
                route_short_name=_json_safe(row.get("route_short_name")),
                trip_count=_json_safe(row.get("trip_count")),
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(json_graph.node_link_data(graph), ensure_ascii=False), encoding="utf-8"
    )
    return output_path


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
    modes: Sequence[str] = ("caminable",),
    output_path: str | Path = "output",
    *,
    boundaries_path: str | Path | None = None,
    crs: str = "EPSG:4326",
    output_file_type: str = "geojson",
    # Extras opcionales (no imprescindibles para el uso basico):
    area_code: str | None = None,
    travel_speed_kmh: float | None = None,
    clean_gtfs: bool = False,
    api_key: str | None = None,
) -> dict:
    """Descarga y estandariza la red multimodal de un municipio.

    Parametros
    ----------
    area_name : str
        Nombre del municipio sobre el que se realiza la descarga.
    modes : list[str]
        Modos de transporte a descargar. Opciones: "caminable", "bicicleta",
        "coche", "bus_urbano", "bus_interurbano", "metro", "cercanias".
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
            boundary, modes=osm_modes, output_crs=crs, travel_speed_kmh=travel_speed_kmh,
        )
        for mode, out in osm_results.items():
            layers[f"osm_{mode}_nodes"] = out["nodes"]
            layers[f"osm_{mode}_edges"] = out["edges"]

    # 3) GTFS (bus / tren) via NAP.
    if nap_modes:
        gtfs_results = get_area.download_gtfs_layers(
            area_name, boundary, output_dir / "gtfs_zips",
            modes=nap_modes, output_crs=crs, api_key=api_key,
        )
        for dataset, out in gtfs_results.items():
            layers[f"gtfs_{dataset}_nodes_stops"] = out["nodes_stops"]
            layers[f"gtfs_{dataset}_edges_stop_to_stop"] = out["edges_stop_to_stop"]
            layers[f"gtfs_{dataset}_edges_shapes_reference"] = out["edges_shapes_reference"]

            # 3b) Limpieza opcional de bus + tren (snap de paradas + tramos).
            if clean_gtfs:
                try:
                    snapped, segments = process_gtfs.clean_gtfs_lines(
                        out["edges_shapes_reference"], out["nodes_stops"],
                    )
                    layers[f"gtfs_{dataset}_stops_snapped"] = snapped.to_frame("geometry").to_crs(crs)
                    layers[f"gtfs_{dataset}_segments"] = segments.to_crs(crs)
                except Exception as exc:  # noqa: BLE001
                    print(f"[aviso] Limpieza GTFS omitida para {dataset}: {exc}")

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
                out["nodes_stops"], out["edges_stop_to_stop"],
                output_dir / f"gtfs_{dataset}_graph.json",
            )
            written.append(path.name)
    else:
        written = _write_layers(layers, output_dir, output_file_type, area_slug=area_slug)

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
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest


# ---------------------------------------------------------------------------
# CLI opcional: python -m multimodalpy.get_network --area "Valencia" ...
# ---------------------------------------------------------------------------
def _cli(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Descarga la red multimodal de un municipio.")
    parser.add_argument("--area", required=True, help="Nombre del municipio.")
    parser.add_argument("--modes", nargs="+", default=["caminable"], help="Modos de transporte.")
    parser.add_argument("--output", default="output", help="Carpeta local de descarga.")
    parser.add_argument("--boundaries", help="Shapefile/GeoJSON de limites (opcional).")
    parser.add_argument("--crs", default="EPSG:4326", help="CRS de salida (por defecto WGS84).")
    parser.add_argument(
        "--output-file-type", default="geojson",
        choices=sorted(VALID_OUTPUT_TYPES), help="Formato de salida.",
    )
    parser.add_argument("--area-code", help="Codigo oficial del municipio (opcional).")
    parser.add_argument("--travel-speed-kmh", type=float, help="Velocidad constante para tiempo de arista.")
    parser.add_argument("--clean-gtfs", action="store_true", help="Aplica limpieza de bus/tren.")
    args = parser.parse_args(argv)

    main(
        area_name=args.area,
        modes=args.modes,
        output_path=args.output,
        boundaries_path=args.boundaries,
        crs=args.crs,
        output_file_type=args.output_file_type,
        area_code=args.area_code,
        travel_speed_kmh=args.travel_speed_kmh,
        clean_gtfs=args.clean_gtfs,
    )


if __name__ == "__main__":
    _cli()
