"""Network analyses: isochrones, shortest paths and more.

Every function takes the network in any form multimodalpy writes or reads:
the output folder of :func:`multimodalpy.download_network` (with ``mode``),
a network folder such as ``"output/walking"``, a ``matrix.npz`` /
``graph.json`` / GeoPackage file, or the result of
:func:`multimodalpy.read_matrix` or :func:`multimodalpy.read_graph`.
Internally it is turned into the CSR matrix of the ``matrix`` format, so
``matrix.npz`` is the fastest to load.

Only walking, bike and driving networks are supported for now: a bus or
train network on its own has no walking access, waiting times or transfers,
so its results would be misleading. They will come with the multimodal
network.

The functions are also available from the package itself::

    from multimodalpy import isochrones
"""

from __future__ import annotations

from ._isochrones import isochrones

__all__ = ["isochrones"]
