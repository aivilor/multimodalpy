"""multimodalpy - libreria para descargar y estandarizar redes multimodales.

Uso basico:

    from multimodalpy import main

    main("Valencia", modes=["caminable", "bicicleta"], output_path="salida")

Modulos:
    - process_osm    : estandarizacion de datos OSM (creacion de intersecciones).
    - process_gtfs   : estandarizacion de datos GTFS + limpieza de bus/tren.
    - get_area       : descarga desde APIs OSM y GTFS.
    - get_network    : descarga en local segun indicaciones del usuario (main()).
"""

from __future__ import annotations

from . import get_area
from .get_area import find_area_boundary
from .get_network import main
from . import get_network, process_gtfs, process_osm

__version__ = "0.1.0"

__all__ = [
    "main",
    "find_area_boundary",
    "get_network",
    "get_area",
    "process_osm",
    "process_gtfs",
    "__version__",
]
