"""Tests for the study-area fixes in ``multimodalpy.area``.

The synthetic boundaries file reproduces cases found in the real IGN
municipal file: bilingual names ("Elx/Elche"), names contained in other
names (Elche de la Sierra, Castellonet), municipalities sharing a name in
two provinces (Castejón), a typo one edit away from two towns (Gerona ->
Girona / Gerena) and a non-municipal territory (Gibraltar).
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pytest
from shapely.geometry import box

from multimodalpy import _boundaries, _text, area, network

# name, NATCODE, NATLEVNAME
AREAS = [
    ("Elx/Elche", "34100303065", "Municipio"),
    ("Elche de la Sierra", "34080202030", "Municipio"),
    ("Castelló de la Plana/Castellón de la Plana", "34101212040", "Municipio"),
    ("Castellonet de la Conquesta", "34104646094", "Municipio"),
    ("Donostia/San Sebastián", "34162020069", "Municipio"),
    ("San Sebastián de los Reyes", "34132828134", "Municipio"),
    ("Castejón", "34153131070", "Municipio"),
    ("Castejón", "34081616067", "Municipio"),
    ("Girona", "34091717079", "Municipio"),
    ("Gerena", "34014141045", "Municipio"),
    ("València", "34104646250", "Municipio"),
    ("Sevilla", "34014141091", "Municipio"),
    ("Gibraltar", "34205454006", "Territorio"),
]


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    """The synthetic file, standing in for the packaged municipal boundaries."""
    gdf = gpd.GeoDataFrame(
        {
            "NAMEUNIT": [name for name, _, _ in AREAS],
            "NATCODE": [code for _, code, _ in AREAS],
            "NATLEVNAME": [level for _, _, level in AREAS],
        },
        # Small boxes around Valencia, so UTM buffering works on them.
        geometry=[
            box(-0.4 + i * 0.01, 39.4, -0.395 + i * 0.01, 39.405)
            for i in range(len(AREAS))
        ],
        crs="EPSG:4326",
    )
    path = tmp_path / "boundaries.geojson"
    gdf.to_file(path, driver="GeoJSON")
    monkeypatch.setattr(_boundaries, "_default_boundaries_path", lambda: path)
    return path


def find(packaged, name, **kwargs):
    return area.find_area(name, **kwargs).iloc[0]


# ---------------------------------------------------------------------------
# Names contained in other names are no longer merged
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("query", "expected", "province"),
    [
        ("Elche", "Elx/Elche", 3),
        ("Elx", "Elx/Elche", 3),
        ("San Sebastián", "Donostia/San Sebastián", 20),
        ("Donostia", "Donostia/San Sebastián", 20),
    ],
)
def test_bilingual_part_wins_over_longer_names(packaged, query, expected, province):
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an official name needs no warning
        row = find(packaged, query)
    assert row["match_val"] == expected
    assert row["match_rows"] == 1
    assert row["prov_codes"] == [province]


def test_whole_word_match_skips_longer_words(packaged):
    with pytest.warns(UserWarning, match="whole words"):
        row = find(packaged, "Castellón")
    assert row["match_val"] == "Castelló de la Plana/Castellón de la Plana"
    assert row["prov_codes"] == [12]


def test_substring_matching_several_towns_is_ambiguous(packaged):
    with pytest.raises(area.AmbiguousAreaError) as excinfo:
        find(packaged, "Sebasti")
    assert len(excinfo.value.candidates) == 2


# ---------------------------------------------------------------------------
# Same name in two provinces
# ---------------------------------------------------------------------------
def test_shared_name_raises_with_both_candidates(packaged):
    with pytest.raises(area.AmbiguousAreaError, match="province=") as excinfo:
        find(packaged, "Castejón")
    labels = " ".join(excinfo.value.candidates)
    assert "Navarra" in labels and "Cuenca" in labels


def test_ambiguous_error_is_a_value_error(packaged):
    with pytest.raises(ValueError):
        find(packaged, "Castejón")


@pytest.mark.parametrize(
    ("province", "expected_code"),
    [("Navarra", 31), ("nafarroa", 31), (31, 31), ("31", 31), ("Cuenca", 16)],
)
def test_province_picks_between_shared_names(packaged, province, expected_code):
    row = find(packaged, "Castejón", province=province)
    assert row["prov_codes"] == [expected_code]


def test_unknown_province_raises(packaged):
    with pytest.raises(ValueError, match="Unknown province"):
        find(packaged, "Castejón", province="Narnia")


def test_province_out_of_range_raises(packaged):
    with pytest.raises(ValueError, match="1 to 52"):
        find(packaged, "Castejón", province=99)


def test_province_with_no_rows_raises(packaged):
    with pytest.raises(ValueError, match="No boundary in the file belongs"):
        find(packaged, "Castejón", province="Lugo")


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------
def test_typo_tied_between_two_towns_is_ambiguous(packaged):
    with pytest.raises(area.AmbiguousAreaError) as excinfo:
        find(packaged, "Gerona")
    labels = " ".join(excinfo.value.candidates)
    assert "Girona" in labels and "Gerena" in labels


def test_typo_resolved_with_province_warns(packaged):
    with pytest.warns(UserWarning, match="typo"):
        row = find(packaged, "Gerona", province="Girona")
    assert row["match_val"] == "Girona"


def test_transposition_counts_as_one_edit(packaged):
    with pytest.warns(UserWarning, match="Valencai"):
        row = find(packaged, "Valencai")
    assert row["match_val"] == "València"


def test_unrelated_name_is_not_forced_onto_a_town(packaged):
    # 70% similar to "Sevilla": accepted by the old 0.7 threshold.
    with pytest.raises(ValueError, match="No boundary found"):
        find(packaged, "Sevillanas")


def test_typo_distance_handles_transpositions():
    assert _text._typo_distance("valencai", "valencia") == 1
    assert _text.levenshtein_distance("valencai", "valencia") == 2
    assert _text._typo_distance("", "abc") == 3


# ---------------------------------------------------------------------------
# area_code
# ---------------------------------------------------------------------------
def test_area_code_accepts_five_digit_ine_code(packaged):
    row = find(packaged, "ignored", area_code="46250")
    assert row["match_val"] == "34104646250"
    assert row["prov_codes"] == [46]


def test_area_code_still_accepts_full_natcode(packaged):
    row = find(packaged, "ignored", area_code="34153131070")
    assert row["prov_codes"] == [31]


def test_unknown_ine_code_raises(packaged):
    with pytest.raises(ValueError, match="No boundary found for code"):
        find(packaged, "ignored", area_code="99999")


# ---------------------------------------------------------------------------
# Non-municipal territories
# ---------------------------------------------------------------------------
def test_non_municipal_territory_warns(packaged):
    with pytest.warns(UserWarning, match="not a municipality"):
        row = find(packaged, "Gibraltar")
    assert row["prov_codes"] == []
    assert row["mun_codes"] == []


# ---------------------------------------------------------------------------
# Default file: coverage note and shapefile selection
# ---------------------------------------------------------------------------
def test_not_found_mentions_coverage_and_own_areas(packaged):
    with pytest.raises(ValueError, match="Canary Islands.*boundaries_path"):
        area.find_area("Las Palmas de Gran Canaria")


def test_pick_shapefile_skips_macos_metadata():
    paths = [
        "/cache/__MACOSX/recintos/._recintos.shp",
        "/cache/recintos/._recintos.shp",
        "/cache/recintos/recintos.dbf",
        "/cache/recintos/recintos.shp",
    ]
    assert str(_boundaries._pick_shapefile(paths)) == "/cache/recintos/recintos.shp"
    assert _boundaries._pick_shapefile(["/cache/__MACOSX/._a.shp"]) is None


def test_boundaries_release_hash_is_set():
    assert _boundaries._BOUNDARIES_RELEASE_HASH.startswith("sha256:")


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------
def test_study_area_geometry_buffer_widens_the_area(packaged):
    boundary = area.find_area("Sevilla")
    exact = _boundaries.study_area_geometry(boundary)
    buffered = _boundaries.study_area_geometry(boundary, buffer_m=500)
    assert buffered.contains(exact)
    assert buffered.area > exact.area
    assert exact.equals(boundary.geometry.iloc[0])


def test_study_area_geometry_rejects_negative_buffer(packaged):
    boundary = area.find_area("Sevilla")
    with pytest.raises(ValueError, match="buffer_m"):
        _boundaries.study_area_geometry(boundary, buffer_m=-10)


def test_download_osm_layers_uses_buffer_and_truncate_by_edge(packaged, monkeypatch):
    import osmnx as ox

    boundary = area.find_area("Sevilla")
    captured = {}

    def fake_graph_from_polygon(polygon, **kwargs):
        captured["polygon"] = polygon
        captured.update(kwargs)
        raise RuntimeError("stop after capturing")

    monkeypatch.setattr(ox, "graph_from_polygon", fake_graph_from_polygon)
    with pytest.raises(RuntimeError, match="stop after capturing"):
        network.download_osm(boundary, modes=["walking"], buffer_m=300)

    assert captured["truncate_by_edge"] is True
    assert captured["polygon"].area > boundary.geometry.iloc[0].area
