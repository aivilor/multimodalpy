"""Tests for city/area resolution in ``multimodalpy.get_area``.

``find_area_boundary`` and ``_select_area_rows`` never touch the network:
they read a local boundaries file and match a name against it. These tests
build a small synthetic boundaries file with several real Spanish
municipalities (including accents and a NATCODE per town) and exercise the
matching logic - exact match, accents/case, small typos, area_code lookup,
and province-code extraction - across all of them.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import box

from multimodalpy import get_area

# name, NATCODE (INSPIRE, <2 pais><2 ccaa><2 provincia><5 municipio>), expected province.
# The 5-digit municipio segment starts with the same 2 provincia digits, matching
# the real format (see the "34074040136" -> Segovia example in get_area.py).
CITIES = [
    ("Valencia", "34" + "10" + "46" + "46250", 46),
    ("Madrid", "34" + "13" + "28" + "28079", 28),
    ("Barcelona", "34" + "09" + "08" + "08019", 8),
    ("Sevilla", "34" + "01" + "41" + "41091", 41),
    ("Zaragoza", "34" + "02" + "50" + "50297", 50),
    ("A Coruña", "34" + "12" + "15" + "15030", 15),
    ("Alcalá de Henares", "34" + "13" + "28" + "28006", 28),
    ("San Sebastián", "34" + "16" + "20" + "20069", 20),
]


@pytest.fixture
def boundaries_path(tmp_path):
    """A synthetic boundaries file shaped like the packaged municipal shapefile."""
    gdf = gpd.GeoDataFrame(
        {
            "nombre": [name for name, _, _ in CITIES],
            "NATCODE": [code for _, code, _ in CITIES],
        },
        # Distinct, non-overlapping boxes so union_all() per city is unambiguous.
        geometry=[box(i, 0, i + 0.5, 0.5) for i in range(len(CITIES))],
        crs="EPSG:4326",
    )
    path = tmp_path / "boundaries.geojson"
    gdf.to_file(path, driver="GeoJSON")
    return path


# ---------------------------------------------------------------------------
# Exact / case / accent-insensitive matching, one per city
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("name", "natcode", "province"), CITIES)
def test_find_area_boundary_exact_name(boundaries_path, name, natcode, province):
    result = get_area.find_area_boundary(name, boundaries_path)
    row = result.iloc[0]
    assert row["matched_value"] == name
    assert row["matched_column"] == "nombre"
    assert row["province_code"] == province
    assert len(result) == 1
    assert not result.geometry.iloc[0].is_empty


@pytest.mark.parametrize(("name", "natcode", "province"), CITIES)
def test_find_area_boundary_is_case_insensitive(
    boundaries_path, name, natcode, province
):
    result = get_area.find_area_boundary(name.upper(), boundaries_path)
    assert result.iloc[0]["matched_value"] == name


@pytest.mark.parametrize(
    ("query", "expected_name"),
    [
        ("a coruna", "A Coruña"),  # missing tilde
        ("ALCALA DE HENARES", "Alcalá de Henares"),  # missing accent + upper
        ("san sebastian", "San Sebastián"),  # missing accent
        ("  Valencia  ", "Valencia"),  # surrounding whitespace
    ],
)
def test_find_area_boundary_ignores_accents_case_and_whitespace(
    boundaries_path, query, expected_name
):
    result = get_area.find_area_boundary(query, boundaries_path)
    assert result.iloc[0]["matched_value"] == expected_name


# ---------------------------------------------------------------------------
# Fuzzy matching (small typos), still resolves to the right city
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("typo", "expected_name"),
    [
        ("Valencai", "Valencia"),  # transposition
        ("Sevila", "Sevilla"),  # missing letter
        ("Zaragosa", "Zaragoza"),  # z/s swap
        ("Barcelona ", "Barcelona"),  # trailing space, still exact after normalize
    ],
)
def test_find_area_boundary_tolerates_small_typos(boundaries_path, typo, expected_name):
    result = get_area.find_area_boundary(typo, boundaries_path)
    assert result.iloc[0]["matched_value"] == expected_name


def test_find_area_boundary_rejects_name_too_different_from_any_city(boundaries_path):
    with pytest.raises(ValueError, match="No se encontro ningun limite"):
        get_area.find_area_boundary("Ciudad Que No Existe Del Todo", boundaries_path)


# ---------------------------------------------------------------------------
# area_code lookup (exact NATCODE match, bypasses name matching entirely)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("name", "natcode", "province"), CITIES)
def test_find_area_boundary_by_area_code(boundaries_path, name, natcode, province):
    # area_name is irrelevant once area_code matches; it's only stored on the result.
    result = get_area.find_area_boundary(
        "nombre que no se usa", boundaries_path, area_code=natcode
    )
    row = result.iloc[0]
    assert row["matched_column"] == "NATCODE"
    assert row["matched_value"] == natcode
    assert row["province_code"] == province


def test_find_area_boundary_unknown_area_code_raises(boundaries_path):
    with pytest.raises(ValueError, match="No se encontro ningun limite"):
        get_area.find_area_boundary("Valencia", boundaries_path, area_code="00000000")


# ---------------------------------------------------------------------------
# Province code extraction directly, across every city in the table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("name", "natcode", "province"), CITIES)
def test_province_code_from_natcode(name, natcode, province):
    assert get_area.province_code_from_natcode(natcode) == province


@pytest.mark.parametrize(
    "bad_value",
    [None, "", "abc", "123", "999999999"],  # too short / non-numeric / out of range
)
def test_province_code_from_natcode_returns_none_for_unusable_input(bad_value):
    assert get_area.province_code_from_natcode(bad_value) is None


def test_province_code_from_natcode_accepts_bare_five_digit_ine_code():
    # "40136" -> Segovia municipality, province digits are the first two.
    assert get_area.province_code_from_natcode("40136") == 40


# ---------------------------------------------------------------------------
# Multiple name columns: the first column with a hit wins, per DEFAULT_NAME_COLUMNS order
# ---------------------------------------------------------------------------
def test_find_area_boundary_falls_back_to_second_name_column(tmp_path):
    gdf = gpd.GeoDataFrame(
        {
            # "nombre" is checked before "provincia" in DEFAULT_NAME_COLUMNS,
            # but only "provincia" actually contains the query here.
            "nombre": ["OtroMunicipio"],
            "provincia": ["Cuenca"],
        },
        geometry=[box(0, 0, 1, 1)],
        crs="EPSG:4326",
    )
    path = tmp_path / "boundaries.geojson"
    gdf.to_file(path, driver="GeoJSON")

    result = get_area.find_area_boundary("Cuenca", path)
    row = result.iloc[0]
    assert row["matched_column"] == "provincia"
    assert row["matched_value"] == "Cuenca"


def test_find_area_boundary_raises_when_no_name_column_present(tmp_path):
    gdf = gpd.GeoDataFrame(
        {"unrelated_column": ["x"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326"
    )
    path = tmp_path / "boundaries.geojson"
    gdf.to_file(path, driver="GeoJSON")

    with pytest.raises(ValueError, match="ninguna columna de nombre"):
        get_area.find_area_boundary("Cuenca", path)


# ---------------------------------------------------------------------------
# File-level validation
# ---------------------------------------------------------------------------
def test_find_area_boundary_rejects_unsupported_suffix(tmp_path):
    bad_path = tmp_path / "boundaries.gpkg"
    bad_path.write_bytes(b"")
    with pytest.raises(ValueError, match="solo admite shapefile"):
        get_area.find_area_boundary("Valencia", bad_path)


def test_find_area_boundary_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_area.find_area_boundary("Valencia", tmp_path / "does_not_exist.geojson")


def test_find_area_boundary_empty_file_raises(tmp_path):
    gdf = gpd.GeoDataFrame({"nombre": []}, geometry=[], crs="EPSG:4326")
    path = tmp_path / "boundaries.geojson"
    gdf.to_file(path, driver="GeoJSON")
    with pytest.raises(ValueError, match="esta vacio"):
        get_area.find_area_boundary("Valencia", path)


# ---------------------------------------------------------------------------
# Output CRS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("name", "natcode", "province"), CITIES[:3])
def test_find_area_boundary_reprojects_to_target_crs(
    boundaries_path, name, natcode, province
):
    result = get_area.find_area_boundary(name, boundaries_path, target_crs="EPSG:25830")
    assert result.crs.to_epsg() == 25830
