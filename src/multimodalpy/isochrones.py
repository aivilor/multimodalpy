"""Isochrones: the area reachable from a point within a travel time.

Works on any network multimodalpy writes (see :func:`isochrones`). The
network is always turned into the CSR matrix of the ``matrix`` format, so a
single scipy Dijkstra gives the travel times from every origin to every node.

Only walking, bike and driving networks are supported for now: a bus or train
network on its own has no access on foot, waiting times or transfers, so its
isochrones would be misleading. They will come with the multimodal network.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import geopandas as gpd
    import numpy as np

    from ._matrix import NetworkMatrix

METHODS = ("edges", "hull")

#: Cutoff unit for each weight unit: travel times are given in minutes,
#: distances in metres.
_CUTOFF_UNITS = {"s": ("min", 60.0), "m": ("m", 1.0)}


# ---------------------------------------------------------------------------
# Weights and cutoffs
# ---------------------------------------------------------------------------
def _weight_unit(matrix: NetworkMatrix, weight: str) -> str:
    """Return the unit of ``weight`` (``"s"`` or ``"m"``) from the metadata."""
    if weight not in matrix.weights:
        raise ValueError(
            f"Weight '{weight}' is not in this network. "
            f"Available: {', '.join(sorted(matrix.weights))}"
        )
    units = {
        entry["name"]: entry.get("unit") for entry in matrix.meta.get("weights", [])
    }
    unit = units.get(weight) or {"tts": "s", "length": "m"}.get(weight)
    if unit not in _CUTOFF_UNITS:
        raise ValueError(f"Weight '{weight}' has no time or distance unit.")
    return unit


def _cutoff_values(
    cutoffs: float | Sequence[float], weight_unit: str
) -> tuple[list[float], list[float], str]:
    """Validate the cutoffs and convert them to the weight's unit.

    Returns the cutoffs as given (sorted, without repeats), the same values
    in the weight's unit (seconds or metres) and the cutoff unit label.
    """
    values = [cutoffs] if isinstance(cutoffs, (int, float)) else list(cutoffs)
    if not values:
        raise ValueError("cutoffs cannot be empty.")
    try:
        numbers = sorted({float(value) for value in values})
    except (TypeError, ValueError):
        raise ValueError(f"cutoffs must be numbers, got {values!r}.") from None
    if numbers[0] <= 0:
        raise ValueError(f"cutoffs must be greater than 0, got {numbers[0]:g}.")
    label, factor = _CUTOFF_UNITS[weight_unit]
    return numbers, [value * factor for value in numbers], label


# ---------------------------------------------------------------------------
# Coordinates in metres
# ---------------------------------------------------------------------------
def _metric_transformer(matrix: NetworkMatrix) -> tuple[Any, Any]:
    """Return (to-metric transformer, metric CRS) for the network's nodes.

    A CRS already in metres is used as is; otherwise (e.g. EPSG:4326) the
    UTM zone of the network is estimated from its nodes.
    """
    import geopandas as gpd
    from pyproj import CRS, Transformer

    crs_text = matrix.meta.get("crs")
    if not crs_text:
        raise ValueError(
            "The network has no CRS, so distances cannot be measured. Networks "
            "written by multimodalpy always have one."
        )
    crs = CRS.from_user_input(crs_text)
    units = {axis.unit_name for axis in crs.axis_info}
    if crs.is_projected and units <= {"metre", "meter"}:
        metric = crs
    else:
        nodes = matrix.nodes.dropna(subset=["x", "y"])
        points = gpd.GeoSeries(gpd.points_from_xy(nodes["x"], nodes["y"]), crs=crs)
        metric = points.estimate_utm_crs()
    return Transformer.from_crs(crs, metric, always_xy=True), metric


# ---------------------------------------------------------------------------
# Origins
# ---------------------------------------------------------------------------
def _is_coordinate(value: object) -> bool:
    """Tell whether ``value`` is a coordinate: two numbers or a shapely Point."""
    if hasattr(value, "geom_type"):
        return getattr(value, "geom_type", None) == "Point"
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    )


def _xy(value: Any) -> tuple[float, float]:
    """Return the x / y of a coordinate tuple or a shapely Point."""
    if hasattr(value, "geom_type"):
        return float(value.x), float(value.y)
    return float(value[0]), float(value[1])


def _origin_list(origins: object) -> list[object]:
    """One origin or a list of them. A tuple of two numbers is a coordinate."""
    if _is_coordinate(origins) or isinstance(origins, (str, int)):
        return [origins]
    if hasattr(origins, "item") and getattr(origins, "ndim", 1) == 0:  # numpy scalar
        return [origins]
    try:
        items = list(origins)  # type: ignore[call-overload]
    except TypeError:
        return [origins]
    if not items:
        raise ValueError("origins cannot be empty.")
    return items


def _snap_origins(
    matrix: NetworkMatrix, origins: object, max_snap_distance: float
) -> np.ndarray:
    """Matrix rows of the origins.

    A ``node_id`` is used as is. A coordinate ``(x, y)`` or shapely Point, in
    the network's CRS, is snapped to the nearest node with outgoing edges;
    if that node is further than ``max_snap_distance`` metres, it is an
    error rather than a silent start from a distant point.
    """
    import numpy as np
    import pandas as pd
    from scipy.spatial import cKDTree

    items = _origin_list(origins)
    rows = np.empty(len(items), dtype=np.int64)
    ids = pd.Index(matrix.nodes["node_id"])

    coordinates = [i for i, item in enumerate(items) if _is_coordinate(item)]
    for i, item in enumerate(items):
        if i in coordinates:
            continue
        position = ids.get_indexer([item])[0]
        if position < 0:
            raise ValueError(f"Origin node_id {item!r} is not in the network.")
        rows[i] = position

    if coordinates:
        transformer, _ = _metric_transformer(matrix)
        out_degree = np.diff(matrix.indptr)
        xy = matrix.nodes[["x", "y"]].to_numpy(dtype=float)
        candidates = np.flatnonzero((out_degree > 0) & np.isfinite(xy).all(axis=1))
        if candidates.size == 0:
            raise ValueError("The network has no nodes with edges to start from.")
        node_x, node_y = transformer.transform(xy[candidates, 0], xy[candidates, 1])
        tree = cKDTree(np.column_stack([node_x, node_y]))
        for i in coordinates:
            item = items[i]
            x, y = _xy(item)
            point = transformer.transform(x, y)
            distance, nearest = tree.query(point)
            if distance > max_snap_distance:
                raise ValueError(
                    f"Origin {x, y} is {distance:.0f} m from the nearest network "
                    f"node, more than max_snap_distance={max_snap_distance:g} m. "
                    "Check that the coordinates are in the network's CRS "
                    f"({matrix.meta.get('crs')})."
                )
            rows[i] = candidates[nearest]
    return rows


# ---------------------------------------------------------------------------
# Travel costs
# ---------------------------------------------------------------------------
def _travel_costs(
    matrix: NetworkMatrix,
    weight: str,
    rows: np.ndarray,
    limit: float,
    *,
    merge_origins: bool,
) -> np.ndarray:
    """Cost from the origins to every node, ``inf`` beyond ``limit``.

    One scipy Dijkstra for all the origins. Returns one row per origin, or a
    single row (the cost from the nearest origin) with ``merge_origins``.
    """
    import numpy as np
    from scipy.sparse.csgraph import dijkstra

    costs = dijkstra(
        matrix.to_scipy(weight),
        directed=True,
        indices=rows,
        limit=limit,
        min_only=merge_origins,
    )
    return np.atleast_2d(costs)


# ---------------------------------------------------------------------------
# Polygons
# ---------------------------------------------------------------------------
def _reached_segments(
    costs: np.ndarray,
    limit: float,
    sources: np.ndarray,
    targets: np.ndarray,
    weights: np.ndarray,
    xy: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Start and end points (in metres) of the stretch of each edge reached.

    An edge whose start is reached within ``limit`` is drawn up to the point
    reached: whole if ``cost + weight <= limit``, otherwise the fraction
    ``(limit - cost) / weight`` of the way. Edges are straight between their
    nodes, which in multimodalpy follows the street geometry (every vertex
    of a street is a node).
    """
    import numpy as np

    start_cost = costs[sources]
    usable = (start_cost <= limit) & np.isfinite(weights)
    remaining = limit - start_cost[usable]
    weight = weights[usable]
    with np.errstate(divide="ignore", invalid="ignore"):
        fraction = np.where(weight > 0, remaining / weight, 1.0)
    fraction = np.clip(fraction, 0.0, 1.0)
    keep = fraction > 0
    start = xy[sources[usable][keep]]
    end = xy[targets[usable][keep]]
    return start, start + fraction[keep, None] * (end - start)


