"""Estandarizacion de datos OSM (principalmente creacion de intersecciones).

Este modulo convierte un grafo de ``osmnx`` en capas normalizadas de nodos y
aristas listas para GeoPandas / QGIS. Su parte mas importante es la creacion
explicita de nodos topologicos (intersecciones, extremos y vertices lineales) a
partir de la geometria de las aristas, de forma que la red quede completa aunque
``osmnx`` simplifique el grafo de enrutamiento.

Funciones publicas principales:

- ``normalize_osm_graph(graph, ...)``              -> ``(nodes_gdf, edges_gdf)``
- ``derive_topology_nodes_from_edges(...)``        -> capa de nodos topologicos
- ``split_edges_with_topology_nodes(...)``         -> aristas cortadas por nodo real
- ``add_mode_travel_time(edges, nodes, mode=...)`` -> tiempo de viaje (``tts``)
- ``build_final_osm_layers(graph, ...)``           -> capas finales listas para exportar
- ``add_edge_travel_time(graph, speed_kmh)``       -> tiempo de viaje simple (legacy)

No se escribe ningun fichero aqui; eso lo hace ``get_network``.
"""

from __future__ import annotations

import json
import re
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


def _flatten_to_string(value: object, sep: str = ";") -> object:
    """Convierte columnas con listas (``highway``, ``lanes``, ``maxspeed``, ``name``)
    en una cadena simple, uniendo los valores con ``sep``.

    OSMnx guarda una lista en estos campos cuando una arista simplificada
    proviene de varias 'ways' de OSM con valores distintos para ese atributo
    (p. ej. ``highway=['residential', 'tertiary']``). Tras exportar a fichero
    (GeoJSON/Shapefile/GeoPackage) las listas no son un tipo valido de columna,
    asi que aqui se aplanan a texto.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value if v is not None]
        return sep.join(parts) if parts else None
    return value


def _first_if_list(value: object) -> object:
    """Se queda con el primer elemento si el valor es una lista (p. ej. ``oneway``/``reversed``)."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _primary_highway(value: object) -> object:
    """Devuelve una unica etiqueta ``highway`` valida de OSM.

    Cuando osmnx simplifica el grafo y fusiona varias 'ways' con etiquetas
    distintas en una sola arista, guarda una lista que ``_flatten_to_string``
    une con ';' (p. ej. ``"footway;steps"``). Ese valor no existe en el
    vocabulario de OSM, asi que aqui se resuelve quedandose con la primera
    etiqueta, que si es real. El valor completo se conserva aparte en
    ``hwy_raw``, de modo que no se pierde informacion.

    Un valor simple se devuelve tal cual. Esto lo diferencia de
    :func:`_normalize_highway`, que agrupa por velocidad y colapsa cualquier
    via transitable a pie a ``"footway"``: eso sirve para elegir una velocidad
    libre, pero no para etiquetar, porque convertiria ``track`` o ``path`` en
    ``footway`` y se perderia el tipo de via real.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ";" not in text:
        return text
    for token in text.split(";"):
        token = token.strip()
        if token:
            return token
    return None


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


# ---------------------------------------------------------------------------
# Division de aristas por nodos topologicos reales
# ---------------------------------------------------------------------------
def split_edges_with_topology_nodes(
    edges: "gpd.GeoDataFrame",
    topology_nodes: "gpd.GeoDataFrame",
    *,
    round_digits: int = TOPOLOGY_ROUND_DIGITS,
    split_roles: tuple[str, ...] = ("intersection", "through_endpoint", "linear_vertex"),
) -> "gpd.GeoDataFrame":
    """Divide cada arista en tramos entre nodos topologicos "reales".

    Un grafo simplificado de OSMnx solo conserva como nodos los extremos
    (``u``/``v``) de cada arista. Si dentro de esa geometria hay un punto en el
    que en realidad cruza o termina otra calle (p. ej. una calle sin salida que
    conecta a mitad de manzana), ese punto queda "escondido" como un vertice mas
    de la geometria. Esta funcion usa la capa de nodos topologicos (ver
    ``derive_topology_nodes_from_edges``) para cortar la arista justo en esos
    puntos, generando tramos mas cortos con su propio ``from_node_id`` /
    ``to_node_id`` y una ``length`` repartida proporcionalmente a la longitud
    original de la arista.

    ``split_roles`` controla que tipos de nodo topologico se consideran puntos
    de corte validos. Por defecto se corta en:

    - "intersection": varias aristas convergen ahi.
    - "through_endpoint": un extremo de otra arista cae ahi, aunque el grado
      sea 2.
    - "linear_vertex": cualquier otro vertice intermedio de la geometria (un
      simple punto de forma, sin otra arista tocandolo). Se incluye para
      obtener el tramo mas fino posible por cada vertice original de la
      geometria; esto tambien acerca la particion a los limites reales entre
      las 'ways' de OSM que se fusionaron en una misma arista simplificada
      (informacion que, de otro modo, se pierde). Nota: al cortar en cada
      vertice, aristas con geometrias muy detalladas (calles curvas, etc.)
      generaran muchos mas tramos y nodos que antes.

    Para volver al comportamiento anterior (sin cortar en simples vertices de
    forma), pasar ``split_roles=("intersection", "through_endpoint")``.
    """
    import geopandas as gpd
    from shapely.geometry import LineString

    if edges.empty:
        return edges.copy()

    node_lookup: dict[tuple[float, float], dict] = {}
    if topology_nodes is not None and not topology_nodes.empty:
        for _, row in topology_nodes.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            key = _coord_key(geom.x, geom.y, round_digits)
            node_lookup[key] = row.to_dict()

    records: list[dict[str, object]] = []
    for _, edge in edges.iterrows():
        parts = _iter_line_coords(edge.geometry)
        if not parts:
            continue

        base_attrs = edge.drop(labels="geometry").to_dict()
        edge_length_m = float(base_attrs.get("length") or 0.0)

        for part in parts:
            if len(part) < 2:
                continue

            # Puntos de corte: siempre los extremos, mas cualquier vertice
            # intermedio que corresponda a un nodo topologico "real".
            split_indices = {0, len(part) - 1}
            for i in range(1, len(part) - 1):
                x, y, *_rest = part[i]
                key = _coord_key(x, y, round_digits)
                node = node_lookup.get(key)
                if node is not None and node.get("node_role") in split_roles:
                    split_indices.add(i)
            ordered_indices = sorted(split_indices)

            # Longitud "planar" de cada micro-segmento, solo para repartir la
            # longitud real (metros) de forma proporcional entre los tramos.
            seg_planar_lengths = [
                LineString([part[i], part[i + 1]]).length for i in range(len(part) - 1)
            ]
            total_planar = sum(seg_planar_lengths) or 1e-12

            for start_idx, end_idx in zip(ordered_indices[:-1], ordered_indices[1:]):
                if start_idx == end_idx:
                    continue
                sub_coords = part[start_idx : end_idx + 1]
                if len(sub_coords) < 2:
                    continue
                sub_geom = LineString(sub_coords)
                sub_planar = sum(seg_planar_lengths[start_idx:end_idx])
                fraction = sub_planar / total_planar
                sub_length_m = edge_length_m * fraction

                start_key = _coord_key(sub_coords[0][0], sub_coords[0][1], round_digits)
                end_key = _coord_key(sub_coords[-1][0], sub_coords[-1][1], round_digits)
                start_node = node_lookup.get(start_key, {})
                end_node = node_lookup.get(end_key, {})

                record = dict(base_attrs)
                record["from_node_id"] = start_node.get("node_id", base_attrs.get("from"))
                record["to_node_id"] = end_node.get("node_id", base_attrs.get("to"))
                record["length"] = sub_length_m
                record["geometry"] = sub_geom
                records.append(record)

    split_gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=edges.crs)
    return split_gdf


# ---------------------------------------------------------------------------
# Velocidades libres y penalizaciones de parada por modo
# ---------------------------------------------------------------------------
# Velocidades por defecto (km/h) segun tipo de via, usadas para "drive" cuando
# no hay ``maxspeed`` valido.
DEFAULT_HWY_SPEEDS_KMH: dict[str, float] = {
    "motorway": 100, "motorway_link": 70,
    "trunk": 80, "trunk_link": 50,
    "primary": 60, "primary_link": 40,
    "secondary": 50, "secondary_link": 40,
    "tertiary": 40, "tertiary_link": 30,
    "residential": 30, "living_street": 15,
    "unclassified": 30, "service": 20,
}

# Perfil de velocidad libre por modo. "walk" siempre usa una velocidad
# constante; "bike" tiene un techo maximo aunque el tag sugiera mas velocidad;
# "drive" usa ``maxspeed`` si es valido y si no, la tabla por tipo de via.
MODE_SPEED_PROFILES: dict[str, dict[str, object]] = {
    "drive": {
        "hwy_speeds_kmh": DEFAULT_HWY_SPEEDS_KMH,
        "fallback_speed_kmh": 30.0,
        "max_speed_kmh": None,
        "use_maxspeed_tag": True,
    },
    "bike": {
        "hwy_speeds_kmh": None,
        "fallback_speed_kmh": 15.0,
        "max_speed_kmh": 25.0,  # tope duro: la bici no puede superar 25-30 km/h
        "use_maxspeed_tag": False,
    },
    "walk": {
        "hwy_speeds_kmh": None,
        "fallback_speed_kmh": 5.0,  # velocidad constante de 5 km/h
        "max_speed_kmh": 5.0,
        "use_maxspeed_tag": False,
    },
}

# Penalizacion de parada (segundos) aplicada al llegar a un nodo, segun el tag
# ``highway`` del nodo (semaforo, stop, etc.) o, si no hay tag disponible, segun
# el rol topologico generico del nodo ("intersection"). "walk" no lleva
# penalizacion: se prioriza la velocidad constante indicada.
MODE_STOP_PENALTIES_SEC: dict[str, dict[str, float]] = {
    "drive": {
        "traffic_signals": 15.0,
        "stop": 4.0,
        "mini_roundabout": 3.0,
        "give_way": 2.0,
        "crossing": 3.0,
        "generic_intersection": 6.0,
    },
    "bike": {
        "traffic_signals": 8.0,
        "stop": 3.0,
        "mini_roundabout": 2.0,
        "give_way": 1.0,
        "crossing": 2.0,
        "generic_intersection": 3.0,
    },
    "walk": {},
}


def _parse_maxspeed_kmh(value: object) -> float | None:
    """Interpreta el tag ``maxspeed`` de OSM (num., texto, listas, "30 mph",
    valores ';'-separados como "30;50" o "20;walk")."""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        parts = value
    elif isinstance(value, (int, float)):
        return float(value)
    else:
        text = str(value).strip().lower()
        if not text or text in {"none", "signals", "variable", "walk"}:
            return None
        parts = text.split(";") if ";" in text else [text]

    numeric_values: list[float] = []
    for part in parts:
        part_text = str(part).strip().lower()
        if not part_text or part_text in {"none", "signals", "variable", "walk"}:
            continue
        match = re.search(r"(\d+(\.\d+)?)", part_text)
        if not match:
            continue
        number = float(match.group(1))
        numeric_values.append(number * 1.60934 if "mph" in part_text else number)

    if not numeric_values:
        return None
    return max(numeric_values)


# Cualquier variante de via peatonal/paso conocida en OSM se colapsa a "footway".
WALKING_HIGHWAY_ALIASES: set[str] = {
    "footway", "path", "steps", "pedestrian", "living_street",
    "track", "corridor", "elevator", "bridleway",
}


def _normalize_highway(highway_value: object) -> object:
    """Normaliza tags highway compuestos (';'-separados o listas) y colapsa
    cualquier variante peatonal conocida a 'footway'."""
    if isinstance(highway_value, (list, tuple)):
        tokens = [str(v) for v in highway_value if v is not None]
    elif isinstance(highway_value, str) and ";" in highway_value:
        tokens = highway_value.split(";")
    elif highway_value is None:
        return highway_value
    else:
        tokens = [str(highway_value)]

    tokens = [t.strip() for t in tokens if t and t.strip()]
    if any(t in WALKING_HIGHWAY_ALIASES for t in tokens):
        return "footway"
    return tokens[0] if tokens else highway_value


def _hwy_free_flow_speed_kmh(highway_value: object, profile: dict) -> float:
    hwy_speeds = profile.get("hwy_speeds_kmh")
    fallback = float(profile["fallback_speed_kmh"])
    if not hwy_speeds:
        return fallback
    highway_value = _normalize_highway(highway_value)
    return float(hwy_speeds.get(highway_value, fallback))


def add_mode_travel_time(
    edges: "gpd.GeoDataFrame",
    nodes: "gpd.GeoDataFrame",
    *,
    mode: str,
    length_col: str = "length",
    to_node_col: str = "to_node_id",
    output_col: str = "tts",
) -> "gpd.GeoDataFrame":
    """Calcula el tiempo de viaje por arista (``tts``, en segundos).

    tiempo = tiempo_libre (longitud / velocidad) + penalizacion_de_parada

    - ``walk``: velocidad constante de 5 km/h, sin penalizacion de parada.
    - ``bike``: velocidad limitada a un maximo de 25 km/h (aunque el tramo
      sugiera mas), con una penalizacion de parada reducida frente al coche.
    - ``drive``: velocidad por ``maxspeed``/tipo de via, con penalizacion de
      parada segun el tipo de nodo de llegada (semaforo, stop, etc., o una
      penalizacion generica si el nodo es una interseccion sin tag conocido).

    La penalizacion se aplica en funcion del nodo de **llegada** (``to_node_id``):
    cada vez que la ruta entra en un nodo con parada, se anade el retardo
    correspondiente.
    """
    if mode not in MODE_SPEED_PROFILES:
        valid = ", ".join(sorted(MODE_SPEED_PROFILES))
        raise ValueError(f"Modo '{mode}' no soportado. Valores validos: {valid}")

    profile = MODE_SPEED_PROFILES[mode]
    penalties = MODE_STOP_PENALTIES_SEC.get(mode, {})

    node_role_by_id: dict[object, object] = {}
    node_highway_by_id: dict[object, object] = {}
    if nodes is not None and not nodes.empty and "node_id" in nodes.columns:
        for _, row in nodes.iterrows():
            node_id = row.get("node_id")
            node_role_by_id[node_id] = row.get("node_role")
            node_highway_by_id[node_id] = row.get("highway")

    edges = edges.copy()
    travel_times: list[float] = []
    for _, edge in edges.iterrows():
        length_m = float(edge.get(length_col) or 0.0)

        if profile.get("use_maxspeed_tag"):
            speed_kmh = _parse_maxspeed_kmh(edge.get("maxspeed"))
            if not speed_kmh:
                speed_kmh = _hwy_free_flow_speed_kmh(edge.get("highway"), profile)
        else:
            speed_kmh = float(profile["fallback_speed_kmh"])

        max_speed_kmh = profile.get("max_speed_kmh")
        if max_speed_kmh is not None:
            speed_kmh = min(speed_kmh, float(max_speed_kmh))
        if not speed_kmh or speed_kmh <= 0:
            speed_kmh = float(profile["fallback_speed_kmh"])

        meters_per_sec = speed_kmh * 1000 / 3600
        free_flow_sec = length_m / meters_per_sec if meters_per_sec > 0 else 0.0

        penalty_sec = 0.0
        if penalties:
            to_node_id = edge.get(to_node_col)
            node_highway_tag = node_highway_by_id.get(to_node_id)
            node_role = node_role_by_id.get(to_node_id)
            if node_highway_tag in penalties:
                penalty_sec = penalties[node_highway_tag]
            elif node_role == "intersection":
                penalty_sec = penalties.get("generic_intersection", 0.0)

        travel_times.append(free_flow_sec + penalty_sec)

    edges[output_col] = travel_times
    return edges


def add_edge_travel_time(graph: object, travel_speed_kmh: float) -> object:
    """Anade tiempo de viaje (minutos) a las aristas usando longitud y velocidad.

    Version simple (legacy, velocidad constante para todo el grafo, sin
    penalizacion de parada). Para el calculo por modo con penalizacion de
    parada, usar ``add_mode_travel_time``.
    """
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
    clean_edges_for_export: bool = True,
) -> tuple["gpd.GeoDataFrame", "gpd.GeoDataFrame"]:
    """Normaliza un grafo de OSMnx en GeoDataFrames de nodos y aristas.

    Devuelve:
        nodes_gdf: nodos del grafo OSM (intersecciones/extremos tras la
            simplificacion de OSMnx, o nodos topologicos completos si
            ``topology_nodes=True``).
        edges_gdf: aristas del grafo OSM con sus atributos.

    ``clean_edges_for_export=False`` deja las columnas de lista de las aristas
    (``osmid``, ``highway``, ``lanes``, ``maxspeed``, ``name``, ``reversed``)
    como listas de Python en vez de convertirlas a texto JSON. Se usa
    internamente en ``build_final_osm_layers``, que necesita las listas
    "vivas" para poder explotarlas (``explode('osmid')``) y aplanarlas antes
    de exportar. Para exportar estas aristas directamente a fichero hay que
    dejar el valor por defecto (``True``) o limpiarlas despues con
    ``_clean_for_file``.
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
    edges = edges.to_crs(output_crs)
    edges = _clean_for_file(edges) if clean_edges_for_export else edges
    if topology_nodes:
        nodes = derive_topology_nodes_from_edges(nodes, edges, layer_id=layer_id)
    return nodes, edges


