"""OSM data standardization (mainly building intersections).

This module converts an ``osmnx`` graph into normalized node and edge
layers ready for GeoPandas / QGIS. Its most important part is the
explicit creation of topology nodes (intersections, endpoints and linear
vertices) from the edges' geometry, so the network stays complete even
when ``osmnx`` simplifies the routing graph.

Main public functions:

- ``normalize_osm_graph(graph, ...)``              -> ``(nodes_gdf, edges_gdf)``
- ``derive_topology_nodes_from_edges(...)``        -> topology nodes layer
- ``split_edges_with_topology_nodes(...)``         -> edges split at real nodes
- ``add_mode_travel_time(edges, nodes, mode=...)`` -> travel time (``tts``)
- ``build_final_osm_layers(graph, ...)``           -> final layers ready to export
- ``add_edge_travel_time(graph, speed_kmh)``       -> simple travel time (legacy)

No file is written here; that's done by ``get_network``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import geopandas as gpd
    import networkx as nx


# CRS of the graphs downloaded by osmnx. The output CRS is decided in the
# main function (``main``); defaults to WGS84 (EPSG:4326), the universal CRS.
SOURCE_CRS = "EPSG:4326"
# Decimals used to decide that two vertices are the same topology node.
# The topology is always built in a projected CRS in metres (see
# ``_metric_crs``), never in the output CRS, so 3 decimals means 1 mm
# whatever ``output_crs`` the user asks for. Rounding in the output CRS
# made the result depend on it: in EPSG:4326, 3 decimals of a degree is
# ~100 m and merged separate intersections into a single node.
TOPOLOGY_ROUND_DIGITS = 3


def _json_safe(value: object) -> object:
    """Convert a value to a type serializable in GeoJSON/attributes."""
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


def _clean_for_file(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Clean non-scalar columns so the layer can be exported to a file."""
    cleaned = gdf.copy()
    for column in cleaned.columns:
        if column != cleaned.geometry.name:
            cleaned[column] = cleaned[column].map(_json_safe)
    return cleaned


def _coord_key(
    x: float, y: float, round_digits: int = TOPOLOGY_ROUND_DIGITS
) -> tuple[float, float]:
    return (round(float(x), round_digits), round(float(y), round_digits))


def _is_metric(crs) -> bool:
    """Return True if ``crs`` is projected and its axes are in metres."""
    if crs is None or not crs.is_projected:
        return False
    return all(axis.unit_name in {"metre", "meter"} for axis in crs.axis_info[:2])


def _metric_crs(*gdfs: gpd.GeoDataFrame | None):
    """Choose the projected CRS (in metres) the topology is built in.

    If the data is already in a metric projected CRS, that one is kept;
    otherwise (e.g. EPSG:4326, as osmnx returns it) the local UTM zone is
    estimated from the data. The choice depends only on the input data,
    never on the requested output CRS, so the resulting network is the
    same whatever ``output_crs`` is. Returns ``None`` if there is no
    geometry to decide from.
    """
    for gdf in gdfs:
        if gdf is None or gdf.crs is None or gdf.empty:
            continue
        if _is_metric(gdf.crs):
            return gdf.crs
        return gdf.estimate_utm_crs()
    return None


def _iter_line_coords(geometry) -> list[list[tuple[float, float]]]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [list(geometry.coords)]
    if geometry.geom_type == "MultiLineString":
        return [list(part.coords) for part in geometry.geoms]
    return []


def _flatten_to_string(value: object, sep: str = ";") -> object:
    """Flatten list-valued columns into a single string.

    Applies to ``highway``, ``lanes``, ``maxspeed`` and ``name``, joining
    their values with ``sep``.

    OSMnx stores a list in these fields when a simplified edge comes from
    several OSM 'ways' with different values for that attribute (e.g.
    ``highway=['residential', 'tertiary']``). After exporting to a file
    (GeoJSON/Shapefile/GeoPackage) lists aren't a valid column type, so
    they're flattened to text here.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value if v is not None]
        return sep.join(parts) if parts else None
    return value


def _first_if_list(value: object) -> object:
    """Keep the first element if the value is a list (e.g. ``oneway``/``reversed``)."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _primary_highway(value: object) -> object:
    """Return a single valid OSM ``highway`` label.

    When simplifying the graph, osmnx merges several 'ways' into one
    edge, and ``_flatten_to_string`` joins their labels with ';'
    (``"footway;steps"``), a value that doesn't exist in OSM. This
    resolves it by keeping the first one; the original is kept in
    ``hwy_raw``.

    Not to be confused with :func:`_normalize_highway`, which groups by
    free-flow speed and collapses any walkable way to ``"footway"``.
    That's useful for picking a speed, but not for labeling: it would
    turn ``track`` or ``path`` into ``footway``.
    """
    if value is None:
        return None
    tokens = [token.strip() for token in str(value).split(";") if token.strip()]
    return tokens[0] if tokens else None