def _edges_polygon(
    reached: np.ndarray, start: np.ndarray, end: np.ndarray, buffer_distance: float
) -> Any:
    """Union of a buffer around every stretch of street reached.

    Every node reached is the end of a stretch drawn, so the nodes are only
    drawn themselves when no stretch is (an origin with nowhere to go).
    Buffering tens of thousands of short segments is the slow part, so the
    two directions of a two-way street are drawn once, consecutive stretches
    are merged into longer lines, and the round ends use few segments
    (``quad_segs=2``; the area changes by well under 1%).
    """
    import numpy as np
    import shapely

    if not len(start):
        return shapely.buffer(shapely.multipoints(reached), buffer_distance)
    # Same stretch in both directions -> one line: order the two ends of
    # each stretch (by x, then y) and key them to the millimetre.
    swap = (start[:, 0] > end[:, 0]) | (
        (start[:, 0] == end[:, 0]) & (start[:, 1] > end[:, 1])
    )
    first = np.where(swap[:, None], end, start)
    second = np.where(swap[:, None], start, end)
    key = np.round(np.hstack([first, second]), 3)
    _, unique = np.unique(key, axis=0, return_index=True)
    lines = shapely.linestrings(np.stack([start[unique], end[unique]], axis=1))
    merged = shapely.line_merge(shapely.multilinestrings(lines))
    return shapely.buffer(merged, buffer_distance, quad_segs=2)


