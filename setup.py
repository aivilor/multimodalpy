"""Configuracion de empaquetado para pip de la libreria multimodalpy."""

from pathlib import Path

from setuptools import find_packages, setup

_here = Path(__file__).resolve().parent
_readme = _here / "README.md"
_long_description = _readme.read_text(encoding="utf-8") if _readme.exists() else ""

setup(
    name="multimodalpy",
    version="0.1.0",
    description="Libreria para descargar y estandarizar redes multimodales de transporte.",
    long_description=_long_description,
    long_description_content_type="text/markdown",
    author="Aida Villalba Ortiz",
    author_email="aivilor@upv.es",
    python_requires=">=3.10",
    packages=find_packages(),
    include_package_data=True,
    # El shapefile de recintos municipales viaja dentro del paquete.
    package_data={
        "multimodalpy": ["data/recintos_municipales_inspire_peninbal_etrs89/*"],
    },
    install_requires=[
        "geopandas",
        "osmnx",
        "shapely",
        "pandas",
        "numpy",
        "networkx",
        "pyproj",
        "requests",
    ],
    entry_points={
        "console_scripts": [
            "multimodalpy=multimodalpy.get_network:_cli",
        ],
    },
)
