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


def _write_feed(path, routes, trips, stop_times, extra=None):
    """Write a minimal GTFS zip.

    ``routes``: list of (route_id, short_name, route_type)
    ``trips``: list of (route_id, service_id, trip_id)
    ``stop_times``: list of (trip_id, arrival, departure, stop_id, seq)
    ``extra``: other files, ``{file name: content}``, replacing the default
    ones (e.g. ``calendar.txt``)
    """
    extra = dict(extra or {})
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
            extra.pop(
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
                "sunday,start_date,end_date\n"
                "WD,1,1,1,1,1,0,0,20260101,20261231\n",
            ),
        )
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            + "\n".join(",".join(map(str, st)) for st in stop_times)
            + "\n",
        )
        for name, content in extra.items():
            zf.writestr(name, content)
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


def test_period_columns_kept_when_route_has_no_short_name(feed):
    """The trip at 17:30 is in peak_pm, whatever the route is called."""
    row = _edges(feed).loc[("B", "C", "R2")]
    assert pd.isna(row["route_short_name"])
    assert row["trip_count_peak_pm"] == 1
    assert row["travel_time_seconds_mean_peak_pm"] == 240
    assert row["travel_time_seconds_median_peak_pm"] == 240
    assert row["trip_count_peak_pm_weekday"] == 1
    assert row["travel_time_seconds_mean_peak_pm_weekday"] == 240
    assert pd.isna(row["trip_count_peak_am"])


def test_period_columns_kept_when_a_stop_has_no_name(tmp_path):
    path = _write_feed(
        tmp_path / "feed.zip",
        [("R1", "1", 3)],
        [("R1", "WD", "t1")],
        [
            ("t1", "07:00:00", "07:00:00", "A", 1),
            ("t1", "07:05:00", "07:05:00", "B", 2),
        ],
    )
    unnamed = tmp_path / "unnamed.zip"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(unnamed, "w") as dst:
        for name in src.namelist():
            content = src.read(name).decode()
            if name == "stops.txt":
                content = content.replace("A,Stop A,", "A,,")
            dst.writestr(name, content)
    row = _edges(unnamed).loc[("A", "B", "R1")]
    assert pd.isna(row["from_stop_name"])
    assert row["trip_count_peak_am"] == 1
    assert row["travel_time_seconds_mean_peak_am"] == 300


# ---------------------------------------------------------------------------
# Hour options: checks and ranges that cross midnight
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("hour", "start", "end", "inside"),
    [
        (7, 7, 9, True),
        (9, 7, 9, False),
        (23, 22, 2, True),
        (1, 22, 2, True),
        (2, 22, 2, False),
        (12, 22, 2, False),
        (23, 22, 24, True),
        (23, 22, 0, True),
        (0, 22, 0, False),
    ],
)
def test_hour_in_range(hour, start, end, inside):
    assert bool(_gtfs.hour_in_range(hour, start, end)) is inside


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"hour_range": (7, 9)},
        {"hour_range": (22, 2)},
        {"hour_range": (np.int64(6), np.int64(10))},
        {"hour_band_size": 3},
        {"hour_band_size": 24},
        {"peak_periods": {"morning": (7, 10), "night": (22, 6)}},
    ],
)
def test_good_hour_options_pass(options):
    _gtfs.check_hour_options(**options)


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"hour_range": 7}, "two hours"),
        ({"hour_range": (7, 8, 9)}, "two hours"),
        ({"hour_range": (7.5, 9)}, "whole numbers"),
        ({"hour_range": (True, 9)}, "whole numbers"),
        ({"hour_range": (-1, 9)}, "from 0 to 23"),
        ({"hour_range": (24, 2)}, "from 0 to 23"),
        ({"hour_range": (7, 25)}, "from 0 to 24"),
        ({"hour_range": (0, 24)}, "same hour"),
        ({"hour_band_size": 0}, "equal bands"),
        ({"hour_band_size": 5}, "equal bands"),
        ({"hour_band_size": "1"}, "equal bands"),
        ({"peak_periods": {}}, "must be a dict"),
        ({"peak_periods": [(7, 9)]}, "must be a dict"),
        ({"peak_periods": {"rest_of_day": (7, 9)}}, "choose another name"),
        ({"peak_periods": {"am": (9, 7)}}, None),  # crosses midnight: fine
        ({"peak_periods": {"am": (7, 10), "pm": (9, 12)}}, "'am' and 'pm'"),
        ({"peak_periods": {"night": (22, 6), "early": (5, 7)}}, "05-06"),
    ],
)
def test_bad_hour_options_are_explained(options, match):
    if match is None:
        _gtfs.check_hour_options(**options)
        return
    with pytest.raises(ValueError, match=match):
        _gtfs.check_hour_options(**options)


def test_an_hour_range_may_cross_midnight(tmp_path):
    path = _write_feed(
        tmp_path / "night.zip",
        [("N1", "N1", 3)],
        [("N1", "WD", "late"), ("N1", "WD", "after"), ("N1", "WD", "day")],
        [
            ("late", "23:30:00", "23:30:00", "A", 1),
            ("late", "23:35:00", "23:35:00", "B", 2),
            ("after", "25:10:00", "25:10:00", "A", 1),  # 01:10 the next day
            ("after", "25:15:00", "25:15:00", "B", 2),
            ("day", "12:00:00", "12:00:00", "A", 1),
            ("day", "12:05:00", "12:05:00", "B", 2),
        ],
    )
    row = _edges(path, hour_range=(22, 2)).loc[("A", "B", "N1")]
    assert row["trip_count"] == 2
    assert row["hourly_trip_counts"] == "01-02:1|23-24:1"