def _hull_polygon(
    reached: np.ndarray,
    end: np.ndarray,
    concavity: float,
    buffer_distance: float,
) -> Any:
    """Concave hull of the nodes reached and the ends of the partial edges."""
    import numpy as np
    import shapely

    points = shapely.multipoints(np.concatenate([reached, end]))
    hull = shapely.concave_hull(points, ratio=concavity)
    return shapely.buffer(hull, buffer_distance) if buffer_distance > 0 else hull


def _check_network_mode(matrix: NetworkMatrix) -> None:
    from ._matrix import GTFS_MODES

    mode = matrix.meta.get("mode")
    layer = str(matrix.meta.get("layer") or "")
    if mode in GTFS_MODES or layer.split("/")[0] in GTFS_MODES:
        raise NotImplementedError(
            f"Isochrones for {mode or layer} networks are not available yet: a "
            "bus or train network alone has no walking access, waiting times or "
            "transfers. They will come with the multimodal network. Use a "
            "walking, bike or driving network."
        )


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------
def isochrones(
    network: Any,
    origins: Any,
    cutoffs: float | Sequence[float],
    *,
    mode: str | None = None,
    weight: str = "tts",
    method: str = "edges",
    merge_origins: bool = False,
    max_snap_distance: float = 250.0,
    concavity: float = 0.3,
    buffer_distance: float = 25.0,
) -> gpd.GeoDataFrame:
    """Compute the area reachable from one or more origins within each cutoff.

    Parameters
    ----------
    network : str | Path | MatrixData | networkx.MultiDiGraph
        The network to travel on, in any form multimodalpy writes or reads:

        - the output folder of :func:`multimodalpy.get_network.main` together
          with ``mode`` (e.g. ``"output"``, ``mode="walking"``);
        - a network folder (``"output/walking"``);
        - a ``matrix.npz``, ``graph.json`` or ``.gpkg`` file (the GeoPackage
          needs ``mode``);
        - the result of :func:`multimodalpy.read_matrix` or
          :func:`multimodalpy.read_graph_json`.

        In a folder, the first format found is used, in this order:
        ``matrix.npz``, ``graph.json``, GeoPackage, GeoJSON, Shapefile. Every
        format gives the same result.
    origins : node_id, (x, y), shapely Point, or a list of them
        Where to start. A ``node_id`` of the network is used as is. A
        coordinate, as a tuple of two numbers or a shapely ``Point`` in the
        network's CRS, starts from the nearest node (see
        ``max_snap_distance``). Pass a list for several origins; a tuple of
        two numbers is always read as one coordinate.
    cutoffs : float or list of float
        Travel budgets: in **minutes** with ``weight="tts"`` and in
        **metres** with ``weight="length"`` (isodistances). One polygon is
        returned per cutoff.
    mode : str, optional
        ``"walking"``, ``"bike"`` or ``"driving"``. Needed when ``network`` is
        an output folder holding several networks, or a GeoPackage.
    weight : str
        ``"tts"`` (travel time, the default) or ``"length"`` (distance).
    method : {"edges", "hull"}
        How the reachable area is drawn:

        - ``"edges"`` (default): a band of ``buffer_distance`` metres on each
          side of every stretch of street reached. It follows the street
          network and leaves out rivers, railways or blocks with no streets.
        - ``"hull"``: a concave hull around the nodes reached, widened by
          ``buffer_distance``. A continuous area, much faster to draw when
          a large part of the network is reached (e.g. long driving
          isochrones), where ``"edges"`` can take a few seconds.
    merge_origins : bool
        With several origins, ``False`` (default) gives one set of polygons
        per origin; ``True`` gives one set for all of them, reachable from
        the nearest origin.
    max_snap_distance : float
        For origins given as coordinates: the furthest, in metres, the
        nearest network node may be. Further away, a ``ValueError`` is raised
        instead of starting from a distant node (e.g. coordinates in the sea,
        outside the municipality or in another CRS). Default 250 m.
    concavity : float
        ``method="hull"`` only: from 0 (the hull hugs the points, with deep
        inlets) to 1 (a convex hull that also covers the gaps). Default 0.3.
    buffer_distance : float
        Width in metres added around the result: with ``"edges"``, on each
        side of the streets; with ``"hull"``, around the hull. Default 25 m.

    Returns
    -------
    geopandas.GeoDataFrame
        One row per origin and cutoff, in the network's CRS:

        - ``origin``: the ``node_id`` the origin started from (with
          ``merge_origins=True``, every origin's ``node_id`` joined by ``;``);
        - ``cutoff`` and ``unit`` (``"min"`` or ``"m"``);
        - ``n_nodes``: network nodes reached within the cutoff;
        - ``geometry``: the reachable area. The polygons of one origin are
          nested: each one contains those of the smaller cutoffs (up to
          floating-point rounding).

    Raises
    ------
    ValueError
        For invalid arguments, an unknown ``node_id``, or a coordinate
        further than ``max_snap_distance`` from the network.
    NotImplementedError
        For bus and train networks, which need the multimodal network.
    FileNotFoundError
        If no network is found at the given path.

    See Also
    --------
    multimodalpy.get_network.main : downloads the networks.
    multimodalpy.read_matrix : reads a ``matrix.npz``.

    Notes
    -----
    The network is turned into the CSR matrix of the ``matrix`` format and
    a single scipy Dijkstra, stopped at the largest cutoff, gives the cost
    from every origin to every node. For each cutoff, an edge whose start is
    reached is drawn up to the point reached: whole, or the fraction of it
    the remaining budget covers. Edges are straight lines between their
    nodes; since multimodalpy makes a node of every vertex of a street, this
    follows the street geometry. Distances (snapping and buffers) are
    measured in metres: in the network's CRS if it is projected in metres,
    otherwise in its UTM zone.

    Only walking, bike and driving networks are supported. A bus or train
    network alone has no walking access, waiting times or transfers, so its
    isochrones would be misleading; they will come with the multimodal
    network.

    Examples
    --------
    >>> from multimodalpy.isochrones import isochrones  # doctest: +SKIP
    >>> areas = isochrones(  # doctest: +SKIP
    ...     "output", origins=(-0.3763, 39.4699), cutoffs=[5, 10, 15],
    ...     mode="walking",
    ... )
    >>> areas.to_file("isochrones.geojson")  # doctest: +SKIP

    Several origins, with the area reachable from any of them:

    >>> isochrones(  # doctest: +SKIP
    ...     "output/bike", origins=[1234, 5678], cutoffs=10, merge_origins=True
    ... )
    """
    import geopandas as gpd
    import numpy as np

    from ._matrix import load_network_matrix

    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}, got {method!r}.")
    if not 0 <= concavity <= 1:
        raise ValueError(f"concavity must be between 0 and 1, got {concavity}.")
    if buffer_distance < 0 or (method == "edges" and buffer_distance == 0):
        raise ValueError(
            "buffer_distance must be greater than 0"
            + (" with method='edges'." if buffer_distance == 0 else ".")
        )
    if max_snap_distance <= 0:
        raise ValueError("max_snap_distance must be greater than 0.")

    matrix = load_network_matrix(network, mode=mode)
    _check_network_mode(matrix)
    unit = _weight_unit(matrix, weight)
    given, limits, label = _cutoff_values(cutoffs, unit)
    rows = _snap_origins(matrix, origins, max_snap_distance)
    costs = _travel_costs(matrix, weight, rows, limits[-1], merge_origins=merge_origins)

    transformer, metric_crs = _metric_transformer(matrix)
    node_xy = matrix.nodes[["x", "y"]].to_numpy(dtype=float)
    x, y = transformer.transform(node_xy[:, 0], node_xy[:, 1])
    xy = np.column_stack([x, y])
    sources = np.repeat(np.arange(matrix.n_nodes), np.diff(matrix.indptr))
    targets = np.asarray(matrix.indices)
    edge_weights = matrix.weights[weight]

    ids = matrix.nodes["node_id"].tolist()
    if merge_origins:
        labels: list[Any] = [";".join(str(ids[row]) for row in dict.fromkeys(rows))]
    else:
        labels = [ids[row] for row in rows]

    records = []
    for origin, origin_costs in zip(labels, costs, strict=True):
        previous = None
        for cutoff, limit in zip(given, limits, strict=True):
            reached_mask = origin_costs <= limit
            reached = xy[reached_mask]
            start, end = _reached_segments(
                origin_costs, limit, sources, targets, edge_weights, xy
            )
            if method == "edges":
                area = _edges_polygon(reached, start, end, buffer_distance)
            else:
                area = _hull_polygon(reached, end, concavity, buffer_distance)
            # The stretches reached grow with the cutoff, so "edges" polygons
            # are already nested; a concave hull is not always.
            if method == "hull" and previous is not None:
                area = area.union(previous)
            previous = area
            records.append(
                {
                    "origin": origin,
                    "cutoff": cutoff,
                    "unit": label,
                    "n_nodes": int(reached_mask.sum()),
                    "geometry": area,
                }
            )

    result = gpd.GeoDataFrame(records, geometry="geometry", crs=metric_crs)
    return result.to_crs(matrix.meta["crs"])