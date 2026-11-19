"""Tests for GTFS normalization (_gtfs), including the fast paths.

The feeds are tiny zips written to ``tmp_path``, so no network is needed.
"""

from __future__ import annotations

import zipfile

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from multimodalpy import _gtfs

# A 0.1 x 0.1 degree study area.
AREA = box(-0.45, 39.42, -0.35, 39.52)

STOPS = {
    # inside the area
    "A": (39.45, -0.42),
    "B": (39.46, -0.41),
    "C": (39.47, -0.40),
    # outside, on either side: the W -> E segment crosses the area
    "W": (39.47, -0.60),
    "E": (39.47, -0.20),
    # far away, never touches the area
    "X": (38.90, -1.00),
    "Y": (38.95, -1.05),
}


def _write_feed(path, routes, trips, stop_times):
    """Write a minimal GTFS zip.

    ``routes``: list of (route_id, short_name, route_type)
    ``trips``: list of (route_id, service_id, trip_id)
    ``stop_times``: list of (trip_id, arrival, departure, stop_id, seq)
    """
    stops = "\n".join(
        f"{sid},Stop {sid},{lat},{lon}" for sid, (lat, lon) in STOPS.items()
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("stops.txt", f"stop_id,stop_name,stop_lat,stop_lon\n{stops}\n")
        zf.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type\n"
            + "\n".join(f"{r},{s},Route {r},{t}" for r, s, t in routes)
            + "\n",
        )
        zf.writestr(
            "trips.txt",
            "route_id,service_id,trip_id\n"
            + "\n".join(",".join(t) for t in trips)
            + "\n",
        )
        zf.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
            "sunday,start_date,end_date\n"
            "WD,1,1,1,1,1,0,0,20260101,20261231\n",
        )
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            + "\n".join(",".join(map(str, st)) for st in stop_times)
            + "\n",
        )
    return path


@pytest.fixture
def feed(tmp_path):
    routes = [("R1", "1", 3), ("R2", "", 3), ("R3", "3", 3), ("R4", "4", 3)]
    trips = [
        ("R1", "WD", "t1"),
        ("R1", "WD", "t2"),
        ("R2", "WD", "t3"),  # route without route_short_name
        ("R3", "WD", "t4"),  # crosses the area without stopping in it
        ("R4", "WD", "t5"),  # entirely outside the area
    ]
    stop_times = [
        ("t1", "07:00:00", "07:00:00", "A", 1),
        ("t1", "07:05:00", "07:05:00", "B", 2),
        ("t1", "07:09:00", "07:09:00", "C", 3),
        ("t2", "08:10:00", "08:10:00", "A", 1),
        ("t2", "08:16:00", "08:16:00", "B", 2),
        ("t3", "17:30:00", "17:30:00", "B", 1),
        ("t3", "17:34:00", "17:34:00", "C", 2),
        ("t4", "09:00:00", "09:00:00", "W", 1),
        ("t4", "09:20:00", "09:20:00", "E", 2),
        ("t5", "10:00:00", "10:00:00", "X", 1),
        ("t5", "10:15:00", "10:15:00", "Y", 2),
    ]
    return _write_feed(tmp_path / "feed.zip", routes, trips, stop_times)


def _edges(feed, **kwargs):
    _, edges, _ = _gtfs.normalize_gtfs_feed(
        feed, dataset_name="ds", layer_id="ds", **kwargs
    )
    return edges.set_index(["from_node", "to_node", "route_id"]).sort_index()


# ---------------------------------------------------------------------------
# Hourly summary (option C)
# ---------------------------------------------------------------------------
def test_pack_by_group_matches_pack_key_value():
    rng = np.random.default_rng(3)
    rows = pd.DataFrame(
        {
            "g1": rng.choice(["a", "b", "c"], 400),
            "g2": rng.choice(["x", None], 400),
            "band": rng.choice([f"{h:02d}-{h + 1:02d}" for h in range(24)], 400),
            "value": rng.uniform(30, 2000, 400),
        }
    ).drop_duplicates(["g1", "g2", "band"])
    rows.loc[rows.sample(frac=0.1, random_state=1).index, "value"] = np.nan

    packed = _gtfs._pack_by_group(rows, ["g1", "g2"], "band", "value")
    # NaN keys can't be looked up with .loc, so compare as plain dicts.
    actual = {
        tuple(None if pd.isna(part) else part for part in key): text
        for key, text in packed.items()
    }

    expected = {}
    for key, group in rows.groupby(["g1", "g2"], dropna=False):
        text = _gtfs._pack_key_value(group.set_index("band")["value"])
        if text is not None:
            expected[tuple(None if pd.isna(part) else part for part in key)] = text

    assert actual == expected
    assert any(key[1] is None for key in actual)  # NaN keys are kept