def test_a_peak_period_may_cross_midnight():
    night = {"night": (22, 6)}
    assert _gtfs.classify_period(23 * 3600, night) == "night"
    assert _gtfs.classify_period(25 * 3600, night) == "night"  # 01:00
    assert _gtfs.classify_period(12 * 3600, night) == "rest_of_day"


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

    monkeypatch.setattr(_gtfs, "_build_stop_to_stop_trip_records", counting_build)
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
        *args, dataset_name="ds", calendar=t["calendar"], filter_geometry=AREA
    )
    pd.testing.assert_frame_equal(
        edges.reset_index(drop=True), standalone_edges.reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(schedule, standalone_schedule)


# ---------------------------------------------------------------------------
# Schedule: clipped like the edges
# ---------------------------------------------------------------------------
def test_schedule_is_clipped_to_the_edges(feed):
    _, edges, schedule = _gtfs.normalize_gtfs_feed(
        feed,
        dataset_name="ds",
        layer_id="ds",
        filter_geometry=AREA,
        include_schedule_table=True,
    )
    assert set(schedule["edge_id"]) == set(edges["edge_id"])
    assert "t5" not in set(schedule["trip_id"])  # X -> Y never touches the area
    assert "t4" in set(schedule["trip_id"])  # W -> E crosses it, as its edge does
    # t1: A-B, B-C; t2: A-B; t3: B-C; t4: W-E.
    assert len(schedule) == 5


def test_schedule_without_an_area_keeps_every_trip(feed):
    *_, schedule = _gtfs.normalize_gtfs_feed(
        feed, dataset_name="ds", layer_id="ds", include_schedule_table=True
    )
    assert set(schedule["trip_id"]) == {"t1", "t2", "t3", "t4", "t5"}


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
    """The columns kept with the default periods (route_type since 0.7.0)."""
    expected = [
        "edge_id", "from_node", "to_node", "route_id", "route_short_name",
        "route_long_name", "route_type", "trip_count", "travel_time_seconds_mean",
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


# ---------------------------------------------------------------------------
# Nodes: the stops the edges use
# ---------------------------------------------------------------------------
@pytest.fixture
def catalogue_feed(tmp_path):
    """A feed whose stops.txt lists more than its trips use.

    A, B, Q inside the area; W outside, ending an edge that crosses the
    boundary; U inside but served by no trip; ST a parent station
    (location_type 1), which trips never use; N inside, served only at
    23:00.
    """
    stops = {
        "A": (39.45, -0.42, 0),
        "B": (39.46, -0.41, 0),
        "Q": (39.47, -0.40, 0),
        "W": (39.47, -0.60, 0),
        "U": (39.48, -0.39, 0),
        "ST": (39.45, -0.42, 1),
        "N": (39.49, -0.38, 0),
    }
    with zipfile.ZipFile(tmp_path / "catalogue.zip", "w") as zf:
        zf.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,location_type\n"
            + "\n".join(
                f"{s},Stop {s},{la},{lo},{t}" for s, (la, lo, t) in stops.items()
            )
            + "\n",
        )
        zf.writestr("routes.txt", "route_id,route_short_name,route_type\nR1,1,3\n")
        zf.writestr("trips.txt", "route_id,service_id,trip_id\nR1,WD,t1\nR1,WD,t2\n")
        zf.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
            "sunday,start_date,end_date\nWD,1,1,1,1,1,0,0,20260101,20261231\n",
        )
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t1,07:00:00,07:00:00,A,1\nt1,07:05:00,07:05:00,B,2\n"
            "t1,07:20:00,07:20:00,W,3\n"
            "t2,23:00:00,23:00:00,Q,1\nt2,23:04:00,23:04:00,N,2\n",
        )
    return tmp_path / "catalogue.zip"


def _layers(feed, **kwargs):
    nodes, edges, _ = _gtfs.normalize_gtfs_feed(
        feed,
        dataset_name="ds",
        layer_id="ds",
        filter_geometry=AREA,
        include_schedule_table=False,
        **kwargs,
    )
    return nodes.set_index("node_id"), edges


@pytest.mark.parametrize(
    ("hour_range", "expected"),
    [
        (None, {"A": True, "B": True, "W": False, "Q": True, "N": True}),
        # Q and N are only served at 23:00: no edge, so no node.
        ((6, 22), {"A": True, "B": True, "W": False}),
    ],
)
def test_nodes_are_the_stops_the_edges_use(catalogue_feed, hour_range, expected):
    nodes, edges = _layers(catalogue_feed, hour_range=hour_range)
    assert nodes["in_area"].to_dict() == expected
    used = set(edges["from_node"]) | set(edges["to_node"])
    assert set(nodes.index) == used  # every node has an edge, every edge its nodes
    assert "U" not in nodes.index  # served by no trip
    assert "ST" not in nodes.index  # parent station


def test_nodes_without_an_area_are_all_inside(catalogue_feed):
    nodes, _ = _gtfs.normalize_gtfs_feed(
        catalogue_feed, dataset_name="ds", layer_id="ds", include_schedule_table=False
    )[:2]
    assert nodes["in_area"].all()
    assert set(nodes["node_id"]) == {"A", "B", "W", "Q", "N"}


def test_no_edges_no_nodes(tmp_path, catalogue_feed):
    far = box(10, 10, 11, 11)
    nodes, edges, _ = _gtfs.normalize_gtfs_feed(
        catalogue_feed,
        dataset_name="ds",
        layer_id="ds",
        filter_geometry=far,
        include_schedule_table=False,
    )
    assert edges.empty
    assert nodes.empty
    assert "in_area" in nodes.columns


def test_stops_layer_without_stop_ids_keeps_the_stops_in_the_area():
    stops = pd.DataFrame(
        {
            "stop_id": ["A", "W", "A"],
            "stop_name": ["a", "w", "a again"],
            "stop_lat": [39.45, 39.47, 39.45],
            "stop_lon": [-0.42, -0.60, -0.42],
        }
    )
    gdf = _gtfs.build_gtfs_stops_gdf(
        stops, dataset_name="ds", layer_id="ds", filter_geometry=AREA
    )
    assert gdf["node_id"].tolist() == ["A"]  # W outside, A only once
    assert gdf["in_area"].tolist() == [True]


