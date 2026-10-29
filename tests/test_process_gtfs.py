"""Tests for GTFS normalization (process_gtfs), including the fast paths.

The feeds are tiny zips written to ``tmp_path``, so no network is needed.
"""

from __future__ import annotations

import zipfile

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from multimodalpy import process_gtfs

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
    _, edges, _ = process_gtfs.normalize_gtfs_feed(
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

    packed = process_gtfs._pack_by_group(rows, ["g1", "g2"], "band", "value")
    # NaN keys can't be looked up with .loc, so compare as plain dicts.
    actual = {
        tuple(None if pd.isna(part) else part for part in key): text
        for key, text in packed.items()
    }

    expected = {}
    for key, group in rows.groupby(["g1", "g2"], dropna=False):
        text = process_gtfs._pack_key_value(group.set_index("band")["value"])
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
