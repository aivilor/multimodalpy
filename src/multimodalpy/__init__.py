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

import logging

from . import get_area, get_network, process_gtfs, process_osm
from .get_area import find_area_boundary
from .get_network import main

# Una libreria no debe escribir en la terminal por su cuenta: sin este handler,
# Python mostraria por stderr los avisos de nivel WARNING aunque la aplicacion
# no haya configurado logging. Se activan con logging.basicConfig(level=...).
logging.getLogger(__name__).addHandler(logging.NullHandler())

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