# ---------------------------------------------------------------------------
# Edge ids: one per pair of stops and route
# ---------------------------------------------------------------------------
def test_edge_ids_are_unique_when_routes_share_a_pair_of_stops(feed):
    _, edges, schedule = _gtfs.normalize_gtfs_feed(
        feed,
        dataset_name="ds",
        layer_id="ds",
        filter_geometry=AREA,
        include_schedule_table=True,
    )
    assert edges["edge_id"].is_unique
    # B -> C is served by R1 (t1) and R2 (t3): two edges, two ids.
    b_to_c = edges[(edges["from_node"] == "B") & (edges["to_node"] == "C")]
    assert sorted(b_to_c["edge_id"]) == ["ds_R1_B_C", "ds_R2_B_C"]
    # Each schedule row joins exactly one edge, of its own route.
    joined = schedule.merge(edges[["edge_id", "route_id"]], on="edge_id", how="left")
    assert len(joined) == len(schedule)
    assert (joined["route_id_x"] == joined["route_id_y"]).all()


def test_edge_id_without_a_route_id():
    frame = pd.DataFrame(
        {"route_id": ["R1", None], "from_node": ["A", "A"], "to_node": ["B", "B"]}
    )
    assert _gtfs._edge_ids("ds", frame).tolist() == ["ds_R1_A_B", "ds__A_B"]


# ---------------------------------------------------------------------------
# Impossible segment times
# ---------------------------------------------------------------------------
@pytest.fixture
def feed_with_a_typo(tmp_path):
    """t2's A -> B "takes" 10 h (17:16 typed for 08:16); t3's B -> C exactly 3 h."""
    trips = [("R1", "WD", "t1"), ("R1", "WD", "t2"), ("R2", "WD", "t3")]
    trips.append(("R4", "WD", "t5"))
    stop_times = [
        ("t1", "07:00:00", "07:00:00", "A", 1),
        ("t1", "07:05:00", "07:05:00", "B", 2),
        ("t2", "08:10:00", "08:10:00", "A", 1),
        ("t2", "18:10:00", "18:10:00", "B", 2),
        ("t3", "14:00:00", "14:00:00", "B", 1),
        ("t3", "17:00:00", "17:00:00", "C", 2),
        # Outside the area: not counted in the warning.
        ("t5", "10:00:00", "10:00:00", "X", 1),
        ("t5", "23:00:00", "23:00:00", "Y", 2),
    ]
    routes = [("R1", "1", 3), ("R2", "2", 3), ("R4", "4", 3)]
    return _write_feed(tmp_path / "typo.zip", routes, trips, stop_times)


def test_impossible_segment_times_are_left_empty(feed_with_a_typo):
    with pytest.warns(UserWarning, match=r"ds: 1 segment\(s\).*more than 3 h"):
        _, edges, schedule = _gtfs.normalize_gtfs_feed(
            feed_with_a_typo,
            dataset_name="ds",
            layer_id="ds",
            filter_geometry=AREA,
            include_schedule_table=True,
        )
    times = schedule.set_index("trip_id")["travel_time_seconds"]
    assert np.isnan(times["t2"])
    assert times["t3"] == 3 * 3600  # exactly the limit is kept
    a_to_b = edges.set_index("edge_id").loc["ds_R1_A_B"]
    assert a_to_b["trip_count"] == 2  # the trip still ran
    assert a_to_b["travel_time_seconds_mean"] == 300  # from t1 only
    assert a_to_b["travel_time_seconds_max"] == 300
    assert "implausible_time" not in schedule.columns


def test_no_warning_without_impossible_times(feed):
    import warnings

    with warnings.catch_warnings():
        # Only multimodalpy's own warnings: older pandas releases warn about
        # numpy deprecations of their own.
        warnings.simplefilter("error", UserWarning)
        _gtfs.normalize_gtfs_feed(
            feed, dataset_name="ds", layer_id="ds", filter_geometry=AREA
        )


# ---------------------------------------------------------------------------
# Medians (documented, but not computed before 0.5.1)
# ---------------------------------------------------------------------------
@pytest.fixture
def feed_with_a_slow_trip(tmp_path):
    """A -> B: 5, 6 and 20 minutes, all in the morning peak."""
    trips = [("R1", "WD", f"t{i}") for i in (1, 2, 3)]
    stop_times = [
        ("t1", "07:00:00", "07:00:00", "A", 1),
        ("t1", "07:05:00", "07:05:00", "B", 2),
        ("t2", "07:30:00", "07:30:00", "A", 1),
        ("t2", "07:36:00", "07:36:00", "B", 2),
        ("t3", "08:00:00", "08:00:00", "A", 1),
        ("t3", "08:20:00", "08:20:00", "B", 2),
    ]
    return _write_feed(tmp_path / "slow.zip", [("R1", "1", 3)], trips, stop_times)


def test_median_travel_times_are_computed(feed_with_a_slow_trip):
    _, edges, _ = _gtfs.normalize_gtfs_feed(
        feed_with_a_slow_trip,
        dataset_name="ds",
        layer_id="ds",
        filter_geometry=AREA,
        include_schedule_table=False,
    )
    edge = edges.iloc[0]
    assert edge["travel_time_seconds_mean"] == pytest.approx((300 + 360 + 1200) / 3)
    assert edge["travel_time_seconds_median"] == 360  # not pulled by the slow trip
    assert edge["travel_time_seconds_median_peak_am"] == 360
    assert pd.isna(edge["travel_time_seconds_median_rest_of_day"])
    # Every median column the layer documents is there, under its short name too.
    rename = _gtfs.gtfs_edge_rename()
    medians = [c for c in _gtfs.gtfs_edge_columns() if "median" in c]
    assert set(medians) <= set(edges.columns)
    assert {rename[c] for c in medians} == {
        "tts_md",
        "tts_md_pam",
        "tts_md_ppm",
        "tts_md_rod",
    }


