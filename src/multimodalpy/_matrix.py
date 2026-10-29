"""Sparse adjacency matrix (CSR) of a network, and its ``matrix.npz`` file.

Private module: not part of the public API. ``get_network`` writes and reads
the files; the format is described in the "Matrix" section of
``DATA_SCHEMA.md``.

The core knows nothing about OSM or GTFS: :func:`build_network_matrix` takes
a node table and an edge table (``from_node``, ``to_node`` and one column per
weight) and returns a :class:`NetworkMatrix`. The OSM and GTFS adapters only
turn the final layers into those two tables. The multimodal network will be
built the same way, from more edge tables (street-stop links, transfers).

Rows are origins and columns destinations; the matrix is directed. Each
weight is a vector aligned with ``indices``, so every weight shares one
structure.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    import geopandas as gpd
    from scipy.sparse import csr_array

logger = logging.getLogger(__name__)

#: Version of the ``matrix.npz`` layout. A reader rejects files with a
#: higher version instead of misreading them.
FORMAT_VERSION = 1

#: Weights of the OSM networks and their units.
OSM_WEIGHTS: dict[str, str] = {"tts": "s", "length": "m"}
OSM_DEFAULT_WEIGHT = "tts"

#: Travel-time columns of the GTFS edge layers (short names), all in seconds.
#: Trip counts are left out on purpose: they are not travel times.
GTFS_WEIGHTS: dict[str, str] = {
    name: "s"
    for name in (
        "tts_m",
        "tts_md",
        "tts_mn",
        "tts_mx",
        "tts_m_pam",
        "tts_m_ppm",
        "tts_m_rod",
        "tts_md_pam",
        "tts_md_ppm",
        "tts_md_rod",
        "tts_m_wd",
        "tts_m_we",
        "tm_pam_wd",
        "tm_pam_we",
        "tm_ppm_wd",
        "tm_ppm_we",
        "tm_rod_wd",
        "tm_rod_we",
    )
}
GTFS_DEFAULT_WEIGHT = "tts_m"

# Keys of the scipy CSR layout, so ``scipy.sparse.load_npz`` opens the file.
_SCIPY_KEYS = ("format", "shape", "indptr", "indices", "data")
_WEIGHT_PREFIX = "w_"
_NODE_KEYS = {
    "layer": "node_layer",
    "node_id": "node_id",
    "x": "node_x",
    "y": "node_y",
    "in_area": "node_in_area",
}
NODE_COLUMNS = tuple(_NODE_KEYS)


@dataclass
class NetworkMatrix:
    """A directed network as a CSR adjacency structure with named weights.

    Attributes
    ----------
    indptr : numpy.ndarray
        ``n_nodes + 1`` offsets: the edges leaving row ``i`` are at
        positions ``indptr[i]`` to ``indptr[i + 1]`` of ``indices``.
    indices : numpy.ndarray
        Destination row of each edge.
    weights : dict[str, numpy.ndarray]
        One float64 vector per weight, aligned with ``indices``. ``inf``
        means the edge cannot be travelled with that weight.
    nodes : pandas.DataFrame
        One row per matrix row, in order: ``layer``, ``node_id``, ``x``,
        ``y``, ``in_area``.
    meta : dict
        Metadata stored as JSON in the file (CRS, weights and units, ...).
    edge_type : numpy.ndarray or None
        Reserved for the multimodal network; ``None`` in single-mode files.
    """

    indptr: np.ndarray
    indices: np.ndarray
    weights: dict[str, np.ndarray]
    nodes: pd.DataFrame
    meta: dict[str, Any]
    edge_type: np.ndarray | None = None

    @property
    def n_nodes(self) -> int:
        """Number of nodes (rows and columns of the matrix)."""
        return len(self.indptr) - 1

    @property
    def n_edges(self) -> int:
        """Number of stored (origin, destination) pairs."""
        return len(self.indices)

    @property
    def default_weight(self) -> str:
        """Name of the weight used when none is given."""
        return str(self.meta["default_weight"])

    def to_scipy(self, weight: str | None = None) -> csr_array:
        """Return the matrix of one weight as a ``scipy.sparse.csr_array``.

        Explicit zero weights are kept as edges.
        """
        from scipy.sparse import csr_array

        name = self.default_weight if weight is None else weight
        if name not in self.weights:
            raise KeyError(
                f"Weight '{name}' is not in this matrix. "
                f"Available: {', '.join(sorted(self.weights))}"
            )
        return csr_array(
            (self.weights[name], self.indices, self.indptr),
            shape=(self.n_nodes, self.n_nodes),
        )


# ---------------------------------------------------------------------------
# Generic builder
# ---------------------------------------------------------------------------
def _endpoint_coords(geometries: Sequence[Any], end: int) -> tuple[list, list]:
    """Return the x / y of the first (``end=0``) or last (``-1``) vertex."""
    xs: list[float] = []
    ys: list[float] = []
    for geom in geometries:
        if geom is None or geom.is_empty:
            xs.append(np.nan)
            ys.append(np.nan)
            continue
        x, y = geom.coords[end][:2]
        xs.append(float(x))
        ys.append(float(y))
    return xs, ys


def _missing_endpoint_nodes(
    edges: pd.DataFrame, from_idx: np.ndarray, to_idx: np.ndarray
) -> pd.DataFrame:
    """Nodes referenced by the edges but missing from the node table.

    Their position is the matching end of the edge geometry. GTFS stops and
    stop-to-stop edges are clipped to the study area separately, so an edge
    crossing the boundary keeps a stop the stops layer dropped.
    """
    parts = []
    has_geometry = "geometry" in edges.columns
    for column, idx, end in (("from_node", from_idx, 0), ("to_node", to_idx, -1)):
        missing = idx < 0
        if not missing.any():
            continue
        subset = edges.loc[missing]
        if has_geometry:
            xs, ys = _endpoint_coords(list(subset["geometry"]), end)
        else:
            xs = ys = [np.nan] * len(subset)
        parts.append(
            pd.DataFrame({"node_id": subset[column].to_numpy(), "x": xs, "y": ys})
        )
    if not parts:
        return pd.DataFrame(columns=["node_id", "x", "y"])
    found = pd.concat(parts, ignore_index=True)
    # The same stop can be the end of several edges: one row per stop, in
    # order of first appearance, with the first position that is known
    # (``first()`` skips missing values).
    return found.groupby("node_id", sort=False, as_index=False).first()


def build_network_matrix(
    nodes: pd.DataFrame,
    edges: pd.DataFrame,
    *,
    weights: Mapping[str, str],
    default_weight: str,
    layer: str,
    meta: Mapping[str, Any] | None = None,
) -> NetworkMatrix:
    """Build a :class:`NetworkMatrix` from a node table and an edge table.

    Parameters
    ----------
    nodes : pandas.DataFrame
        ``node_id``, ``x``, ``y`` and, optionally, ``in_area`` (default
        ``True``). Its order is the order of the matrix rows.
    edges : pandas.DataFrame
        ``from_node``, ``to_node``, one column per weight and, optionally,
        ``geometry`` (used to place endpoints missing from ``nodes``).
    weights : mapping
        Weight column -> unit. Columns missing from ``edges`` are skipped.
    default_weight : str
        Weight returned when none is asked for.
    layer : str
        Value of the ``layer`` node column (``"walking"``, ``"bus/<dataset>"``).
    meta : mapping, optional
        Extra metadata (``crs``, ``mode``, ``area``, ...).

    Returns
    -------
    NetworkMatrix
        One entry per (origin, destination) pair, with the minimum of each
        weight taken separately over parallel edges. Self-loops are dropped
        and missing weights become ``inf``.

    Raises
    ------
    ValueError
        If ``nodes`` has repeated ``node_id`` values, or if no weight column
        is present in ``edges``.
    """
    node_table = pd.DataFrame(
        {
            "node_id": nodes["node_id"].to_numpy(),
            "x": pd.to_numeric(nodes["x"], errors="coerce").to_numpy(dtype=float),
            "y": pd.to_numeric(nodes["y"], errors="coerce").to_numpy(dtype=float),
            "in_area": (
                nodes["in_area"].to_numpy(dtype=bool)
                if "in_area" in nodes.columns
                else np.ones(len(nodes), dtype=bool)
            ),
        }
    )
    if node_table["node_id"].duplicated().any():
        repeated = node_table.loc[node_table["node_id"].duplicated(), "node_id"]
        raise ValueError(
            f"Repeated node_id in the node table: {list(repeated.unique()[:5])}"
        )

    present = {name: unit for name, unit in weights.items() if name in edges.columns}
    if not present:
        raise ValueError(
            f"None of the weight columns {list(weights)} is in the edge table."
        )
    if default_weight not in present:
        default_weight = next(iter(present))

    # 1) node_id -> row. Endpoints missing from the node table are appended.
    index = pd.Index(node_table["node_id"])
    from_idx = index.get_indexer(edges["from_node"])
    to_idx = index.get_indexer(edges["to_node"])
    extra = _missing_endpoint_nodes(edges, from_idx, to_idx)
    if not extra.empty:
        extra["in_area"] = False
        node_table = pd.concat([node_table, extra], ignore_index=True)
        index = pd.Index(node_table["node_id"])
        from_idx = index.get_indexer(edges["from_node"])
        to_idx = index.get_indexer(edges["to_node"])

    # 2) Edge table with row numbers; NaN weights cannot be travelled.
    table = pd.DataFrame({"row": from_idx, "col": to_idx})
    for name in present:
        values = pd.to_numeric(edges[name], errors="coerce").to_numpy(dtype=float)
        table[name] = np.where(np.isnan(values), np.inf, values)

    # 3) Self-loops never shorten a path.
    loops = table["row"] == table["col"]
    if loops.any():
        logger.info("Matrix %s: dropped %d self-loop edges.", layer, int(loops.sum()))
        table = table.loc[~loops]

    # 4) One entry per (origin, destination), minimum of each weight. sort=True
    #    leaves the rows ordered by origin and then destination (CSR order).
    table = table.groupby(["row", "col"], sort=True, as_index=False).min()

    n_nodes = len(node_table)
    rows = table["row"].to_numpy()
    index_dtype = (
        np.int32 if max(n_nodes, len(table)) < np.iinfo(np.int32).max else np.int64
    )
    counts = np.bincount(rows, minlength=n_nodes)
    indptr = np.concatenate([[0], np.cumsum(counts)]).astype(index_dtype)
    indices = table["col"].to_numpy().astype(index_dtype)

    node_table.insert(0, "layer", layer)
    node_table = node_table[list(NODE_COLUMNS)]

    full_meta: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "layer": layer,
        **dict(meta or {}),
        "weights": [{"name": name, "unit": unit} for name, unit in present.items()],
        "default_weight": default_weight,
        "multimodalpy_version": _package_version(),
    }
    return NetworkMatrix(
        indptr=indptr,
        indices=indices,
        weights={name: table[name].to_numpy(dtype=np.float64) for name in present},
        nodes=node_table.reset_index(drop=True),
        meta=full_meta,
    )


def _package_version() -> str:
    try:
        return version("multimodalpy")
    except PackageNotFoundError:
        return "0.0.0+unknown"


# ---------------------------------------------------------------------------
# Adapters: final layers -> node and edge tables
# ---------------------------------------------------------------------------
def _point_xy(layer: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
    xs = np.array([np.nan if g is None or g.is_empty else g.x for g in layer.geometry])
    ys = np.array([np.nan if g is None or g.is_empty else g.y for g in layer.geometry])
    return xs, ys


def _crs_text(*layers: gpd.GeoDataFrame | None) -> str | None:
    for layer in layers:
        crs = getattr(layer, "crs", None)
        if crs is not None:
            return str(crs)
    return None


def osm_network_matrix(
    nodes: gpd.GeoDataFrame,
    edges: gpd.GeoDataFrame,
    *,
    mode: str,
    meta: Mapping[str, Any] | None = None,
) -> NetworkMatrix:
    """Build the matrix of an OSM network from its final layers.

    Weights: ``tts`` (seconds, default) and ``length`` (metres). The edge
    layer is already directed: a two-way street has one row per direction.
    """
    xs, ys = _point_xy(nodes)
    node_table = pd.DataFrame(
        {"node_id": nodes["node_id"].to_numpy(), "x": xs, "y": ys}
    )
    return build_network_matrix(
        node_table,
        edges,
        weights=OSM_WEIGHTS,
        default_weight=OSM_DEFAULT_WEIGHT,
        layer=mode,
        meta={
            "mode": mode,
            "dataset": None,
            "crs": _crs_text(edges, nodes),
            **dict(meta or {}),
        },
    )


def gtfs_network_matrix(
    stops: gpd.GeoDataFrame,
    edges: gpd.GeoDataFrame,
    *,
    mode: str,
    dataset: str,
    periods: Mapping[str, Sequence[int]] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> NetworkMatrix:
    """Build the matrix of a GTFS dataset from its final layers.

    ``edges`` must use the short column names of the GTFS edge layer
    (``tts_m``, ``tm_pam_wd``, ...). Weights: the travel-time columns in
    :data:`GTFS_WEIGHTS` present in the layer, ``tts_m`` by default. Stops
    reached by an edge but outside the stops layer are appended with
    ``in_area=False``. ``periods`` (period name -> start and end hour) is
    stored in the metadata, since it gives the hours behind ``pam`` /
    ``ppm`` / ``rod``.
    """
    xs, ys = _point_xy(stops)
    node_table = pd.DataFrame(
        {"node_id": stops["node_id"].astype(str).to_numpy(), "x": xs, "y": ys}
    )
    edge_table = edges.copy()
    edge_table["from_node"] = edge_table["from_node"].astype(str)
    edge_table["to_node"] = edge_table["to_node"].astype(str)
    gtfs_periods = (
        {name: list(hours) for name, hours in periods.items()} if periods else None
    )
    return build_network_matrix(
        node_table,
        edge_table,
        weights=GTFS_WEIGHTS,
        default_weight=GTFS_DEFAULT_WEIGHT,
        layer=f"{mode}/{dataset}",
        meta={
            "mode": mode,
            "dataset": dataset,
            "crs": _crs_text(edges, stops),
            "gtfs_periods": gtfs_periods,
            **dict(meta or {}),
        },
    )


# ---------------------------------------------------------------------------
# File: matrix.npz
# ---------------------------------------------------------------------------
def _node_id_array(values: pd.Series) -> np.ndarray:
    """Integer ids stay int64; anything else is stored as text (no pickle)."""
    if pd.api.types.is_integer_dtype(values):
        return values.to_numpy(dtype=np.int64)
    return values.astype(str).to_numpy(dtype=str)


def write_matrix(matrix: NetworkMatrix, path: str | Path) -> Path:
    """Write ``matrix`` as a compressed ``.npz`` file.

    The file holds the scipy CSR keys (``data`` is the default weight), so
    ``scipy.sparse.load_npz`` opens it directly. Nothing is pickled.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    nodes = matrix.nodes
    arrays: dict[str, np.ndarray] = {
        "format": np.array("csr"),
        "shape": np.array([matrix.n_nodes, matrix.n_nodes], dtype=np.int64),
        "indptr": matrix.indptr,
        "indices": matrix.indices,
        "data": matrix.weights[matrix.default_weight],
        "meta": np.array(json.dumps(matrix.meta, ensure_ascii=False)),
        _NODE_KEYS["layer"]: nodes["layer"].astype(str).to_numpy(dtype=str),
        _NODE_KEYS["node_id"]: _node_id_array(nodes["node_id"]),
        _NODE_KEYS["x"]: nodes["x"].to_numpy(dtype=np.float64),
        _NODE_KEYS["y"]: nodes["y"].to_numpy(dtype=np.float64),
        _NODE_KEYS["in_area"]: nodes["in_area"].to_numpy(dtype=bool),
    }
    for name, values in matrix.weights.items():
        arrays[f"{_WEIGHT_PREFIX}{name}"] = values
    if matrix.edge_type is not None:
        arrays["edge_type"] = matrix.edge_type.astype(str)
    # np.savez adds ".npz" when the name lacks it; write through a file
    # object so the path is exactly the one given.
    with path.open("wb") as handle:
        np.savez_compressed(handle, **arrays)  # type: ignore[arg-type]
    return path