# ---------------------------------------------------------------------------
# Pipeline completo: grafo OSMnx -> capas finales de nodos / aristas
# ---------------------------------------------------------------------------
FINAL_EDGE_COLUMNS = [
    "osmid", "from_node_id", "to_node_id", "highway", "hwy_raw", "lanes",
    "maxspeed", "spd_raw", "name", "oneway", "reversed", "length", "tts",
    "geometry",
]
# ``from_node_id`` tiene 12 caracteres y Shapefile lo truncaba a
# ``from_node_``, con lo que la columna dejaba de casar con la tabla de nodos y
# se rompia la topologia de la red. Se acorta en los tres formatos para que la
# columna se llame igual en todos. ``to_node_id`` tiene justo 10 y se conserva.
FINAL_EDGE_RENAME = {"osmid": "edge_id", "from_node_id": "from_node"}
FINAL_NODE_COLUMNS = ["node_id", "node_role", "geometry"]


def build_final_osm_layers(
    graph: object,
    *,
    layer_id: str,
    mode: str,
    output_crs: str = SOURCE_CRS,
    round_digits: int = TOPOLOGY_ROUND_DIGITS,
) -> tuple["gpd.GeoDataFrame", "gpd.GeoDataFrame"]:
    """Pipeline completo: grafo OSMnx -> capas finales de nodos y aristas.

    Pasos:

    1. Normaliza el grafo (``normalize_osm_graph``): nodos + aristas + nodos
       topologicos derivados de la geometria.
    2. Divide cada arista por los nodos topologicos "reales"
       (``split_edges_with_topology_nodes``), generando ``from_node_id`` /
       ``to_node_id``.
    3. Explota ``osmid`` (una fila por cada osmid original fusionado en la
       arista simplificada) y convierte a texto las columnas de lista
       (``highway``, ``lanes``, ``maxspeed``, ``name``).
    4. Calcula el tiempo de viaje (``tts``, segundos) segun el modo
       (``drive``/``bike``/``walk``), con velocidad libre + penalizacion de
       parada basada en el nodo de llegada (``add_mode_travel_time``).
    5. Selecciona y renombra las columnas finales de nodos
       (``node_id``, ``node_role``, ``geometry``) y de aristas (``edge_id``,
       ``from_node_id``, ``to_node_id``, ``highway``, ``lanes``, ``maxspeed``,
       ``name``, ``oneway``, ``reversed``, ``length``, ``tts``, ``geometry``).
    """
    nodes, edges = normalize_osm_graph(
        graph, layer_id=layer_id, output_crs=output_crs, topology_nodes=True,
        clean_edges_for_export=False,
    )

    edges = split_edges_with_topology_nodes(edges, nodes, round_digits=round_digits)

    if "osmid" in edges.columns:
        edges = edges.explode("osmid", ignore_index=True)

    for col in ("highway", "lanes", "maxspeed", "name"):
        if col in edges.columns:
            edges[col] = edges[col].map(_flatten_to_string)
    for col in ("oneway", "reversed"):
        if col in edges.columns:
            edges[col] = edges[col].map(_first_if_list)

    edges = add_mode_travel_time(edges, nodes, mode=mode)

    # Tras calcular el tiempo de viaje, se dejan ``highway`` y ``maxspeed`` con
    # un unico valor utilizable y se guarda el tag original de OSM al lado.
    # Se hace despues de ``add_mode_travel_time`` para que el calculo siga
    # viendo exactamente los mismos valores que antes.
    if "highway" in edges.columns:
        edges["hwy_raw"] = edges["highway"]
        edges["highway"] = edges["highway"].map(_primary_highway)
    if "maxspeed" in edges.columns:
        edges["spd_raw"] = edges["maxspeed"]
        edges["maxspeed"] = edges["maxspeed"].map(_parse_maxspeed_kmh)

    edge_cols = [c for c in FINAL_EDGE_COLUMNS if c in edges.columns]
    edges_final = _clean_for_file(edges[edge_cols]).rename(columns=FINAL_EDGE_RENAME)

    node_cols = [c for c in FINAL_NODE_COLUMNS if c in nodes.columns]
    nodes_final = _clean_for_file(nodes[node_cols])

    return nodes_final, edges_final