# ---------------------------------------------------------------------------
# Dataset names: stable from one download to the next
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("20261119_030028_GenValenciana_Interurbano.zip", "genvalenciana_interurbano"),
        ("20260727_080002_BUS_Samar.zip", "bus_samar"),
        ("EMT Valencia.zip", "emt_valencia"),  # no timestamp: kept
        ("20261119_030028_.zip", "20261119_030028"),  # nothing else: kept
    ],
)
def test_dataset_name_drops_the_nap_timestamp(file_name, expected):
    assert _gtfs.dataset_name_from_file(file_name) == expected


def test_dataset_names_that_clash_are_numbered():
    taken: set[str] = set()
    names = [
        _gtfs.dataset_name_from_file(name, taken)
        for name in (
            "20261119_050013_RENFE_CERCA.zip",
            "20261120_050013_RENFE_CERCA.zip",
            "20261121_050013_RENFE_CERCA.zip",
        )
    ]
    assert names == ["renfe_cerca", "renfe_cerca_2", "renfe_cerca_3"]


def test_download_gtfs_uses_the_stable_names(tmp_path, monkeypatch):
    import geopandas as gpd

    trips = [("R1", "WD", "t1")]
    stop_times = [
        ("t1", "07:00:00", "07:00:00", "A", 1),
        ("t1", "07:05:00", "07:05:00", "B", 2),
    ]
    feed = _write_feed(
        tmp_path / "20261119_030028_GenValenciana_Interurbano.zip",
        [("R1", "1", 3)],
        trips,
        stop_times,
    )
    monkeypatch.setattr(_gtfs, "download_gtfs_nap_zips", lambda *a, **k: [feed])
    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    results = _gtfs.download_gtfs(
        "Valencia", boundary, tmp_path / "zips", modes=[1], api_key="key"
    )
    assert list(results) == ["genvalenciana_interurbano"]
    edges = results["genvalenciana_interurbano"]["edges"]
    assert edges["edge_id"].tolist() == ["genvalenciana_interurbano_R1_A_B"]


# ---------------------------------------------------------------------------
# Report: what happened to each downloaded file
# ---------------------------------------------------------------------------
def test_download_gtfs_completes_the_report(tmp_path, monkeypatch):
    import geopandas as gpd

    trips = [("R1", "WD", "t1")]
    inside = [
        ("t1", "07:00:00", "07:00:00", "A", 1),
        ("t1", "07:05:00", "07:05:00", "B", 2),
    ]
    outside = [
        ("t1", "07:00:00", "07:00:00", "X", 1),
        ("t1", "07:05:00", "07:05:00", "Y", 2),
    ]
    bus = _write_feed(tmp_path / "bus.zip", [("R1", "1", 3)], trips, inside)
    far = _write_feed(tmp_path / "far.zip", [("R1", "1", 3)], trips, outside)
    train = _write_feed(tmp_path / "train.zip", [("R1", "", 2)], trips, inside)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    files = [bus, far, train, broken]

    def fake_zips(area_name, output_dir, *, report=None, **kwargs):
        # Like the NAP client: one row per downloaded file, with its path.
        for i, path in enumerate(files):
            report.append({"dataset_id": i, "status": "downloaded", "path": str(path)})
        return files

    monkeypatch.setattr(_gtfs, "download_gtfs_nap_zips", fake_zips)
    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    report: list[dict] = []
    results = _gtfs.download_gtfs(
        "Valencia", boundary, tmp_path / "zips", modes=[1], api_key="k", report=report
    )
    rows = {row["dataset"]: row for row in report}
    assert rows["bus"]["status"] == "written"
    assert (rows["bus"]["routes"], rows["bus"]["stops"], rows["bus"]["edges"]) == (
        1,
        2,
        1,
    )
    assert rows["far"]["status"] == "nothing_in_area"
    assert rows["train"]["status"] == "skipped_mode"
    assert rows["train"]["mode"] == "train"
    assert rows["broken"]["status"] == "failed_processing"
    assert rows["broken"]["error"]
    assert len(report) == 4  # completed in place, not appended
    assert set(results) == {"bus", "far"}


# ---------------------------------------------------------------------------
# Trips defined by frequencies.txt
# ---------------------------------------------------------------------------
def _frequency_feed(tmp_path, frequencies, template_start="06:00:00"):
    """Route R1: one template trip A -> B (5 min) -> C (4 min), plus t2."""
    start = _gtfs.gtfs_time_to_seconds(template_start)
    times = [_gtfs.seconds_to_gtfs_time(start + d) for d in (0, 300, 540)]
    stop_times = [
        ("tf", times[0], times[0], "A", 1),
        ("tf", times[1], times[1], "B", 2),
        ("tf", times[2], times[2], "C", 3),
        ("t2", "12:00:00", "12:00:00", "A", 1),
        ("t2", "12:05:00", "12:05:00", "B", 2),
    ]
    return _write_feed(
        tmp_path / "freq.zip",
        [("R1", "1", 3)],
        [("R1", "WD", "tf"), ("R1", "WD", "t2")],
        stop_times,
        extra={
            "frequencies.txt": "trip_id,start_time,end_time,headway_secs\n"
            + "".join(f"{row}\n" for row in frequencies)
        },
    )


def test_frequency_trips_count_every_departure(tmp_path):
    feed = _frequency_feed(tmp_path, ["tf,07:00:00,08:00:00,1200"])
    _, edges, schedule = _gtfs.normalize_gtfs_feed(
        feed, dataset_name="ds", layer_id="ds", filter_geometry=AREA
    )
    ab = edges.set_index(["from_node", "to_node"]).loc[("A", "B")]
    # 07:00, 07:20 and 07:40 (08:00 is the end, not a departure), plus t2.
    assert ab["trip_count"] == 4
    assert ab["trip_count_peak_am"] == 3
    assert ab["trip_count_rest_of_day"] == 1
    assert ab["travel_time_seconds_mean"] == 300
    rows = schedule[(schedule["from_node"] == "A") & (schedule["to_node"] == "B")]
    assert sorted(rows["trip_id"]) == [
        "t2",
        "tf@07:00:00",
        "tf@07:20:00",
        "tf@07:40:00",
    ]
    by_trip = rows.set_index("trip_id")
    assert by_trip.loc["tf@07:20:00", "departure_time"] == "07:20:00"
    assert by_trip.loc["tf@07:20:00", "travel_time_seconds"] == 300


