"""Isochrones: the area reachable from a point within a travel time.

Private module; the function is public as :func:`multimodalpy.analysis.isochrones`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import geopandas as gpd
    import numpy as np


from ._common import (
    check_network_mode,
    cutoff_values,
    metric_transformer,
    snap_points,
    travel_costs,
    weight_unit,
)

METHODS = ("edges", "hull")


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

        - the output folder of :func:`multimodalpy.download_network` together
          with ``mode`` (e.g. ``"output"``, ``mode="walking"``);
        - a network folder (``"output/walking"``);
        - a ``matrix.npz``, ``graph.json`` or ``.gpkg`` file (the GeoPackage
          needs ``mode``);
        - the result of :func:`multimodalpy.read_matrix` or
          :func:`multimodalpy.read_graph`.

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
    multimodalpy.download_network : downloads the networks.
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
    >>> from multimodalpy import isochrones  # doctest: +SKIP
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

    from .._matrix import load_network_matrix

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
    check_network_mode(matrix, "Isochrones")
    unit = weight_unit(matrix, weight)
    given, limits, label = cutoff_values(cutoffs, unit)
    rows = snap_points(matrix, origins, max_snap_distance)
    costs = travel_costs(matrix, weight, rows, limits[-1], merge_origins=merge_origins)

    transformer, metric_crs = metric_transformer(matrix)
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