def derive_topology_nodes_from_edges(
    nodes: gpd.GeoDataFrame,
    edges: gpd.GeoDataFrame,
    *,
    layer_id: str,
    round_digits: int = TOPOLOGY_ROUND_DIGITS,
) -> gpd.GeoDataFrame:
    """Build a complete nodes layer from the edges' vertices.

    OSMnx's simplified graphs keep the routing graph compact, so the
    geometry's intermediate vertices aren't always present in the
    exported nodes layer. This function creates one node per unique edge
    vertex and classifies it as an endpoint, linear vertex or
    intersection. If a derived node matches an original OSMnx node, its
    metadata is kept.

    Vertices are matched in a projected CRS in metres (see
    ``_metric_crs``): if the layers come in a geographic CRS they are
    projected for the matching and the result is returned in the edges'
    original CRS. ``round_digits`` is therefore in metres (3 = 1 mm).
    Each node keeps the exact coordinates of its first occurrence, not the
    rounded matching key.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    if edges.empty:
        return nodes.copy()

    output_crs = edges.crs
    work_crs = _metric_crs(edges)
    if work_crs is not None and not _is_metric(edges.crs):
        edges = edges.to_crs(work_crs)
        if not nodes.empty and nodes.crs is not None:
            nodes = nodes.to_crs(work_crs)

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
            for coord in part:
                x, y = coord[0], coord[1]
                key = _coord_key(x, y, round_digits)
                entry = stats.setdefault(
                    key,
                    {
                        # Exact coordinates of the first occurrence.
                        "x": float(x),
                        "y": float(y),
                        "incident_segment_count": 0,
                        "incident_feature_count": 0,
                        "endpoint_occurrences": 0,
                        "vertex_occurrences": 0,
                    },
                )
                entry["vertex_occurrences"] = cast(int, entry["vertex_occurrences"]) + 1
                seen_in_feature.add(key)
                if key in endpoint_keys:
                    entry["endpoint_occurrences"] = (
                        cast(int, entry["endpoint_occurrences"]) + 1
                        )
            # part[1:] is always one shorter, by design
            for start, end in zip(part, part[1:], strict=False):
                start_key = _coord_key(start[0], start[1], round_digits)
                end_key = _coord_key(end[0], end[1], round_digits)
                if start_key == end_key:
                    continue
                stats[start_key]["incident_segment_count"] = (
                    cast(int, stats[start_key]["incident_segment_count"]) + 1
                )
                stats[end_key]["incident_segment_count"] = (
                    cast(int, stats[end_key]["incident_segment_count"]) + 1
                )

        for key in seen_in_feature:
            stats[key]["incident_feature_count"] = (
                cast(int, stats[key]["incident_feature_count"]) + 1
            )

    role_counts: Counter[str] = Counter()
    records: list[dict[str, object]] = []
    geometries: list[Point] = []
    for idx, (key, entry) in enumerate(stats.items(), start=1):
        degree = cast(int, entry["incident_segment_count"])
        endpoint_occurrences = cast(int, entry["endpoint_occurrences"])
        if degree <= 0:
            node_role = "isolated_vertex"
        elif degree == 1:
            node_role = "endpoint"
        elif degree == 2:
            node_role = (
                "through_endpoint" if endpoint_occurrences > 0 else "linear_vertex"
            )
        else:
            node_role = "intersection"
        role_counts[node_role] += 1

        original = original_by_coord.get(key, {})
        original_osm_id = original.get("osm_id")
        record = {
            **original,
            "node_id": original.get("node_id")
            or original_osm_id
            or f"{layer_id}_topology_node_{idx:07d}",
            "source_osm_id": original_osm_id,
            "type": "node",
            "layer": layer_id,
            "feature_role": "node",
            "node_role": node_role,
            "node_origin": "osm_node" if original else "derived_from_edge_geometry",
            "incident_segment_count": degree,
            "incident_feature_count": cast(int, entry["incident_feature_count"]),
            "endpoint_occurrences": endpoint_occurrences,
            "vertex_occurrences": cast(int, entry["vertex_occurrences"]),
            "coordinate_round_digits": round_digits,
        }
        records.append(record)
        geometries.append(Point(cast(float, entry["x"]), cast(float, entry["y"])))


    topology_nodes = gpd.GeoDataFrame(records, geometry=geometries, crs=edges.crs)
    if output_crs is not None and topology_nodes.crs != output_crs:
        topology_nodes = topology_nodes.to_crs(output_crs)
    topology_nodes.attrs["node_role_counts"] = dict(sorted(role_counts.items()))
    return _clean_for_file(topology_nodes)


# ---------------------------------------------------------------------------
# Splitting edges at real topology nodes
# ---------------------------------------------------------------------------
def split_edges_with_topology_nodes(
    edges: gpd.GeoDataFrame,
    topology_nodes: gpd.GeoDataFrame,
    *,
    round_digits: int = TOPOLOGY_ROUND_DIGITS,
    split_roles: tuple[str, ...] = (
        "intersection",
        "through_endpoint",
        "linear_vertex",
    ),
) -> gpd.GeoDataFrame:
    """Split each edge into segments between "real" topology nodes.

    A simplified OSMnx graph only keeps the endpoints (``u``/``v``) of
    each edge as nodes. If there's a point within that geometry where
    another street actually crosses or ends (e.g. a dead-end street that
    connects mid-block), that point stays "hidden" as just another
    vertex of the geometry. This function uses the topology nodes layer
    (see ``derive_topology_nodes_from_edges``) to cut the edge exactly at
    those points, generating shorter segments with their own
    ``from_node_id`` / ``to_node_id`` and a ``length`` distributed
    proportionally to the edge's original length.

    ``split_roles`` controls which topology-node types count as valid
    cut points. By default it cuts at:

    - "intersection": several edges converge there.
    - "through_endpoint": another edge's endpoint falls there, even if
      the degree is 2.
    - "linear_vertex": any other intermediate vertex of the geometry (a
      plain shape point, with no other edge touching it). Included to
      get the finest possible segment at every original geometry
      vertex; this also brings the split closer to the real boundaries
      between the OSM 'ways' that were merged into the same simplified
      edge (information that would otherwise be lost). Note: since it
      cuts at every vertex, edges with very detailed geometries (curvy
      streets, etc.) will generate many more segments and nodes than
      before.

    To go back to the previous behavior (not cutting at plain shape
    vertices), pass ``split_roles=("intersection", "through_endpoint")``.

    As in ``derive_topology_nodes_from_edges``, vertices are matched in a
    projected CRS in metres; layers in a geographic CRS are projected for
    the matching and the result is returned in the edges' original CRS.
    """
    import geopandas as gpd
    from shapely.geometry import LineString

    if edges.empty:
        return edges.copy()

    output_crs = edges.crs
    work_crs = _metric_crs(edges)
    if work_crs is not None and not _is_metric(edges.crs):
        edges = edges.to_crs(work_crs)
    if (
        topology_nodes is not None
        and not topology_nodes.empty
        and topology_nodes.crs is not None
        and edges.crs is not None
        and topology_nodes.crs != edges.crs
    ):
        topology_nodes = topology_nodes.to_crs(edges.crs)

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

            # Cut points: always the endpoints, plus any intermediate
            # vertex that corresponds to a "real" topology node.
            split_indices = {0, len(part) - 1}
            for i in range(1, len(part) - 1):
                coord = part[i]
                x, y = coord[0], coord[1]
                key = _coord_key(x, y, round_digits)
                node = node_lookup.get(key)
                if node is not None and node.get("node_role") in split_roles:
                    split_indices.add(i)
            ordered_indices = sorted(split_indices)

            # "Planar" length of each micro-segment, only used to
            # distribute the real length (meters) proportionally across
            # the segments.
            seg_planar_lengths = [
                LineString([part[i], part[i + 1]]).length for i in range(len(part) - 1)
            ]
            total_planar = sum(seg_planar_lengths) or 1e-12
            # sliding window, lengths differ by one on purpose
            for start_idx, end_idx in zip(
                ordered_indices[:-1], ordered_indices[1:], strict=False
            ):
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
                record["from_node_id"] = start_node.get(
                    "node_id", base_attrs.get("from")
                )
                record["to_node_id"] = end_node.get("node_id", base_attrs.get("to"))
                record["length"] = sub_length_m
                record["geometry"] = sub_geom
                records.append(record)

    split_gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=edges.crs)
    if output_crs is not None and split_gdf.crs != output_crs:
        split_gdf = split_gdf.to_crs(output_crs)
    return split_gdf


# ---------------------------------------------------------------------------
# Free-flow speeds and stop penalties by mode
# ---------------------------------------------------------------------------
# Default speeds (km/h) by road type, used for "drive" when there's no
# valid maxspeed.
DEFAULT_HWY_SPEEDS_KMH: dict[str, float] = {
    "motorway": 100,
    "motorway_link": 70,
    "trunk": 80,
    "trunk_link": 50,
    "primary": 60,
    "primary_link": 40,
    "secondary": 50,
    "secondary_link": 40,
    "tertiary": 40,
    "tertiary_link": 30,
    "residential": 30,
    "living_street": 15,
    "unclassified": 30,
    "service": 20,
}

# Free-flow speed profile per mode. "walk" always uses a constant speed;
# "bike" has a hard cap even if the tag suggests a higher speed; "drive"
# uses ``maxspeed`` if valid, otherwise the per-road-type table.
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
        "max_speed_kmh": 25.0,  # hard cap: bikes can't exceed 25-30 km/h
        "use_maxspeed_tag": False,
    },
    "walk": {
        "hwy_speeds_kmh": None,
        "fallback_speed_kmh": 5.0,  # constant speed of 5 km/h
        "max_speed_kmh": 5.0,
        "use_maxspeed_tag": False,
    },
}

# Stop penalty (seconds) applied on arrival at a node, based on the
# node's ``highway`` tag (traffic signals, stop, etc.) or, if no tag is
# available, the node's generic topological role ("intersection").
# "walk" carries no penalty: the constant speed given takes priority.
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
    """Parse the OSM ``maxspeed`` tag.

    Accepts a number, text, a list, "30 mph", or ';'-separated values
    like "30;50" or "20;walk".
    """
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


# Any known pedestrian/walkway variant in OSM collapses to "footway".
WALKING_HIGHWAY_ALIASES: set[str] = {
    "footway",
    "path",
    "steps",
    "pedestrian",
    "living_street",
    "track",
    "corridor",
    "elevator",
    "bridleway",
}


def _normalize_highway(highway_value: object) -> object:
    """Normalize compound highway tags (';'-separated or lists).

    Collapses any known pedestrian variant to 'footway'.
    """
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
    edges: gpd.GeoDataFrame,
    nodes: gpd.GeoDataFrame,
    *,
    mode: str,
    length_col: str = "length",
    to_node_col: str = "to_node_id",
    output_col: str = "tts",
) -> gpd.GeoDataFrame:
    """Compute the travel time per edge (``tts``, in seconds).

    time = free_flow_time (length / speed) + stop_penalty

    - ``walk``: constant speed of 5 km/h, no stop penalty.
    - ``bike``: speed capped at a maximum of 25 km/h (even if the
      segment suggests more), with a reduced stop penalty compared to
      driving.
    - ``drive``: speed from ``maxspeed``/road type, with a stop penalty
      based on the arrival node's type (traffic signals, stop, etc., or
      a generic penalty if the node is an intersection with no known
      tag).

    The penalty is applied based on the **arrival** node
    (``to_node_id``): every time the route enters a node with a stop,
    the corresponding delay is added.
    """
    if mode not in MODE_SPEED_PROFILES:
        valid = ", ".join(sorted(MODE_SPEED_PROFILES))
        raise ValueError(f"Mode '{mode}' not supported. Valid values: {valid}")

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
            speed_kmh = cast(float, profile["fallback_speed_kmh"])

        max_speed_kmh = profile.get("max_speed_kmh")
        if max_speed_kmh is not None:
            speed_kmh = min(speed_kmh, cast(float, max_speed_kmh))
        if not speed_kmh or speed_kmh <= 0:
            speed_kmh = cast(float, profile["fallback_speed_kmh"])

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


def add_edge_travel_time(
    graph: nx.MultiDiGraph, travel_speed_kmh: float
) -> nx.MultiDiGraph:
    """Add travel time (minutes) to the edges using length and speed.

    Simple (legacy) version: constant speed for the whole graph, no stop
    penalty. For the per-mode calculation with a stop penalty, use
    ``add_mode_travel_time``.
    """
    meters_per_minute = travel_speed_kmh * 1000 / 60
    if meters_per_minute <= 0:
        raise ValueError("travel_speed_kmh must be greater than zero.")

    for _u, _v, _key, data in graph.edges(data=True, keys=True):
        length = data.get("length")
        if length is not None:
            data["time"] = float(length) / meters_per_minute
    return graph


def normalize_osm_graph(
    graph: nx.MultiDiGraph,
    *,
    layer_id: str,
    output_crs: str | None = SOURCE_CRS,
    travel_speed_kmh: float | None = None,
    topology_nodes: bool = True,
    clean_edges_for_export: bool = True,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Normalize an OSMnx graph into node and edge GeoDataFrames.

    Returns
    -------
        nodes_gdf: OSM graph nodes (intersections/endpoints after
            OSMnx's simplification, or full topology nodes if
            ``topology_nodes=True``).
        edges_gdf: OSM graph edges with their attributes.

    The topology is always derived in a projected CRS in metres chosen
    from the graph itself (see ``_metric_crs``), and only then reprojected
    to ``output_crs``, so the nodes do not depend on the output CRS.
    ``output_crs=None`` leaves both layers in that metric working CRS
    (used by ``build_final_osm_layers``, which still needs to split the
    edges before reprojecting).

    ``clean_edges_for_export=False`` leaves the edges' list-valued
    columns (``osmid``, ``highway``, ``lanes``, ``maxspeed``, ``name``,
    ``reversed``) as Python lists instead of converting them to JSON
    text. Used internally by ``build_final_osm_layers``, which needs the
    "live" lists to explode them (``explode('osmid')``) and flatten them
    before exporting. To export these edges directly to a file, either
    keep the default (``True``) or clean them afterward with
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

    work_crs = _metric_crs(edges, nodes) or edges.crs
    nodes = _clean_for_file(nodes.to_crs(work_crs))
    edges = edges.to_crs(work_crs)
    edges = _clean_for_file(edges) if clean_edges_for_export else edges
    if topology_nodes:
        nodes = derive_topology_nodes_from_edges(nodes, edges, layer_id=layer_id)
    if output_crs is not None:
        nodes = nodes.to_crs(output_crs)
        edges = edges.to_crs(output_crs)
    return nodes, edges


# ---------------------------------------------------------------------------
# Full pipeline: OSMnx graph -> final node/edge layers
# ---------------------------------------------------------------------------
FINAL_EDGE_COLUMNS = [
    "osmid",
    "from_node_id",
    "to_node_id",
    "highway",
    "hwy_raw",
    "lanes",
    "maxspeed",
    "spd_raw",
    "name",
    "oneway",
    "reversed",
    "length",
    "tts",
    "geometry",
]
# Shapefile was truncating ``from_node_id`` (12 characters) to
# ``from_node_``, so edges no longer matched the nodes table.
# ``to_node_id`` is exactly 10 characters and is kept as-is.
FINAL_EDGE_RENAME = {"osmid": "edge_id", "from_node_id": "from_node"}
FINAL_NODE_COLUMNS = ["node_id", "node_role", "geometry"]


def build_final_osm_layers(
    graph: nx.MultiDiGraph,
    *,
    layer_id: str,
    mode: str,
    output_crs: str = SOURCE_CRS,
    round_digits: int = TOPOLOGY_ROUND_DIGITS,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Full pipeline: OSMnx graph -> final node and edge layers.

    Steps:

    1. Normalize the graph (``normalize_osm_graph``): nodes + edges +
       topology nodes derived from the geometry.
    2. Split each edge at the "real" topology nodes
       (``split_edges_with_topology_nodes``), generating
       ``from_node_id`` / ``to_node_id``.
    3. Explode ``osmid`` (one row per original osmid merged into the
       simplified edge) and convert the list-valued columns to text
       (``highway``, ``lanes``, ``maxspeed``, ``name``).
    4. Compute the travel time (``tts``, seconds) based on the mode
       (``drive``/``bike``/``walk``), with free-flow speed + a stop
       penalty based on the arrival node (``add_mode_travel_time``).
    5. Select and rename the final node columns (``node_id``,
       ``node_role``, ``geometry``) and edge columns (``edge_id``,
       ``from_node_id``, ``to_node_id``, ``highway``, ``lanes``,
       ``maxspeed``, ``name``, ``oneway``, ``reversed``, ``length``,
       ``tts``, ``geometry``).

    Steps 1-2 run in a projected CRS in metres chosen from the graph
    (``_metric_crs``); both layers are reprojected to ``output_crs`` only
    at the end, so the network's topology is the same for any output CRS.
    """
    nodes, edges = normalize_osm_graph(
        graph,
        layer_id=layer_id,
        output_crs=None,  # stay in the metric working CRS until the end
        topology_nodes=True,
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

    # Done after computing travel time, so that calculation still sees
    # the original values.
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

    return nodes_final.to_crs(output_crs), edges_final.to_crs(output_crs)