def test_frequency_trips_keep_the_template_times_between_stops(tmp_path):
    # The template starts at 00:00; several windows; one past midnight.
    feed = _frequency_feed(
        tmp_path,
        ["tf,08:00:00,08:30:00,900", "tf,23:50:00,24:10:00,1200"],
        template_start="00:00:00",
    )
    _, edges, schedule = _gtfs.normalize_gtfs_feed(
        feed, dataset_name="ds", layer_id="ds"
    )
    bc = schedule[(schedule["from_node"] == "B") & (schedule["to_node"] == "C")]
    # 08:00 and 08:15; 23:50 (24:10 is the end), all leaving B 5 min later.
    assert sorted(bc["departure_time"]) == ["08:05:00", "08:20:00", "23:55:00"]
    assert set(bc["travel_time_seconds"]) == {240}


def test_unusable_frequency_rows_leave_the_template_as_a_trip(tmp_path):
    feed = _frequency_feed(
        tmp_path, ["tf,07:00:00,08:00:00,0", "tf,09:00:00,08:00:00,600"]
    )
    edges = _edges(feed)
    assert edges.loc[("A", "B", "R1"), "trip_count"] == 2  # tf and t2


def test_seconds_to_gtfs_time():
    assert _gtfs.seconds_to_gtfs_time(0) == "00:00:00"
    assert _gtfs.seconds_to_gtfs_time(25 * 3600 + 61) == "25:01:01"
    assert _gtfs.seconds_to_gtfs_time(float("nan")) is None


# ---------------------------------------------------------------------------
# Service date: the trips that run on one day
# ---------------------------------------------------------------------------
CALENDAR = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
    "start_date,end_date\n"
    "WD,1,1,1,1,1,0,0,20260101,20261231\n"
    "SAT,0,0,0,0,0,1,0,20260101,20261231\n"
    "SUMMER,1,1,1,1,1,0,0,20260701,20260831\n"
)
# 8 December 2026 (a Tuesday) is a holiday: the weekday service doesn't run
# and the Saturday one does; 9 December has an extra service.
CALENDAR_DATES = (
    "service_id,date,exception_type\nWD,20261208,2\nSAT,20261208,1\nEXTRA,20261209,1\n"
)


@pytest.fixture
def seasonal_feed(tmp_path):
    trips = [
        ("R1", "WD", "wd1"),
        ("R1", "WD", "wd2"),
        ("R1", "SAT", "sat"),
        ("R1", "SUMMER", "sum"),
        ("R1", "EXTRA", "extra"),
    ]
    departures = {
        "wd1": "07:00:00",
        "wd2": "08:30:00",
        "sat": "10:00:00",
        "sum": "07:30:00",
        "extra": "12:00:00",
    }
    stop_times = []
    for trip, dep in departures.items():
        arrival = _gtfs.seconds_to_gtfs_time(_gtfs.gtfs_time_to_seconds(dep) + 300)
        stop_times += [(trip, dep, dep, "A", 1), (trip, arrival, arrival, "B", 2)]
    return _write_feed(
        tmp_path / "seasonal.zip",
        [("R1", "1", 3)],
        trips,
        stop_times,
        extra={"calendar.txt": CALENDAR, "calendar_dates.txt": CALENDAR_DATES},
    )


@pytest.mark.parametrize(
    "value",
    ["2026-11-25", "20261125", pd.Timestamp("2026-11-25 10:00").to_pydatetime()],
)
def test_parse_service_date(value):
    import datetime

    assert _gtfs.parse_service_date(value) == datetime.date(2026, 11, 25)


@pytest.mark.parametrize("value", ["25/11/2026", "2026-13-01", "tomorrow"])
def test_parse_service_date_rejects_other_formats(value):
    with pytest.raises(ValueError, match="gtfs_service_date"):
        _gtfs.parse_service_date(value)


def _calendar(*rows):
    """calendar.txt rows: (service_id, "1111100", start, end)."""
    return pd.DataFrame(
        [
            {
                "service_id": sid,
                **dict(zip(_gtfs._WEEKDAY_COLUMNS, days, strict=True)),
                "start_date": start,
                "end_date": end,
            }
            for sid, days, start, end in rows
        ]
    ).astype(str)


def _calendar_dates(*rows):
    """calendar_dates.txt rows: (service_id, date, exception_type)."""
    return pd.DataFrame(rows, columns=["service_id", "date", "exception_type"]).astype(
        str
    )


def test_service_date_counts_follow_the_gtfs_reference():
    calendar = _calendar(
        ("WD", "1111100", "20260105", "20260111"),  # Mon 5 - Sun 11 January
        ("SAT", "0000010", "20260101", "20260131"),
        ("STUB", "0000000", "20260101", "20261231"),  # dates only in calendar_dates
        ("BAD", "1111111", "2026-01-01", "20260131"),  # unreadable start: no dates
    )
    calendar_dates = _calendar_dates(
        ("WD", "20260106", 2),  # holiday removed
        ("WD", "20260110", 1),  # a Saturday added
        ("WD", "20260107", 1),  # already in the pattern: counts once
        ("WD", "20260201", 2),  # removed but never in the pattern: no effect
        ("STUB", "20260301", 1),
        ("STUB", "20260302", 1),
        ("ONLY", "20260401", 1),  # not in calendar.txt at all
    )
    counts = _gtfs.service_date_counts(calendar, calendar_dates).to_dict()
    assert counts == {"WD": 5, "SAT": 5, "STUB": 2, "ONLY": 1}


def test_service_date_counts_without_calendar_files():
    assert _gtfs.service_date_counts(None, None).empty
    only_dates = _calendar_dates(("X", "20260101", 1), ("X", "20260102", 1))
    assert _gtfs.service_date_counts(None, only_dates).to_dict() == {"X": 2}


