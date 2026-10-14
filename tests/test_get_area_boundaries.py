"""Tests for the study-area fixes in ``multimodalpy.get_area``.

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

from multimodalpy import get_area

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
def boundaries_path(tmp_path):
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
    return path


def find(path, name, **kwargs):
    return get_area.find_area_boundary(name, path, **kwargs).iloc[0]


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
def test_bilingual_part_wins_over_longer_names(
    boundaries_path, query, expected, province
):
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an official name needs no warning
        row = find(boundaries_path, query)
    assert row["matched_value"] == expected
    assert row["matched_rows"] == 1
    assert row["province_code"] == province


def test_whole_word_match_skips_longer_words(boundaries_path):
    with pytest.warns(UserWarning, match="whole words"):
        row = find(boundaries_path, "Castellón")
    assert row["matched_value"] == "Castelló de la Plana/Castellón de la Plana"
    assert row["province_code"] == 12


def test_substring_matching_several_towns_is_ambiguous(boundaries_path):
    with pytest.raises(get_area.AmbiguousAreaError) as excinfo:
        find(boundaries_path, "Sebasti")
    assert len(excinfo.value.candidates) == 2


# ---------------------------------------------------------------------------
# Same name in two provinces
# ---------------------------------------------------------------------------
def test_shared_name_raises_with_both_candidates(boundaries_path):
    with pytest.raises(get_area.AmbiguousAreaError, match="province=") as excinfo:
        find(boundaries_path, "Castejón")
    labels = " ".join(excinfo.value.candidates)
    assert "Navarra" in labels and "Cuenca" in labels


def test_ambiguous_error_is_a_value_error(boundaries_path):
    with pytest.raises(ValueError):
        find(boundaries_path, "Castejón")


@pytest.mark.parametrize(
    ("province", "expected_code"),
    [("Navarra", 31), ("nafarroa", 31), (31, 31), ("31", 31), ("Cuenca", 16)],
)
def test_province_picks_between_shared_names(boundaries_path, province, expected_code):
    row = find(boundaries_path, "Castejón", province=province)
    assert row["province_code"] == expected_code


def test_unknown_province_raises(boundaries_path):
    with pytest.raises(ValueError, match="Unknown province"):
        find(boundaries_path, "Castejón", province="Narnia")


def test_province_out_of_range_raises(boundaries_path):
    with pytest.raises(ValueError, match="1 to 52"):
        find(boundaries_path, "Castejón", province=99)


def test_province_with_no_rows_raises(boundaries_path):
    with pytest.raises(ValueError, match="No boundary in the file belongs"):
        find(boundaries_path, "Castejón", province="Lugo")


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------
def test_typo_tied_between_two_towns_is_ambiguous(boundaries_path):
    with pytest.raises(get_area.AmbiguousAreaError) as excinfo:
        find(boundaries_path, "Gerona")
    labels = " ".join(excinfo.value.candidates)
    assert "Girona" in labels and "Gerena" in labels


def test_typo_resolved_with_province_warns(boundaries_path):
    with pytest.warns(UserWarning, match="typo"):
        row = find(boundaries_path, "Gerona", province="Girona")
    assert row["matched_value"] == "Girona"


def test_transposition_counts_as_one_edit(boundaries_path):
    with pytest.warns(UserWarning, match="Valencai"):
        row = find(boundaries_path, "Valencai")
    assert row["matched_value"] == "València"


def test_unrelated_name_is_not_forced_onto_a_town(boundaries_path):
    # 70% similar to "Sevilla": accepted by the old 0.7 threshold.
    with pytest.raises(ValueError, match="No boundary found"):
        find(boundaries_path, "Sevillanas")


def test_typo_distance_handles_transpositions():
    assert get_area._typo_distance("valencai", "valencia") == 1
    assert get_area.levenshtein_distance("valencai", "valencia") == 2
    assert get_area._typo_distance("", "abc") == 3


# ---------------------------------------------------------------------------
# area_code
# ---------------------------------------------------------------------------
def test_area_code_accepts_five_digit_ine_code(boundaries_path):
    row = find(boundaries_path, "ignored", area_code="46250")
    assert row["matched_value"] == "34104646250"
    assert row["province_code"] == 46


def test_area_code_still_accepts_full_natcode(boundaries_path):
    row = find(boundaries_path, "ignored", area_code="34153131070")
    assert row["province_code"] == 31


def test_unknown_ine_code_raises(boundaries_path):
    with pytest.raises(ValueError, match="No boundary found for code"):
        find(boundaries_path, "ignored", area_code="99999")


# ---------------------------------------------------------------------------
# Non-municipal territories
# ---------------------------------------------------------------------------
def test_non_municipal_territory_warns(boundaries_path):
    with pytest.warns(UserWarning, match="not a municipality"):
        row = find(boundaries_path, "Gibraltar")
    assert row["province_code"] is None


# ---------------------------------------------------------------------------
# Default file: coverage note and shapefile selection
# ---------------------------------------------------------------------------
def test_not_found_with_default_file_mentions_coverage(boundaries_path, monkeypatch):
    monkeypatch.setattr(get_area, "_default_boundaries_path", lambda: boundaries_path)
    with pytest.raises(ValueError, match="Canary Islands"):
        get_area.find_area_boundary("Las Palmas de Gran Canaria")


def test_not_found_with_own_file_has_no_coverage_note(boundaries_path):
    with pytest.raises(ValueError) as excinfo:
        find(boundaries_path, "Las Palmas de Gran Canaria")
    assert "Canary" not in str(excinfo.value)


def test_pick_shapefile_skips_macos_metadata():
    paths = [
        "/cache/__MACOSX/recintos/._recintos.shp",
        "/cache/recintos/._recintos.shp",
        "/cache/recintos/recintos.dbf",
        "/cache/recintos/recintos.shp",
    ]
    assert str(get_area._pick_shapefile(paths)) == "/cache/recintos/recintos.shp"
    assert get_area._pick_shapefile(["/cache/__MACOSX/._a.shp"]) is None


def test_boundaries_release_hash_is_set():
    assert get_area._BOUNDARIES_RELEASE_HASH.startswith("sha256:")


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------
def test_study_area_geometry_buffer_widens_the_area(boundaries_path):
    boundary = get_area.find_area_boundary("Sevilla", boundaries_path)
    exact = get_area.study_area_geometry(boundary)
    buffered = get_area.study_area_geometry(boundary, buffer_m=500)
    assert buffered.contains(exact)
    assert buffered.area > exact.area
    assert exact.equals(boundary.geometry.iloc[0])


def test_study_area_geometry_rejects_negative_buffer(boundaries_path):
    boundary = get_area.find_area_boundary("Sevilla", boundaries_path)
    with pytest.raises(ValueError, match="buffer_m"):
        get_area.study_area_geometry(boundary, buffer_m=-10)


def test_download_osm_layers_uses_buffer_and_truncate_by_edge(
    boundaries_path, monkeypatch
):
    import osmnx as ox

    boundary = get_area.find_area_boundary("Sevilla", boundaries_path)
    captured = {}

    def fake_graph_from_polygon(polygon, **kwargs):
        captured["polygon"] = polygon
        captured.update(kwargs)
        raise RuntimeError("stop after capturing")

    monkeypatch.setattr(ox, "graph_from_polygon", fake_graph_from_polygon)
    with pytest.raises(RuntimeError, match="stop after capturing"):
        get_area.download_osm_layers(boundary, modes=["walking"], buffer_m=300)

    assert captured["truncate_by_edge"] is True
    assert captured["polygon"].area > boundary.geometry.iloc[0].area