def test_hourly_summary_is_packed_in_hour_order(feed):
    edges = _edges(feed)
    row = edges.loc[("A", "B", "R1")]
    assert row["hourly_trip_counts"] == "07-08:1|08-09:1"
    assert row["hourly_travel_times"] == "07-08:300|08-09:360"


def test_hourly_summary_kept_when_route_has_no_short_name(feed):
    edges = _edges(feed)
    row = edges.loc[("B", "C", "R2")]
    assert pd.isna(row["route_short_name"])
    assert row["hourly_trip_counts"] == "17-18:1"
    assert row["hourly_travel_times"] == "17-18:240"


# ---------------------------------------------------------------------------
# Clipping to the study area before aggregating
# ---------------------------------------------------------------------------
def test_clip_keeps_segments_crossing_the_area_without_a_stop(feed):
    edges = _edges(feed, filter_geometry=AREA)
    assert ("W", "E", "R3") in edges.index
    assert edges.loc[("W", "E", "R3"), "trip_count"] == 1


def test_clip_drops_trips_outside_the_area(feed):
    edges = _edges(feed, filter_geometry=AREA)
    assert ("X", "Y", "R4") not in edges.index
    assert set(edges.index.get_level_values("route_id")) == {"R1", "R2", "R3"}


def test_clip_gives_the_same_edges_as_clipping_after_aggregating(feed):
    clipped = _edges(feed, filter_geometry=AREA)
    everything = _edges(feed)
    expected = everything[everything.geometry.intersects(AREA)]
    pd.testing.assert_frame_equal(
        clipped.drop(columns="geometry"), expected.drop(columns="geometry")
    )


def test_area_without_transit_gives_an_empty_layer_with_all_columns(feed):
    _, edges, _ = _gtfs.normalize_gtfs_feed(
        feed, dataset_name="ds", layer_id="ds", filter_geometry=box(10, 10, 11, 11)
    )
    assert edges.empty
    assert {"edge_id", "trip_count", "hourly_trip_counts", "geometry"} <= set(
        edges.columns
    )


# ---------------------------------------------------------------------------
# Trip records shared by the edges layer and the schedule table
# ---------------------------------------------------------------------------
def _tables(feed):
    return {
        name: _gtfs.read_gtfs_table(feed, f"{name}.txt")
        for name in ("stop_times", "stops", "trips", "routes", "calendar")
    }


def test_trip_records_are_built_once_with_the_schedule_table(feed, monkeypatch):
    calls = []
    build = _gtfs._build_stop_to_stop_trip_records

    def counting_build(*args, **kwargs):
        calls.append(1)
        return build(*args, **kwargs)

    monkeypatch.setattr(_gtfs, "_build_stop_to_stop_trip_records", counting_build
    )
    _gtfs.normalize_gtfs_feed(
        feed, dataset_name="ds", layer_id="ds", include_schedule_table=True
    )
    assert len(calls) == 1


def test_shared_records_give_the_same_tables_as_the_standalone_builders(feed):
    _, edges, schedule = _gtfs.normalize_gtfs_feed(
        feed,
        dataset_name="ds",
        layer_id="ds",
        filter_geometry=AREA,
        include_schedule_table=True,
        target_crs=None,
    )
    t = _tables(feed)
    args = (t["stop_times"], t["stops"], t["trips"], t["routes"])
    standalone_edges = _gtfs.build_gtfs_stop_to_stop_edges_gdf(
        *args,
        dataset_name="ds",
        layer_id="ds",
        filter_geometry=AREA,
        calendar=t["calendar"],
    )
    standalone_schedule = _gtfs.build_gtfs_schedule_table(
        *args, dataset_name="ds", calendar=t["calendar"]
    )
    pd.testing.assert_frame_equal(
        edges.reset_index(drop=True), standalone_edges.reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(schedule, standalone_schedule)


# ---------------------------------------------------------------------------
# Feeds of a mode that wasn't requested
# ---------------------------------------------------------------------------
@pytest.fixture
def bus_and_train_feeds(tmp_path, monkeypatch):
    """The NAP returns a bus feed and a high-speed rail feed (route_type 101)."""
    import geopandas as gpd

    trips = [("R1", "WD", "t1")]
    stop_times = [
        ("t1", "07:00:00", "07:00:00", "A", 1),
        ("t1", "07:05:00", "07:05:00", "B", 2),
    ]
    bus = _write_feed(tmp_path / "emt.zip", [("R1", "1", 3)], trips, stop_times)
    train = _write_feed(tmp_path / "ouigo.zip", [("R1", "", 101)], trips, stop_times)
    monkeypatch.setattr(
        _gtfs, "download_gtfs_nap_zips", lambda *args, **kwargs: [bus, train]
    )
    return gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")


@pytest.mark.parametrize(
    ("modes", "kept", "skipped"),
    [
        ([1], {"emt": "bus"}, {"ouigo": "train"}),
        ([2], {"ouigo": "train"}, {"emt": "bus"}),
        ([1, 2], {"emt": "bus", "ouigo": "train"}, {}),
    ],
)
def test_feeds_of_unrequested_modes_are_skipped(
    tmp_path, bus_and_train_feeds, modes, kept, skipped
):
    found: dict[str, str] = {}
    results = _gtfs.download_gtfs(
        "Valencia",
        bus_and_train_feeds,
        tmp_path / "zips",
        modes=modes,
        api_key="key",
        skipped=found,
    )
    assert {name: out["mode"] for name, out in results.items()} == kept
    assert found == skipped


# ---------------------------------------------------------------------------
# Custom peak periods: codes and columns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("periods", "expected"),
    [
        (None, {"peak_am": "pam", "peak_pm": "ppm", "rest_of_day": "rod"}),
        (
            {"morning": (6, 9), "evening": (18, 21)},
            {"morning": "mor", "evening": "eve", "rest_of_day": "rod"},
        ),
        (
            {"peak_am": (7, 9), "night": (22, 24)},
            {"peak_am": "pam", "night": "nig", "rest_of_day": "rod"},
        ),
        ({"Mañana": (7, 10)}, {"Mañana": "man", "rest_of_day": "rod"}),
        # Clashing first letters: all custom periods are numbered.
        (
            {"morning": (6, 8), "mornings2": (8, 10)},
            {"morning": "p1", "mornings2": "p2", "rest_of_day": "rod"},
        ),
        # Too short, or the same as a default code.
        ({"am": (7, 9)}, {"am": "p1", "rest_of_day": "rod"}),
        ({"pamplona": (7, 9)}, {"pamplona": "p1", "rest_of_day": "rod"}),
    ],
)
def test_period_codes(periods, expected):
    assert _gtfs.period_codes(periods) == expected