def test_days_active_is_the_number_of_service_dates(feed):
    _, _, schedule = _gtfs.normalize_gtfs_feed(feed, dataset_name="ds", layer_id="ds")
    # WD runs Monday to Friday through 2026: 261 dates, not 5 days a week.
    assert set(schedule["days_active"]) == {261}


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2026-11-25", {"WD"}),  # a Wednesday
        ("2026-11-28", {"SAT"}),  # a Saturday
        ("2026-11-29", set()),  # a Sunday
        ("2026-07-15", {"WD", "SUMMER"}),
        ("2026-12-08", {"SAT"}),  # holiday: WD removed, SAT added
        ("2026-12-09", {"WD", "EXTRA"}),
        ("2027-01-13", set()),  # after the calendar ends
    ],
)
def test_active_service_ids(day, expected):
    calendar = pd.read_csv(pd.io.common.StringIO(CALENDAR), dtype=str)
    calendar_dates = pd.read_csv(pd.io.common.StringIO(CALENDAR_DATES), dtype=str)
    date = _gtfs.parse_service_date(day)
    assert _gtfs.active_service_ids(calendar, calendar_dates, date) == expected


def test_without_a_service_date_every_trip_counts(seasonal_feed):
    edge = _edges(seasonal_feed).loc[("A", "B", "R1")]
    assert edge["trip_count"] == 5
    assert edge["trip_count_weekday"] == 4  # wd1, wd2, sum and extra
    assert edge["trip_count_weekend"] == 1


@pytest.mark.parametrize(
    ("day", "trips", "weekday", "weekend"),
    [
        ("2026-11-25", 2, 2, None),
        ("2026-07-15", 3, 3, None),
        ("2026-11-28", 1, None, 1),
        ("2026-12-08", 1, 1, None),  # a Tuesday, with the Saturday service
    ],
)
def test_a_service_date_counts_the_trips_of_that_day(
    seasonal_feed, day, trips, weekday, weekend
):
    edge = _edges(seasonal_feed, service_date=_gtfs.parse_service_date(day)).loc[
        ("A", "B", "R1")
    ]
    assert edge["trip_count"] == trips
    assert edge["trip_count_weekday"] == weekday or (
        weekday is None and pd.isna(edge["trip_count_weekday"])
    )
    assert edge["trip_count_weekend"] == weekend or (
        weekend is None and pd.isna(edge["trip_count_weekend"])
    )


def test_a_service_date_filters_the_schedule(seasonal_feed):
    _, _, schedule = _gtfs.normalize_gtfs_feed(
        seasonal_feed,
        dataset_name="ds",
        layer_id="ds",
        service_date=_gtfs.parse_service_date("2026-07-15"),
    )
    assert sorted(schedule["trip_id"]) == ["sum", "wd1", "wd2"]


def test_a_service_date_applies_to_frequency_trips(tmp_path):
    feed = _frequency_feed(tmp_path, ["tf,07:00:00,08:00:00,1200"])
    sunday = _gtfs.parse_service_date("2026-11-29")
    wednesday = _gtfs.parse_service_date("2026-11-25")
    assert _edges(feed, service_date=wednesday).loc[("A", "B", "R1"), "trip_count"] == 4
    assert _edges(feed, service_date=sunday).empty


def test_download_gtfs_reports_feeds_without_service_that_day(
    tmp_path, monkeypatch, seasonal_feed
):
    import geopandas as gpd

    monkeypatch.setattr(
        _gtfs, "download_gtfs_nap_zips", lambda *a, **k: [seasonal_feed]
    )
    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    report: list[dict] = []
    results = _gtfs.download_gtfs(
        "Valencia",
        boundary,
        tmp_path / "zips",
        modes=[1],
        api_key="k",
        report=report,
        service_date="2027-01-13",
    )
    assert results == {}
    assert report[0]["status"] == "no_service_on_date"
    assert report[0]["error"] == (
        "no trips on 2027-01-13; its calendar runs from 2026-01-01 to 2026-12-31"
    )
    results = _gtfs.download_gtfs(
        "Valencia",
        boundary,
        tmp_path / "zips",
        modes=[1],
        api_key="k",
        service_date="2026-11-25",
    )
    assert results["seasonal"]["edges"]["trip_count"].tolist() == [2]


# ---------------------------------------------------------------------------
# Feeds from outside the NAP (gtfs_feeds)
# ---------------------------------------------------------------------------
def _simple_feed(path, route="R1"):
    """One trip of ``route`` from A to B; feeds of different routes differ."""
    trips = [(route, "WD", "t1")]
    stop_times = [
        ("t1", "07:00:00", "07:00:00", "A", 1),
        ("t1", "07:05:00", "07:05:00", "B", 2),
    ]
    return _write_feed(path, [(route, "1", 3)], trips, stop_times)


def test_user_feed_sources_accepts_every_form(tmp_path):
    local = _simple_feed(tmp_path / "local.zip")
    url = "https://gvinterbus.gva.es/estatico/gtfs.zip"
    assert _gtfs.user_feed_sources(None) == []
    assert _gtfs.user_feed_sources({"metrobus": url}) == [("metrobus", url)]
    assert _gtfs.user_feed_sources([url, local, ("mine", local)]) == [
        (None, url),
        (None, str(local)),
        ("mine", str(local)),
    ]
    assert _gtfs.user_feed_sources(url) == [(None, url)]
    assert _gtfs.user_feed_sources(tmp_path) == [(None, str(tmp_path))]  # a folder


@pytest.mark.parametrize(
    ("feeds", "error", "match"),
    [
        (["missing.zip"], FileNotFoundError, "missing.zip"),
        (["ftp://example.org/gtfs.zip"], ValueError, "http"),
        ([""], ValueError, "empty source"),
        ({"": "https://example.org/gtfs.zip"}, ValueError, "name must be text"),
    ],
)
def test_user_feed_sources_rejects_mistakes(feeds, error, match):
    with pytest.raises(error, match=match):
        _gtfs.user_feed_sources(feeds)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("https://gvinterbus.gva.es/estatico/gtfs.zip", "gtfs.zip"),
        ("https://example.org/feeds/metro.zip?key=1", "metro.zip"),
        ("https://example.org/", "gtfs"),
        ("/data/feeds/emt", "emt"),
    ],
)
def test_feed_file_name(source, expected):
    assert _gtfs._feed_file_name(source) == expected


