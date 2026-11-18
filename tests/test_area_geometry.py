"""Tests for the study area given as a geometry (``find_area(boundaries_path=...)``).

The packaged municipal file is replaced by a synthetic one, laid out in
metres (EPSG:25830) and stored in EPSG:4258 like the real file, so the
overlap shares are exact::

    +---------+---------+
    | C (12)  |         |      y 4380-4390 km
    +---------+---------+---------+
    | A (46)  | B (46)  | T (--)  |  y 4370-4380 km
    +---------+---------+---------+
    x 720     730       740       750 km

A and B are municipalities of Valencia (46), C of Castellón (12) and T a
shared territory with no province.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from multimodalpy import _boundaries, area

KM = 1000
A = box(720 * KM, 4370 * KM, 730 * KM, 4380 * KM)
B = box(730 * KM, 4370 * KM, 740 * KM, 4380 * KM)
C = box(720 * KM, 4380 * KM, 730 * KM, 4390 * KM)
T = box(740 * KM, 4370 * KM, 750 * KM, 4380 * KM)


@pytest.fixture(autouse=True)
def packaged(tmp_path, monkeypatch):
    gdf = gpd.GeoDataFrame(
        {
            "NAMEUNIT": ["Alfa", "Beta", "Castell", "Comunidad"],
            "NATCODE": ["34104646001", "34104646002", "34101212003", "34205454006"],
            "NATLEVNAME": ["Municipio", "Municipio", "Municipio", "Territorio"],
        },
        geometry=[A, B, C, T],
        crs="EPSG:25830",
    ).to_crs("EPSG:4258")
    path = tmp_path / "packaged.shp"
    gdf.to_file(path)
    monkeypatch.setattr(_boundaries, "_default_boundaries_path", lambda: path)
    return path


def write(tmp_path, geometries, name="study_area.geojson", crs="EPSG:25830"):
    gdf = gpd.GeoDataFrame({"pat": ["x"] * len(geometries)}, geometry=geometries)
    if crs is not None:
        gdf = gdf.set_crs(crs)
    path = tmp_path / name
    gdf.to_file(path)
    return path


def find(path, **kwargs):
    return area.find_area(boundaries_path=path, **kwargs).iloc[0]


# ---------------------------------------------------------------------------
# The file's geometry is the study area
# ---------------------------------------------------------------------------
def test_no_name_or_code_columns_are_needed(tmp_path):
    row = find(write(tmp_path, [A.union(B)]))
    assert row["prov_codes"] == [46]
    assert row["prov_names"] == ["Valencia/València"]
    assert row["mun_codes"] == ["46001", "46002"]
    assert row["mun_names"] == ["Alfa", "Beta"]


def test_area_name_defaults_to_the_file_name(tmp_path):
    row = find(write(tmp_path, [A], name="pateval.geojson"))
    assert row["area_name"] == "pateval"
    assert row["source_path"].endswith("pateval.geojson")


def test_area_name_is_kept_as_the_label(tmp_path):
    row = find(write(tmp_path, [A]), area_name="Área metropolitana")
    assert row["area_name"] == "Área metropolitana"


def test_several_polygons_are_merged_into_one(tmp_path):
    result = area.find_area(
        boundaries_path=write(tmp_path, [A, B]), target_crs="EPSG:25830"
    )
    assert len(result) == 1
    assert result.iloc[0]["matched_rows"] == 2
    assert result.geometry.iloc[0].area == pytest.approx(A.union(B).area)


def test_geometry_is_the_users_not_the_municipalities(tmp_path):
    inner = box(722 * KM, 4372 * KM, 728 * KM, 4378 * KM)
    result = area.find_area(
        boundaries_path=write(tmp_path, [inner]), target_crs="EPSG:25830"
    )
    assert result.geometry.iloc[0].area == pytest.approx(inner.area)


@pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:3857", "EPSG:25830"])
def test_any_crs_is_converted(tmp_path, crs):
    geometry = gpd.GeoSeries([A], crs="EPSG:25830").to_crs(crs).iloc[0]
    path = write(tmp_path, [geometry], crs=crs)
    result = area.find_area(boundaries_path=path)
    assert result.crs.to_epsg() == 4326
    assert result.iloc[0]["prov_codes"] == [46]


def test_shapefile_is_accepted(tmp_path):
    row = find(write(tmp_path, [B], name="area.shp"))
    assert row["mun_names"] == ["Beta"]


def test_invalid_polygon_is_repaired(tmp_path):
    # A bow-tie: self-intersecting, so its plain area would be 0.
    bowtie = Polygon(
        [(720 * KM, 4370 * KM), (730 * KM, 4380 * KM),
         (730 * KM, 4370 * KM), (720 * KM, 4380 * KM)]
    )  # fmt: skip
    row = find(write(tmp_path, [bowtie]))
    assert row["prov_codes"] == [46]


# ---------------------------------------------------------------------------
# Provinces: at least 5% of the study area
# ---------------------------------------------------------------------------
def test_sliver_into_a_neighbour_province_is_ignored(tmp_path):
    # A plus 100 m of C: 1% of the area falls in Castellón.
    study = box(720 * KM, 4370 * KM, 730 * KM, 4380.1 * KM)
    row = find(write(tmp_path, [study]))
    assert row["prov_codes"] == [46]
    assert row["mun_names"] == ["Alfa"]


@pytest.mark.parametrize(
    ("north_km", "expected"),
    [(0.4, [46]), (0.6, [46, 12]), (5, [46, 12])],
)
def test_province_needs_five_percent_of_the_area(tmp_path, north_km, expected):
    # 10 km of A plus ``north_km`` of C: C's share is north_km / (10 + north_km).
    study = box(720 * KM, 4370 * KM, 730 * KM, (4380 + north_km) * KM)
    assert find(write(tmp_path, [study]))["prov_codes"] == expected


def test_provinces_are_listed_by_their_share(tmp_path):
    # 30% in Valencia, 70% in Castellón.
    study = box(720 * KM, 4377 * KM, 730 * KM, 4387 * KM)
    row = find(write(tmp_path, [study]))
    assert row["prov_codes"] == [12, 46]
    assert row["prov_names"] == ["Castellón/Castelló", "Valencia/València"]


# ---------------------------------------------------------------------------
# Municipalities
# ---------------------------------------------------------------------------
def test_large_municipality_holding_the_whole_area_is_listed(tmp_path):
    # 1 km2 inside A: 1% of Alfa, but 100% of the study area.
    study = box(724 * KM, 4374 * KM, 725 * KM, 4375 * KM)
    assert find(write(tmp_path, [study]))["mun_names"] == ["Alfa"]


def test_territories_are_not_municipalities(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        row = find(write(tmp_path, [B.union(T)]))
    assert row["prov_codes"] == [46]
    assert row["mun_names"] == ["Beta"]


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------
def test_area_outside_the_coverage_raises(tmp_path):
    far_away = box(400 * KM, 3100 * KM, 410 * KM, 3110 * KM)
    with pytest.raises(ValueError, match="outside the area covered.*Canary"):
        find(write(tmp_path, [far_away]))


def test_area_in_no_province_raises(tmp_path):
    with pytest.raises(ValueError, match="no province holds at least 5%"):
        find(write(tmp_path, [T]))


def test_area_partly_outside_the_boundaries_warns(tmp_path):
    # Half of it south of A, where there is no municipality (e.g. the sea).
    study = box(720 * KM, 4360 * KM, 730 * KM, 4380 * KM)
    with pytest.warns(UserWarning, match="50% of the study area is outside"):
        row = find(write(tmp_path, [study]))
    assert row["prov_codes"] == [46]


def test_area_inside_the_boundaries_does_not_warn(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        find(write(tmp_path, [A.union(B)]))


# ---------------------------------------------------------------------------
# Invalid input
# ---------------------------------------------------------------------------
def test_file_without_crs_raises(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # pyogrio warns when writing it
        path = write(tmp_path, [A], name="no_crs.shp", crs=None)
    with pytest.raises(ValueError, match="no CRS"):
        find(path)


@pytest.mark.parametrize(
    "geometry",
    [
        Point(725 * KM, 4375 * KM),
        LineString([(720 * KM, 4370 * KM), (730 * KM, 4380 * KM)]),
    ],
)
def test_points_and_lines_are_not_an_area(tmp_path, geometry):
    with pytest.raises(ValueError, match="must be a polygon"):
        find(write(tmp_path, [geometry]))


@pytest.mark.parametrize("extra", [{"area_code": "46250"}, {"province": "Valencia"}])
def test_name_options_are_rejected_with_a_geometry(tmp_path, extra):
    with pytest.raises(ValueError, match="only apply to a search by name"):
        find(write(tmp_path, [A]), **extra)


def test_an_area_must_be_given():
    with pytest.raises(ValueError, match="Give the study area"):
        area.find_area()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def test_boundary_for_export_joins_the_lists(tmp_path):
    boundary = area.find_area(boundaries_path=write(tmp_path, [A.union(C)]))
    exported = _boundaries.boundary_for_export(boundary)
    row = exported.iloc[0]
    assert row["prov_codes"] == "12; 46"  # equal shares: by code
    assert row["mun_names"] == "Castell; Alfa"  # by INE code
    assert boundary.iloc[0]["prov_codes"] == [12, 46]  # the input is untouched


# matched_column, matched_value... are truncated in a Shapefile (as before);
# the new list columns are not.
@pytest.mark.filterwarnings("ignore:Column names longer than 10")
@pytest.mark.parametrize("name", ["boundary.gpkg", "boundary.shp", "boundary.geojson"])
def test_exported_boundary_can_be_written(tmp_path, name):
    boundary = area.find_area(boundaries_path=write(tmp_path, [A]))
    _boundaries.boundary_for_export(boundary).to_file(tmp_path / name)
    written = gpd.read_file(tmp_path / name)
    assert written.iloc[0]["mun_codes"] == "46001"
