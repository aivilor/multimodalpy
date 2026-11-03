"""Shared steps of the network analyses: load, snap, cost.

Private module. Every analysis in :mod:`multimodalpy.analysis` turns the
network into the CSR matrix of the ``matrix`` format, snaps its points to
nodes and runs scipy's Dijkstra; those steps live here so they are written
once.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

    from .._matrix import NetworkMatrix

#: Cutoff unit for each weight unit: travel times are given in minutes,
#: distances in metres.
CUTOFF_UNITS = {"s": ("min", 60.0), "m": ("m", 1.0)}


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def check_network_mode(matrix: NetworkMatrix, analysis: str) -> None:
    """Reject bus and train networks until the multimodal network exists.

    ``analysis`` names the calculation in the message (``"Isochrones"``).
    """
    from .._matrix import GTFS_MODES

    mode = matrix.meta.get("mode")
    layer = str(matrix.meta.get("layer") or "")
    if mode in GTFS_MODES or layer.split("/")[0] in GTFS_MODES:
        raise NotImplementedError(
            f"{analysis} for {mode or layer} networks are not available yet: a "
            "bus or train network alone has no walking access, waiting times or "
            "transfers. They will come with the multimodal network. Use a "
            "walking, bike or driving network."
        )


# ---------------------------------------------------------------------------
# Weights and cutoffs
# ---------------------------------------------------------------------------
def weight_unit(matrix: NetworkMatrix, weight: str) -> str:
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
    if unit not in CUTOFF_UNITS:
        raise ValueError(f"Weight '{weight}' has no time or distance unit.")
    return unit


def cutoff_values(
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
    label, factor = CUTOFF_UNITS[weight_unit]
    return numbers, [value * factor for value in numbers], label


# ---------------------------------------------------------------------------
# Coordinates in metres
# ---------------------------------------------------------------------------
def metric_transformer(matrix: NetworkMatrix) -> tuple[Any, Any]:
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
# Points (origins, destinations)
# ---------------------------------------------------------------------------
def is_coordinate(value: object) -> bool:
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


def point_list(points: object, role: str = "origin") -> list[object]:
    """One point or a list of them. A tuple of two numbers is a coordinate."""
    if is_coordinate(points) or isinstance(points, (str, int)):
        return [points]
    if hasattr(points, "item") and getattr(points, "ndim", 1) == 0:  # numpy scalar
        return [points]
    try:
        items = list(points)  # type: ignore[call-overload]
    except TypeError:
        return [points]
    if not items:
        raise ValueError(f"{role}s cannot be empty.")
    return items


def snap_points(
    matrix: NetworkMatrix,
    points: object,
    max_snap_distance: float,
    *,
    role: str = "origin",
) -> np.ndarray:
    """Matrix rows of the points (origins or destinations).

    A ``node_id`` is used as is. A coordinate ``(x, y)`` or shapely Point, in
    the network's CRS, is snapped to the nearest node that can be used in
    that ``role``: with outgoing edges for an origin, with incoming edges
    for a destination. If that node is further than ``max_snap_distance``
    metres, it is an error rather than a silent start from (or arrival at)
    a distant point.
    """
    import numpy as np
    import pandas as pd
    from scipy.spatial import cKDTree

    label = role.capitalize()
    items = point_list(points, role)
    rows = np.empty(len(items), dtype=np.int64)
    ids = pd.Index(matrix.nodes["node_id"])

    coordinates = [i for i, item in enumerate(items) if is_coordinate(item)]
    for i, item in enumerate(items):
        if i in coordinates:
            continue
        position = ids.get_indexer([item])[0]
        if position < 0:
            raise ValueError(f"{label} node_id {item!r} is not in the network.")
        rows[i] = position

    if coordinates:
        transformer, _ = metric_transformer(matrix)
        if role == "destination":
            degree = np.bincount(matrix.indices, minlength=matrix.n_nodes)
        else:
            degree = np.diff(matrix.indptr)
        xy = matrix.nodes[["x", "y"]].to_numpy(dtype=float)
        candidates = np.flatnonzero((degree > 0) & np.isfinite(xy).all(axis=1))
        if candidates.size == 0:
            raise ValueError(f"The network has no nodes with edges for a {role}.")
        node_x, node_y = transformer.transform(xy[candidates, 0], xy[candidates, 1])
        tree = cKDTree(np.column_stack([node_x, node_y]))
        for i in coordinates:
            item = items[i]
            x, y = _xy(item)
            point = transformer.transform(x, y)
            distance, nearest = tree.query(point)
            if distance > max_snap_distance:
                raise ValueError(
                    f"{label} {x, y} is {distance:.0f} m from the nearest network "
                    f"node, more than max_snap_distance={max_snap_distance:g} m. "
                    "Check that the coordinates are in the network's CRS "
                    f"({matrix.meta.get('crs')})."
                )
            rows[i] = candidates[nearest]
    return rows


# ---------------------------------------------------------------------------
# Travel costs
# ---------------------------------------------------------------------------
def travel_costs(
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