class _Response:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        import requests

        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Not Found")


@pytest.fixture
def user_feeds(tmp_path, monkeypatch):
    """Two feeds served at addresses, an address that fails and a local feed."""
    import requests

    served = {
        "/gtfs.zip": _simple_feed(tmp_path / "served.zip", route="R9").read_bytes(),
        "/emt.zip": _simple_feed(tmp_path / "emt_served.zip").read_bytes(),
    }
    asked: list[str] = []

    def fake_get(url, **kwargs):
        asked.append(url)
        for ending, content in served.items():
            if url.endswith(ending):
                return _Response(content)
        return _Response(status=404)

    monkeypatch.setattr(requests, "get", fake_get)
    (tmp_path / "local").mkdir()
    return {
        "local": _simple_feed(tmp_path / "local" / "emt.zip"),
        "url": "https://gvinterbus.gva.es/estatico/gtfs.zip",
        "emt": "https://example.org/emt.zip",
        "broken": "https://example.org/gone.zip",
        "asked": asked,
    }


def test_download_gtfs_with_user_feeds_only(tmp_path, monkeypatch, user_feeds):
    import geopandas as gpd

    def no_nap(*args, **kwargs):
        raise AssertionError("the NAP must not be asked")

    monkeypatch.setattr(_gtfs, "download_gtfs_nap_zips", no_nap)
    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    report: list[dict] = []
    results = _gtfs.download_gtfs(
        "Valencia",
        boundary,
        tmp_path / "zips",
        modes=[1],
        report=report,
        use_nap=False,
        feeds=[
            ("MetroBus", user_feeds["url"]),
            user_feeds["emt"],
            user_feeds["broken"],
        ],
    )
    assert set(results) == {"metrobus", "emt"}
    assert results["metrobus"]["edges"]["edge_id"].tolist() == ["metrobus_R9_A_B"]
    assert (tmp_path / "zips" / "metrobus.zip").is_file()
    rows = {row["dataset_name"]: row for row in report}
    assert {row["source"] for row in report} == {"user"}
    assert rows["MetroBus"]["status"] == "written"
    assert rows["MetroBus"]["file_name"] == "gtfs.zip"
    assert rows["emt.zip"]["status"] == "written"
    assert rows["gone.zip"]["status"] == "failed"
    assert "404" in rows["gone.zip"]["error"]


def test_user_feeds_come_after_the_nap_and_never_take_its_names(
    tmp_path, monkeypatch, user_feeds
):
    import geopandas as gpd

    nap_feed = _simple_feed(tmp_path / "20261119_030028_EMT.zip", route="R5")

    def fake_zips(area_name, output_dir, *, report=None, **kwargs):
        report.append(
            {"dataset_id": 965, "status": "downloaded", "path": str(nap_feed)}
        )
        return [nap_feed]

    monkeypatch.setattr(_gtfs, "download_gtfs_nap_zips", fake_zips)
    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    report: list[dict] = []
    results = _gtfs.download_gtfs(
        "Valencia",
        boundary,
        tmp_path / "zips",
        modes=[1],
        api_key="k",
        report=report,
        feeds=[user_feeds["emt"], user_feeds["url"]],
    )
    assert list(results) == ["emt", "emt_2", "gtfs"]
    assert [(row["source"], row["dataset"]) for row in report] == [
        ("nap", "emt"),
        ("user", "emt_2"),
        ("user", "gtfs"),
    ]


def test_a_user_feed_does_not_overwrite_a_nap_zip(tmp_path, monkeypatch):
    import requests

    class Answer:
        content = b"user feed"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Answer())
    nap_zip = tmp_path / "emt.zip"
    nap_zip.write_bytes(b"nap feed")
    fetched = _gtfs._fetch_user_feeds(
        [("emt", "https://example.org/gtfs.zip")], tmp_path, [], taken={"emt"}
    )
    assert [path.name for path, _, _ in fetched] == ["emt_2.zip"]
    assert nap_zip.read_bytes() == b"nap feed"


def test_a_user_feed_that_repeats_a_nap_dataset_is_skipped(tmp_path, monkeypatch):
    """The same routes and stops in the area: not written twice."""
    import geopandas as gpd
    import requests

    nap_feed = _simple_feed(tmp_path / "20261119_030028_genvalenciana.zip")
    # The same network, exported another day: other trips, same routes and stops.
    copy = _write_feed(
        tmp_path / "copy.zip",
        [("R1", "1", 3)],
        [("R1", "WD", "other_trip")],
        [
            ("other_trip", "09:00:00", "09:00:00", "A", 1),
            ("other_trip", "09:06:00", "09:06:00", "B", 2),
        ],
    )
    different = _simple_feed(tmp_path / "different.zip", route="R2")

    class Answer:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            return None

    served = {"gtfs.zip": copy, "mine.zip": different}
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **kw: Answer(served[url.rsplit("/", 1)[1]].read_bytes()),
    )

    def fake_zips(area_name, output_dir, *, report=None, **kwargs):
        report.append(
            {"dataset_id": 1325, "status": "downloaded", "path": str(nap_feed)}
        )
        return [nap_feed]

    monkeypatch.setattr(_gtfs, "download_gtfs_nap_zips", fake_zips)
    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    report: list[dict] = []
    results = _gtfs.download_gtfs(
        "Valencia",
        boundary,
        tmp_path / "zips",
        modes=[1],
        api_key="k",
        report=report,
        feeds={
            "interurbano": "https://example.org/gtfs.zip",
            "mine": "https://example.org/mine.zip",
        },
    )
    assert list(results) == ["genvalenciana", "mine"]
    rows = {row["dataset"]: row for row in report}
    assert rows["interurbano"]["status"] == "duplicate"
    assert rows["interurbano"]["error"] == (
        "same routes and stops in the area as genvalenciana"
    )
    assert rows["mine"]["status"] == "written"