@pytest.mark.parametrize(
    "periods",
    [
        None,
        {"morning": (6, 9), "evening": (18, 21)},
        {f"period_{i}": (i, i + 1) for i in range(12)},  # p1 ... p12
    ],
)
def test_short_names_fit_the_shapefile_and_are_unique(periods):
    rename = _gtfs.gtfs_edge_rename(periods)
    targets = list(rename.values())
    assert max(len(name) for name in targets) <= 10
    assert len(targets) == len(set(targets))


def test_default_columns_are_unchanged():
    """The columns kept with the default periods, as before custom periods."""
    expected = [
        "edge_id", "from_node", "to_node", "route_id", "route_short_name",
        "route_long_name", "trip_count", "travel_time_seconds_mean",
        "travel_time_seconds_median", "travel_time_seconds_min",
        "travel_time_seconds_max",
        "travel_time_seconds_mean_peak_am", "travel_time_seconds_mean_peak_pm",
        "travel_time_seconds_mean_rest_of_day",
        "travel_time_seconds_median_peak_am",
        "travel_time_seconds_median_peak_pm",
        "travel_time_seconds_median_rest_of_day",
        "trip_count_peak_am", "trip_count_peak_pm", "trip_count_rest_of_day",
        "travel_time_seconds_mean_weekday", "trip_count_weekday",
        "travel_time_seconds_mean_weekend", "trip_count_weekend",
        "days_of_week_summary", "hourly_travel_times", "hourly_trip_counts",
        "geometry",
    ]  # fmt: skip
    for period in ("peak_am", "peak_pm", "rest_of_day"):
        for day_type in ("weekday", "weekend"):
            expected.append(f"travel_time_seconds_mean_{period}_{day_type}")
            expected.append(f"trip_count_{period}_{day_type}")
    assert _gtfs.gtfs_edge_columns() == expected
    assert _gtfs.gtfs_edge_rename() == _gtfs.GTFS_EDGE_RENAME


def test_every_period_column_of_a_feed_is_kept(feed):
    """What normalize_gtfs_feed computes for custom periods is all kept."""
    periods = {"morning": (6, 9), "evening": (17, 20)}
    _, edges, _ = _gtfs.normalize_gtfs_feed(
        feed,
        dataset_name="ds",
        layer_id="ds",
        filter_geometry=AREA,
        peak_periods=periods,
        include_schedule_table=False,
    )
    per_period = [
        column
        for column in edges.columns
        if any(name in column for name in ("morning", "evening", "rest_of_day"))
    ]
    assert "travel_time_seconds_mean_morning" in per_period
    assert "trip_count_evening_weekday" in per_period
    kept = _gtfs.gtfs_edge_columns(periods)
    assert set(per_period) <= set(kept)
    rename = _gtfs.gtfs_edge_rename(periods)
    assert rename["travel_time_seconds_mean_morning"] == "tts_m_mor"
    assert rename["trip_count_evening_weekday"] == "trp_eve_wd"
