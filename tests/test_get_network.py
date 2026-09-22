"""Tests para multimodalpy.get_network.main().

Las llamadas reales a OSM/Overpass y a la API de NAP se simulan (mock) para
que los tests sean rapidos, deterministas y no dependan de red ni de
credenciales. Se comprueba tanto la validacion de parametros como el flujo
completo de escritura de ficheros con datos de ejemplo controlados.
"""

from __future__ import annotations

from unittest.mock import patch

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from multimodalpy import get_area, get_network


# ---------------------------------------------------------------------------
# Fixtures: datos de ejemplo minimos, sin red
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_boundary():
    """Un limite de estudio minimo (un cuadrado) en WGS84."""
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    return gpd.GeoDataFrame({"name": ["fake_area"]}, geometry=[polygon], crs="EPSG:4326")


@pytest.fixture
def fake_osm_layers():
    """Simula la salida de get_area.download_osm_layers para el modo 'walking'."""
    nodes = gpd.GeoDataFrame(
        {"osmid": [1, 2]}, geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:4326"
    )
    edges = gpd.GeoDataFrame(
        {"length": [100.0]},
        geometry=[LineString([(0, 0), (1, 1)])],
        crs="EPSG:4326",
    )
    return {"walking": {"nodes": nodes, "edges": edges, "graph": object()}}


# ---------------------------------------------------------------------------
# Validacion de parametros (no requieren red, deben fallar antes de llamar
# a get_area)
# ---------------------------------------------------------------------------
def test_main_rejects_invalid_output_file_type(tmp_path):
    with pytest.raises(ValueError, match="output_file_type"):
        get_network.main(
            area_name="Valencia",
            modes=["walking"],
            output_path=str(tmp_path),
            output_file_type="not_a_real_format",
        )


def test_main_rejects_invalid_mode(tmp_path):
    with pytest.raises(ValueError, match="Modo desconocido"):
        get_network.main(
            area_name="Valencia",
            modes=["not_a_real_mode"],
            output_path=str(tmp_path),
        )


# ---------------------------------------------------------------------------
# Flujo completo (OSM), con red simulada
# ---------------------------------------------------------------------------
def test_main_downloads_and_writes_osm_layers(tmp_path, fake_boundary, fake_osm_layers):
    with (
        patch.object(get_area, "find_area_boundary", return_value=fake_boundary) as mock_boundary,
        patch.object(get_area, "download_osm_layers", return_value=fake_osm_layers) as mock_osm,
    ):
        manifest = get_network.main(
            area_name="Valencia",
            modes=["walking"],
            output_path=str(tmp_path),
            output_file_type="geojson",
        )

    # Se llama a las funciones de descarga con el area solicitada.
    mock_boundary.assert_called_once()
    mock_osm.assert_called_once()

    # El manifiesto refleja correctamente lo descargado.
    assert manifest["area_name"] == "Valencia"
    assert manifest["osm_modes"] == ["walking"]
    assert manifest["gtfs_datasets"] == []
    assert manifest["output_file_type"] == "geojson"

    # Los ficheros esperados existen realmente en disco.
    written_files = set(manifest["files"])
    assert "study_area_boundary.geojson" in written_files
    assert "osm_walking_nodes.geojson" in written_files
    assert "osm_walking_edges.geojson" in written_files
    for filename in written_files:
        assert (tmp_path / filename).exists()