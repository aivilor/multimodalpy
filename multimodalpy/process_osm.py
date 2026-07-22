"""Estandarizacion de datos OSM (principalmente creacion de intersecciones).

Este modulo convierte un grafo de ``osmnx`` en capas normalizadas de nodos y
aristas listas para GeoPandas / QGIS. Su parte mas importante es la creacion
explicita de nodos topologicos (intersecciones, extremos y vertices lineales) a
partir de la geometria de las aristas, de forma que la red quede completa aunque
``osmnx`` simplifique el grafo de enrutamiento.

Funciones publicas principales:

- ``normalize_osm_graph(graph, ...)``            -> ``(nodes_gdf, edges_gdf)``
- ``derive_topology_nodes_from_edges(...)``      -> capa de nodos topologicos
- ``add_edge_travel_time(graph, speed_kmh)``     -> anade tiempo de viaje

No se escribe ningun fichero aqui; eso lo hace ``get_network``.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import geopandas as gpd


# CRS de trabajo. El CRS de salida se decide en la funcion principal (``main``);
# por defecto es WGS84 (EPSG:4326), el CRS universal.
SOURCE_CRS = "EPSG:4326"
TOPOLOGY_ROUND_DIGITS = 3


def _json_safe(value: object) -> object:
    """Convierte un valor a un tipo serializable en GeoJSON/atributos."""
    try:
        if value != value:  # NaN
            return None
    except Exception:
        pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _clean_for_file(gdf: "gpd.GeoDataFrame") -> "gpd.GeoDataFrame":
    """Limpia columnas no escalares para poder exportar la capa a fichero."""
    cleaned = gdf.copy()
    for column in cleaned.columns:
        if column != cleaned.geometry.name:
            cleaned[column] = cleaned[column].map(_json_safe)
    return cleaned


def _coord_key(x: float, y: float, round_digits: int = TOPOLOGY_ROUND_DIGITS) -> tuple[float, float]:
    return (round(float(x), round_digits), round(float(y), round_digits))


def _iter_line_coords(geometry) -> list[list[tuple[float, float]]]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [list(geometry.coords)]
    if geometry.geom_type == "MultiLineString":
        return [list(part.coords) for part in geometry.geoms]
    return []


def derive_topology_nodes_from_edges(
    nodes: "gpd.GeoDataFrame",
    edges: "gpd.GeoDataFrame",
    *,
    layer_id: str,
    round_digits: int = TOPOLOGY_ROUND_DIGITS,
) -> "gpd.GeoDataFrame":
    """Construye una capa de nodos completa a partir de los vertices de las aristas.

    Los grafos simplificados de OSMnx mantienen el grafo de enrutamiento compacto,
    por lo que los vertices intermedios de la geometria no siempre estan presentes
    en la capa de nodos exportada. Esta funcion crea un nodo por cada vertice unico
    de las aristas y lo clasifica como extremo, vertice lineal o interseccion. Si un
    nodo derivado coincide con un nodo original de OSMnx, se conservan sus metadatos.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    if edges.empty:
        return nodes.copy()

    original_by_coord: dict[tuple[float, float], dict] = {}
    if not nodes.empty:
        for _, row in nodes.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            key = _coord_key(geom.x, geom.y, round_digits)
            original_by_coord.setdefault(key, row.drop(labels="geometry").to_dict())

    stats: dict[tuple[float, float], dict[str, object]] = {}
    for _, edge in edges.iterrows():
        seen_in_feature: set[tuple[float, float]] = set()
        for part in _iter_line_coords(edge.geometry):
            if len(part) < 2:
                continue

            endpoint_keys = {
                _coord_key(part[0][0], part[0][1], round_digits),
                _coord_key(part[-1][0], part[-1][1], round_digits),
            }
            for x, y, *_rest in part:
                key = _coord_key(x, y, round_digits)
                entry = stats.setdefault(
                    key,
                    {
                        "x": key[0],
                        "y": key[1],
                        "incident_segment_count": 0,
                        "incident_feature_count": 0,
                        "endpoint_occurrences": 0,
                        "vertex_occurrences": 0,
                    },
                )
                entry["vertex_occurrences"] = int(entry["vertex_occurrences"]) + 1
                seen_in_feature.add(key)
                if key in endpoint_keys:
                    entry["endpoint_occurrences"] = int(entry["endpoint_occurrences"]) + 1

            for start, end in zip(part, part[1:]):
                start_key = _coord_key(start[0], start[1], round_digits)
                end_key = _coord_key(end[0], end[1], round_digits)
                if start_key == end_key:
                    continue
                stats[start_key]["incident_segment_count"] = int(stats[start_key]["incident_segment_count"]) + 1
                stats[end_key]["incident_segment_count"] = int(stats[end_key]["incident_segment_count"]) + 1

        for key in seen_in_feature:
            stats[key]["incident_feature_count"] = int(stats[key]["incident_feature_count"]) + 1

    role_counts: Counter[str] = Counter()
    records: list[dict[str, object]] = []
    geometries: list[Point] = []
    for idx, (key, entry) in enumerate(stats.items(), start=1):
        degree = int(entry["incident_segment_count"])
        endpoint_occurrences = int(entry["endpoint_occurrences"])
        if degree <= 0:
            node_role = "isolated_vertex"
        elif degree == 1:
            node_role = "endpoint"
        elif degree == 2:
            node_role = "through_endpoint" if endpoint_occurrences > 0 else "linear_vertex"
        else:
            node_role = "intersection"
        role_counts[node_role] += 1

        original = original_by_coord.get(key, {})
        original_osm_id = original.get("osm_id")
        record = {
            **original,
            "node_id": original.get("node_id") or original_osm_id or f"{layer_id}_topology_node_{idx:07d}",
            "source_osm_id": original_osm_id,
            "type": "node",
            "layer": layer_id,
            "feature_role": "node",
            "node_role": node_role,
            "node_origin": "osm_node" if original else "derived_from_edge_geometry",
            "incident_segment_count": degree,
            "incident_feature_count": int(entry["incident_feature_count"]),
            "endpoint_occurrences": endpoint_occurrences,
            "vertex_occurrences": int(entry["vertex_occurrences"]),
            "coordinate_round_digits": round_digits,
        }
        records.append(record)
        geometries.append(Point(float(entry["x"]), float(entry["y"])))

    topology_nodes = gpd.GeoDataFrame(records, geometry=geometries, crs=edges.crs)
    topology_nodes.attrs["node_role_counts"] = dict(sorted(role_counts.items()))
    return _clean_for_file(topology_nodes)


