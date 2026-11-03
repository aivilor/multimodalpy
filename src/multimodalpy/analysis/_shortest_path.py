"""Shortest paths between every origin and every destination.

Private module; the function is public as
:func:`multimodalpy.analysis.shortest_path`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import geopandas as gpd
    import numpy as np

    from .._matrix import NetworkMatrix

from ._common import check_network_mode, cutoff_values, snap_points, weight_unit

#: Upper bound on the cells of one Dijkstra batch (origins x nodes): costs
#: (float64) and predecessors (int32) take about 12 bytes per cell, so this
#: keeps a batch under ~250 MB whatever the size of the network.
_BATCH_CELLS = 20_000_000

#: Predecessor scipy gives a node with no predecessor (the origin, or a node
#: that was not reached).
_NO_PREDECESSOR = -9999


# ---------------------------------------------------------------------------
# Edge lookup and sums along the shortest-path tree
# ---------------------------------------------------------------------------
def _edge_keys(matrix: NetworkMatrix) -> np.ndarray:
    """Sorted key ``origin * n + destination`` of every stored edge."""
    import numpy as np

    rows = np.repeat(np.arange(matrix.n_nodes, dtype=np.int64), np.diff(matrix.indptr))
    return rows * matrix.n_nodes + matrix.indices.astype(np.int64)


def _edge_values(
    keys: np.ndarray,
    values: np.ndarray,
    n_nodes: int,
    sources: np.ndarray,
    targets: np.ndarray,
) -> np.ndarray:
    """Value of the edge ``sources[i] -> targets[i]`` for each ``i``.

    The edges are in CSR order, so their keys are sorted and one binary
    search finds them all.
    """
    import numpy as np

    wanted = sources.astype(np.int64) * n_nodes + targets.astype(np.int64)
    return values[np.searchsorted(keys, wanted)]


def _sums_along_tree(parent: np.ndarray, edge_value: np.ndarray) -> np.ndarray:
    """Sum of ``edge_value`` from the root of the tree down to every node.

    ``parent[v]`` is the parent of ``v`` (``-1`` for the root) and
    ``edge_value[v]`` the value of the edge ``parent[v] -> v``. Pointer
    jumping: every round adds the sum up to the current ancestor and jumps
    twice as far up, so the loop runs log2(depth) times, all vectorised.
    """
    import numpy as np

    total = edge_value.astype(np.float64)
    ancestor = parent.copy()
    while True:
        has = ancestor >= 0
        if not has.any():
            return total
        up = ancestor[has]
        new_total = total.copy()
        new_total[has] += total[up]
        new_ancestor = ancestor.copy()
        new_ancestor[has] = ancestor[up]
        total, ancestor = new_total, new_ancestor


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------
def shortest_path(
    network: Any,
    origins: Any,
    destinations: Any,
    threshold: float | None,
    *,
    mode: str | None = None,
    weight: str = "tts",
    max_snap_distance: float = 250.0,
    geometry: bool = True,
) -> gpd.GeoDataFrame:
    """Compute the shortest path from every origin to every destination.

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
        ``matrix.npz``, ``graph.json``, GeoPackage, GeoJSON, Shapefile.
    origins, destinations : node_id, (x, y), shapely Point, or a list of them
        Where the paths start and end. A ``node_id`` of the network is used
        as is. A coordinate, as a tuple of two numbers or a shapely ``Point``
        in the network's CRS, is snapped to the nearest node that can start
        a path (an origin) or end one (a destination); see
        ``max_snap_distance``. A tuple of two numbers is always read as one
        coordinate. Every origin is paired with every destination.
    threshold : float or None
        The longest path to keep: in **minutes** with ``weight="tts"`` and
        in **metres** with ``weight="length"``. Pairs beyond it, or with no
        path at all, are left out. ``None`` keeps every pair with a path.
    mode : str, optional
        ``"walking"``, ``"bike"`` or ``"driving"``. Needed when ``network`` is
        an output folder holding several networks, or a GeoPackage.
    weight : str
        What the path minimises: ``"tts"`` (travel time, the default) or
        ``"length"`` (distance).
    max_snap_distance : float
        For points given as coordinates: the furthest, in metres, the
        nearest suitable network node may be. Further away, a ``ValueError``
        is raised instead of using a distant node. Default 250 m.
    geometry : bool
        Draw each path as a line. Default True. With False the ``geometry``
        column is empty, which is much faster for large origin-destination
        tables.

    Returns
    -------
    geopandas.GeoDataFrame
        One row per origin and destination within ``threshold``, origins in
        the order given and, for each, destinations in the order given:

        - ``origin`` and ``destination``: the ``node_id`` of each end;
        - ``time_min``: travel time along the path, in minutes;
        - ``length_m``: length of the path, in metres;
        - ``n_edges``: number of edges in the path;
        - ``geometry``: the path as a LineString in the network's CRS
          (empty when ``geometry=False``, or when origin and destination are
          the same node).

        Both ``time_min`` and ``length_m`` are given whatever ``weight`` is,
        e.g. the metres of the fastest path. A measure the network does not
        have (a :func:`multimodalpy.read_matrix` result holds one weight) is
        left empty.

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
    multimodalpy.isochrones : the area reachable within a travel time.
    multimodalpy.download_network : downloads the networks.

    Notes
    -----
    The network is turned into the CSR matrix of the ``matrix`` format.
    scipy's Dijkstra runs from the origins, stopped at ``threshold``, and
    returns the shortest-path tree; the time, length and number of edges of
    every path are summed along that tree, vectorised, and only the paths
    drawn are walked node by node. Origins are processed in batches, so
    memory stays bounded however many origins are given. Paths are straight
    lines between their nodes; since multimodalpy makes a node of every
    vertex of a street, this follows the street geometry.

    Only walking, bike and driving networks are supported. A bus or train
    network alone has no walking access, waiting times or transfers; shortest
    paths on it will come with the multimodal network.

    Examples
    --------
    >>> from multimodalpy import shortest_path  # doctest: +SKIP
    >>> paths = shortest_path(  # doctest: +SKIP
    ...     "output", origins=[(-0.3763, 39.4699)],
    ...     destinations=[(-0.3546, 39.4562), (-0.3881, 39.4789)],
    ...     threshold=30, mode="walking",
    ... )
    >>> paths[["origin", "destination", "time_min", "length_m"]]  # doctest: +SKIP

    A travel-time table between two sets of nodes, without drawing the paths:

    >>> shortest_path(  # doctest: +SKIP
    ...     "output/bike", origins=stops, destinations=schools, threshold=20,
    ...     geometry=False,
    ... )
    """
    import geopandas as gpd
    import numpy as np
    from scipy.sparse.csgraph import dijkstra

    from .._matrix import load_network_matrix

    if max_snap_distance <= 0:
        raise ValueError("max_snap_distance must be greater than 0.")

    matrix = load_network_matrix(network, mode=mode)
    check_network_mode(matrix, "Shortest paths")
    unit = weight_unit(matrix, weight)
    limit = np.inf if threshold is None else cutoff_values(threshold, unit)[1][0]
    origin_rows = snap_points(matrix, origins, max_snap_distance, role="origin")
    destination_rows = snap_points(
        matrix, destinations, max_snap_distance, role="destination"
    )

    n_nodes = matrix.n_nodes
    keys = _edge_keys(matrix)
    # Seconds for the time, metres for the length; missing ones stay NaN.
    measures = {
        name: matrix.weights.get(source)
        for name, source in (("time_min", "tts"), ("length_m", "length"))
    }
    scale = {"time_min": 1 / 60, "length_m": 1.0}
    node_xy = matrix.nodes[["x", "y"]].to_numpy(dtype=float)
    ids = matrix.nodes["node_id"].to_numpy()

    unique_origins = np.unique(origin_rows)
    per_origin: dict[int, dict[str, Any]] = {}
    batch = max(1, _BATCH_CELLS // max(n_nodes, 1))
    for start in range(0, len(unique_origins), batch):
        rows = unique_origins[start : start + batch]
        costs, predecessors = dijkstra(
            matrix.to_scipy(weight),
            directed=True,
            indices=rows,
            limit=limit,
            return_predecessors=True,
        )
        for row, cost, predecessor in zip(rows, costs, predecessors, strict=True):
            per_origin[int(row)] = _paths_from_origin(
                cost,
                predecessor,
                destination_rows,
                limit=limit,
                keys=keys,
                n_nodes=n_nodes,
                measures=measures,
                scale=scale,
                node_xy=node_xy if geometry else None,
            )

    columns: dict[str, list[Any]] = {
        "origin": [],
        "destination": [],
        "time_min": [],
        "length_m": [],
        "n_edges": [],
        "geometry": [],
    }
    for row in origin_rows:
        found = per_origin[int(row)]
        kept = found["kept"]
        columns["origin"].extend([ids[row]] * len(kept))
        columns["destination"].extend(ids[destination_rows[kept]])
        for name in ("time_min", "length_m", "n_edges", "geometry"):
            columns[name].extend(found[name])

    lines = gpd.GeoSeries(columns.pop("geometry"), crs=matrix.meta.get("crs"))
    result = gpd.GeoDataFrame(columns, geometry=lines)
    result["n_edges"] = result["n_edges"].astype("int64")
    return result


def _paths_from_origin(
    cost: np.ndarray,
    predecessor: np.ndarray,
    destination_rows: np.ndarray,
    *,
    limit: float,
    keys: np.ndarray,
    n_nodes: int,
    measures: dict[str, np.ndarray | None],
    scale: dict[str, float],
    node_xy: np.ndarray | None,
) -> dict[str, Any]:
    """Measures and lines of the paths from one origin to the destinations."""
    import numpy as np
    import shapely

    destination_cost = cost[destination_rows]
    kept = np.flatnonzero(np.isfinite(destination_cost) & (destination_cost <= limit))
    found: dict[str, Any] = {"kept": kept}
    if not len(kept):
        for name in ("time_min", "length_m", "n_edges", "geometry"):
            found[name] = []
        return found

    # The shortest-path tree, restricted to the nodes reached.
    reached = np.flatnonzero(np.isfinite(cost))
    position = np.full(n_nodes, -1, dtype=np.int64)
    position[reached] = np.arange(len(reached))
    parent_node = predecessor[reached].astype(np.int64)
    has_parent = parent_node != _NO_PREDECESSOR
    parent = np.where(has_parent, position[np.where(has_parent, parent_node, 0)], -1)

    targets = position[destination_rows[kept]]
    for name, values in measures.items():
        if values is None:
            found[name] = [np.nan] * len(kept)
            continue
        edge = np.zeros(len(reached))
        edge[has_parent] = _edge_values(
            keys, values, n_nodes, parent_node[has_parent], reached[has_parent]
        )
        found[name] = list(_sums_along_tree(parent, edge)[targets] * scale[name])
    found["n_edges"] = list(
        _sums_along_tree(parent, has_parent.astype(float))[targets].astype(np.int64)
    )

    lines: list[Any] = []
    if node_xy is None:
        lines = [None] * len(kept)
    else:
        for target in targets:
            path = []
            node = int(target)
            while node >= 0:
                path.append(reached[node])
                node = int(parent[node])
            if len(path) < 2:
                lines.append(None)
            else:
                lines.append(shapely.linestrings(node_xy[path[::-1]]))
    found["geometry"] = lines
    return found
