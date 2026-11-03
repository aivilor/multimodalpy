"""Reading the files :func:`multimodalpy.download_network` writes.

- :func:`read_matrix` reads a ``matrix.npz`` (``formats="matrix"``).
- :func:`read_graph` reads a ``graph.json`` (``formats="networkx"``).

The layers in GeoJSON, Shapefile or GeoPackage are read with
``geopandas.read_file``. Every column is described in ``DATA_SCHEMA.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    import networkx as nx
    import pandas as pd
    from scipy.sparse import csr_array

from ._writers import GRAPH_JSON_EDGES_KEY

__all__ = ["MatrixData", "read_graph", "read_matrix"]


class MatrixData(NamedTuple):
    """What :func:`read_matrix` returns: ``(matrix, nodes, meta, weight)``."""

    #: Directed adjacency matrix (``scipy.sparse.csr_array``) of the chosen
    #: weight: row = origin node, column = destination node. A missing entry
    #: means there is no edge; ``inf`` means the edge exists but cannot be
    #: travelled with this weight (e.g. a GTFS period with no service).
    matrix: csr_array
    #: One row per matrix row, in order: ``layer``, ``node_id``, ``x``, ``y``
    #: (in ``meta["crs"]``) and ``in_area``.
    nodes: pd.DataFrame
    #: File metadata: ``crs``, ``mode``, ``dataset``, ``area``, ``weights``
    #: (names and units), ``default_weight``, ...
    meta: dict[str, Any]
    #: Name of the weight in ``matrix``.
    weight: str


def read_matrix(path: str | Path, weight: str | None = None) -> MatrixData:
    """Read a ``matrix.npz`` written with ``formats="matrix"``.

    Parameters
    ----------
    path : str | Path
        The ``matrix.npz`` file, or the folder that holds it (e.g.
        ``"output/walking"`` or ``"output/bus/<dataset>"``).
    weight : str, optional
        Weight to load: ``"tts"`` or ``"length"`` for walking / bike /
        driving, or one of the GTFS travel-time columns (``"tts_m"``,
        ``"tm_pam_wd"``, ...) for bus / train. Defaults to the file's
        default weight (``tts`` for OSM, ``tts_m`` for GTFS). The weights
        in a file are listed in ``meta["weights"]``.

    Returns
    -------
    MatrixData
        The matrix of that weight, the node table and the metadata.

    Raises
    ------
    FileNotFoundError
        If there is no ``matrix.npz`` at ``path``.
    ValueError
        If ``weight`` is not in the file, the file is not a multimodalpy
        matrix, or it was written by a newer multimodalpy.

    See Also
    --------
    multimodalpy.download_network : writes the files.

    Notes
    -----
    The file layout is described in the "Matrix" section of
    ``DATA_SCHEMA.md``. It can also be opened without multimodalpy:
    ``scipy.sparse.load_npz`` returns the default weight, and
    ``numpy.load`` gives every array.

    Examples
    --------
    >>> from scipy.sparse.csgraph import dijkstra  # doctest: +SKIP
    >>> data = read_matrix("output/walking")  # doctest: +SKIP
    >>> origin = data.nodes.index[data.nodes["node_id"] == 1234][0]  # doctest: +SKIP
    >>> seconds = dijkstra(data.matrix, indices=origin, limit=600)  # doctest: +SKIP
    """
    # Imported here so ``import multimodalpy`` does not load numpy / pandas.
    from . import _matrix

    file_path = Path(path)
    if file_path.is_dir():
        file_path = file_path / "matrix.npz"
    if not file_path.is_file():
        raise FileNotFoundError(f"No matrix file at {file_path}.")

    network = _matrix.read_network_matrix(file_path)
    name = network.default_weight if weight is None else weight
    if name not in network.weights:
        raise ValueError(
            f"Weight '{name}' is not in {file_path}. "
            f"Available: {', '.join(sorted(network.weights))}"
        )
    return MatrixData(
        matrix=network.to_scipy(name),
        nodes=network.nodes,
        meta=network.meta,
        weight=name,
    )


def read_graph(path: str | Path, *, parse_geometry: bool = True) -> nx.MultiDiGraph:
    """Read a ``graph.json`` written with ``formats="networkx"``.

    Parameters
    ----------
    path : str | Path
        The ``graph.json`` file.
    parse_geometry : bool
        Turn each edge's ``geometry`` WKT text into a shapely geometry.
        Defaults to True.

    Returns
    -------
    networkx.MultiDiGraph
        The network. The CRS of the node ``x`` / ``y`` and of the edge
        geometries is in ``graph.graph["crs"]``.

    Notes
    -----
    Accepts files with the edge list under ``"edges"`` (current) or
    ``"links"`` (written with networkx < 3.6 before the key was fixed).

    Examples
    --------
    >>> G = read_graph("output/walking/graph.json")  # doctest: +SKIP
    >>> G.graph["crs"]  # doctest: +SKIP
    'EPSG:4326'
    """
    from networkx.readwrite import json_graph

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    edges_key = GRAPH_JSON_EDGES_KEY if GRAPH_JSON_EDGES_KEY in data else "links"
    graph = json_graph.node_link_graph(data, edges=edges_key)

    if parse_geometry:
        from shapely import wkt

        for _u, _v, attrs in graph.edges(data=True):
            geom = attrs.get("geometry")
            if isinstance(geom, str):
                attrs["geometry"] = wkt.loads(geom)
    return graph