def test_nap_datasets_are_never_skipped_as_duplicates(tmp_path, monkeypatch):
    import geopandas as gpd

    first = _simple_feed(tmp_path / "a.zip")
    second = _simple_feed(tmp_path / "b.zip")

    def fake_zips(area_name, output_dir, *, report=None, **kwargs):
        for number, path in enumerate([first, second]):
            report.append(
                {"dataset_id": number, "status": "downloaded", "path": str(path)}
            )
        return [first, second]

    monkeypatch.setattr(_gtfs, "download_gtfs_nap_zips", fake_zips)
    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    results = _gtfs.download_gtfs(
        "Valencia", boundary, tmp_path / "zips", modes=[1], api_key="k", report=[]
    )
    assert list(results) == ["a", "b"]


def test_edge_network_key():
    edges = pd.DataFrame(
        {"route_id": ["R1", "R1"], "from_node": ["A", "B"], "to_node": ["B", "C"]}
    )
    assert _gtfs.edge_network_key(edges) == {("R1", "A", "B"), ("R1", "B", "C")}
    assert _gtfs.edge_network_key(edges.iloc[::-1]) == _gtfs.edge_network_key(edges)
    assert _gtfs.edge_network_key(edges.iloc[:0]) == frozenset()


def test_edges_keep_their_route_type(tmp_path):
    """A feed that mixes modes (metro and bus here) keeps each edge's type."""
    path = _write_feed(
        tmp_path / "mixed.zip",
        [("M1", "M1", 1), ("B1", "B1", 3)],
        [("M1", "WD", "m"), ("B1", "WD", "b")],
        [
            ("m", "07:00:00", "07:00:00", "A", 1),
            ("m", "07:03:00", "07:03:00", "B", 2),
            ("b", "08:00:00", "08:00:00", "B", 1),
            ("b", "08:06:00", "08:06:00", "C", 2),
        ],
    )
    edges = _edges(path)
    assert str(edges["route_type"].dtype) == "Int64"
    assert edges.loc[("A", "B", "M1"), "route_type"] == 1
    assert edges.loc[("B", "C", "B1"), "route_type"] == 3


def test_stops_keep_their_station(tmp_path):
    path = _write_feed(
        tmp_path / "feed.zip",
        [("R1", "1", 1)],
        [("R1", "WD", "t1")],
        [
            ("t1", "07:00:00", "07:00:00", "A", 1),
            ("t1", "07:05:00", "07:05:00", "B", 2),
        ],
    )
    with_station = tmp_path / "with_station.zip"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(with_station, "w") as dst:
        for name in src.namelist():
            content = src.read(name).decode()
            if name == "stops.txt":
                lines = content.strip().split("\n")
                lines[0] += ",parent_station,location_type"
                lines = [lines[0]] + [
                    line + (",ST1,0" if line.startswith("A,") else ",,0")
                    for line in lines[1:]
                ]
                lines.append("ST1,Central,39.5,-0.4,,1")
                content = "\n".join(lines) + "\n"
            dst.writestr(name, content)
    nodes, _, _ = _gtfs.normalize_gtfs_feed(
        with_station, dataset_name="ds", layer_id="ds"
    )
    station = nodes.set_index("node_id")["station_id"]
    assert station["A"] == "ST1"
    assert pd.isna(station["B"])
    assert "ST1" not in station.index  # the station itself is no stop of an edge
    plain, _, _ = _gtfs.normalize_gtfs_feed(path, dataset_name="ds", layer_id="ds")
    assert plain["station_id"].isna().all()  # no parent_station column at all


def test_trip_counts_are_whole_numbers(feed):
    edges = _edges(feed)
    counts = [col for col in edges.columns if col.startswith("trip_count")]
    assert len(counts) > 10
    assert {str(edges[col].dtype) for col in counts} == {"Int64"}
    assert pd.isna(edges.loc[("B", "C", "R2"), "trip_count_peak_am"])


def test_the_report_counts_the_stops_in_the_area(tmp_path, feed, monkeypatch):
    import geopandas as gpd
    import requests

    monkeypatch.setattr(requests, "get", lambda url, **kw: _Response(feed.read_bytes()))
    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    report: list[dict] = []
    results = _gtfs.download_gtfs(
        "Valencia",
        boundary,
        tmp_path / "zips",
        modes=[1],
        report=report,
        use_nap=False,
        feeds={"ds": "https://example.org/feed.zip"},
    )
    nodes = results["ds"]["nodes"]
    assert not nodes["in_area"].all()  # an edge ends at a stop outside the area
    assert report[0]["stops"] == int(nodes["in_area"].sum())


def test_process_gtfs_feeds_works_offline(tmp_path, feed, monkeypatch):
    import geopandas as gpd
    import requests

    def no_network(*args, **kwargs):
        raise AssertionError("process_gtfs_feeds must not touch the network")

    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "post", no_network)
    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    report: list[dict] = []
    results = _gtfs.process_gtfs_feeds(
        [(feed, "metro", None)], boundary, modes=[1], report=report
    )
    assert list(results) == ["metro"]
    assert report[0]["dataset"] == "metro"
    assert report[0]["status"] == "written"
    assert report[0]["file_name"] == feed.name


def test_download_gtfs_refuses_local_feeds(tmp_path, user_feeds):
    import geopandas as gpd

    boundary = gpd.GeoDataFrame(geometry=[AREA], crs="EPSG:4326")
    with pytest.raises(ValueError, match="build_network"):
        _gtfs.download_gtfs(
            "Valencia",
            boundary,
            tmp_path / "zips",
            modes=[1],
            use_nap=False,
            feeds=[user_feeds["url"], user_feeds["local"]],
        )
    assert user_feeds["asked"] == []  # refused before downloading anything