def read_network_matrix(path: str | Path) -> NetworkMatrix:
    """Read a ``matrix.npz`` written by :func:`write_matrix`.

    Raises
    ------
    ValueError
        If the file was written with a newer format version, or its arrays
        do not fit together.
    """
    with np.load(Path(path), allow_pickle=False) as data:
        files = set(data.files)
        missing = [
            key
            for key in (*_SCIPY_KEYS, "meta", *_NODE_KEYS.values())
            if key not in files
        ]
        if missing:
            raise ValueError(f"{path} is not a multimodalpy matrix: missing {missing}.")
        meta = json.loads(str(data["meta"]))
        file_version = int(meta.get("format_version", 0))
        if file_version > FORMAT_VERSION:
            raise ValueError(
                f"{path} uses matrix format version {file_version}; this "
                f"multimodalpy reads up to version {FORMAT_VERSION}. Upgrade "
                "multimodalpy to read it."
            )
        indptr = data["indptr"]
        indices = data["indices"]
        weights = {
            key[len(_WEIGHT_PREFIX) :]: data[key]
            for key in data.files
            if key.startswith(_WEIGHT_PREFIX)
        }
        nodes = pd.DataFrame({column: data[key] for column, key in _NODE_KEYS.items()})
        edge_type = data["edge_type"] if "edge_type" in files else None

    n_nodes = len(indptr) - 1
    problems = []
    if len(nodes) != n_nodes:
        problems.append(f"{len(nodes)} nodes for {n_nodes} rows")
    if int(indptr[-1]) != len(indices):
        problems.append(f"indptr ends at {int(indptr[-1])} for {len(indices)} edges")
    problems.extend(
        f"weight '{name}' has {len(values)} values for {len(indices)} edges"
        for name, values in weights.items()
        if len(values) != len(indices)
    )
    if problems:
        raise ValueError(f"{path} is inconsistent: {'; '.join(problems)}.")
    return NetworkMatrix(
        indptr=indptr,
        indices=indices,
        weights=weights,
        nodes=nodes,
        meta=meta,
        edge_type=edge_type,
    )