def add_edge_travel_time(graph: object, travel_speed_kmh: float) -> object:
    """Anade tiempo de viaje (minutos) a las aristas usando longitud y velocidad."""
    meters_per_minute = travel_speed_kmh * 1000 / 60
    if meters_per_minute <= 0:
        raise ValueError("travel_speed_kmh debe ser mayor que cero.")

    for _u, _v, _key, data in graph.edges(data=True, keys=True):
        length = data.get("length")
        if length is not None:
            data["time"] = float(length) / meters_per_minute
    return graph


def normalize_osm_graph(
    graph: object,
    *,
    layer_id: str,
    output_crs: str = SOURCE_CRS,
    travel_speed_kmh: float | None = None,
    topology_nodes: bool = True,
) -> tuple["gpd.GeoDataFrame", "gpd.GeoDataFrame"]:
    """Normaliza un grafo de OSMnx en GeoDataFrames de nodos y aristas.

    Devuelve:
        nodes_gdf: nodos del grafo OSM (intersecciones/extremos tras la
            simplificacion de OSMnx, o nodos topologicos completos si
            ``topology_nodes=True``).
        edges_gdf: aristas del grafo OSM con sus atributos.
    """
    import osmnx as ox

    if travel_speed_kmh is not None:
        graph = add_edge_travel_time(graph, travel_speed_kmh)

    nodes, edges = ox.graph_to_gdfs(graph, nodes=True, edges=True)

    nodes = nodes.reset_index().rename(columns={"osmid": "osm_id"})
    edges = edges.reset_index()
    if "u" in edges.columns and "v" in edges.columns:
        edges["from"] = edges["u"]
        edges["to"] = edges["v"]

    nodes["type"] = "node"
    edges["type"] = "edge"
    nodes["layer"] = layer_id
    edges["layer"] = layer_id

    nodes = _clean_for_file(nodes.to_crs(output_crs))
    edges = _clean_for_file(edges.to_crs(output_crs))
    if topology_nodes:
        nodes = derive_topology_nodes_from_edges(nodes, edges, layer_id=layer_id)
    return nodes, edges
