"""GTFS data standardization. Cleans bus + train data.

This module has two parts:

1. Standardization: turns a GTFS feed (zip or folder) into GeoPandas
   layers -- ``nodes_stops`` (stops as points), ``edges`` (stop-to-stop
   edges with travel time and an hour-band breakdown),
   ``edges_shapes_reference`` (route geometry from shapes.txt), and
   ``schedule`` (a flat, non-spatial table with one record per trip and
   segment, for anyone who needs the full schedule detail without losing
   granularity).
2. Bus + train cleanup: snapping stops onto the route geometry and
   splitting the route lines into segments between consecutive stops, to
   get a coherent stop-segment-stop topology.

No file is written here; that's done by ``get_network``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import BadZipFile, ZipFile

if TYPE_CHECKING:
    import geopandas as gpd
    import pandas as pd


SOURCE_CRS = "EPSG:4326"

# Default peak periods for the option A breakdown.
# "rest_of_day" is automatically assigned to any hour not covered here.
DEFAULT_PEAK_PERIODS: dict[str, tuple[int, int]] = {
    "peak_am": (7, 9),  # morning peak, 07:00-09:00
    "peak_pm": (17, 20),  # evening peak, 17:00-20:00
}


# ---------------------------------------------------------------------------
# Reading GTFS tables
# ---------------------------------------------------------------------------
def _strip_gtfs_whitespace(df: pd.DataFrame) -> pd.DataFrame:
    """Strip padding whitespace from a GTFS table's headers and values.

    Some official feeds publish their CSVs with columns padded to a fixed
    width (e.g. RENFE's Cercanias feed, whose literal header is
    ``"stop_sequence            ..."`` and whose values look like
    ``"005        ..."``). Without trimming, any access by column name
    breaks with a KeyError and identifiers don't match across tables.
    """
    from pandas.api.types import is_object_dtype, is_string_dtype

    df.columns = [str(column).strip() for column in df.columns]
    for column in df.columns:
        series = df[column]
        # Tables are read with dtype=str, but the concrete dtype depends
        # on the pandas version ("object" up to 2.x, "str" from 3.0).
        if is_string_dtype(series) or is_object_dtype(series):
            df[column] = series.str.strip()
    return df


def read_gtfs_table(feed_path: str | Path, table_name: str) -> pd.DataFrame:
    """Read a GTFS table from a zip file or an extracted GTFS folder."""
    import pandas as pd

    feed_path = Path(feed_path)
    if feed_path.is_dir():
        return _strip_gtfs_whitespace(
            pd.read_csv(feed_path / table_name, dtype=str, low_memory=False)
        )

    with ZipFile(feed_path) as zf:
        if table_name not in zf.namelist():
            raise FileNotFoundError(f"{table_name} not found in {feed_path}")
        with zf.open(table_name) as file:
            return _strip_gtfs_whitespace(
                pd.read_csv(file, dtype=str, low_memory=False)
            )


# GTFS spec ``route_type``. Rail-based modes are tram (0), subway (1),
# rail (2), cable tram (5), funicular (7) and monorail (12); everything
# else (bus 3, trolleybus 11, bus rapid transit 700-799...) is treated as
# bus.
RAIL_ROUTE_TYPES: frozenset[int] = frozenset({0, 1, 2, 5, 7, 12})


def infer_transport_mode(feed_path: str | Path) -> str:
    """Return ``"train"`` or ``"bus"`` based on the feed's ``route_type`` values.

    Used to decide which folder a GTFS dataset's layers are saved under.
    The feed itself is checked rather than the NAP metadata, because there
    the same dataset can be declared as both bus and rail at once (e.g.
    Cercanias Renfe). If the feed mixes both, the majority type wins; if
    it can't be read, ``"bus"`` is assumed.
    """
    import pandas as pd

    try:
        routes = read_gtfs_table(feed_path, "routes.txt")
    except (OSError, ValueError, BadZipFile):
        return "bus"
    if "route_type" not in routes.columns:
        return "bus"

    route_types = pd.to_numeric(routes["route_type"], errors="coerce").dropna()
    if route_types.empty:
        return "bus"
    rail = int(route_types.isin(RAIL_ROUTE_TYPES).sum())
    return "train" if rail * 2 > len(route_types) else "bus"


def _read_optional_gtfs_table(
    feed_path: str | Path, table_name: str
) -> pd.DataFrame | None:
    try:
        return read_gtfs_table(feed_path, table_name)
    except FileNotFoundError:
        return None


def _existing_columns(df: pd.DataFrame, columns: list[str]) -> list[str]:
    return [column for column in columns if column in df.columns]


def _unique_join(values) -> str | None:
    clean = sorted(
        {str(value) for value in values if value is not None and str(value) != "nan"}
    )
    return "|".join(clean) if clean else None


def _filter_by_geometry(gdf: gpd.GeoDataFrame, filter_geometry) -> gpd.GeoDataFrame:
    if filter_geometry is None:
        return gdf
    return gdf[gdf.geometry.intersects(filter_geometry)].copy()


# ---------------------------------------------------------------------------
# GTFS times (allow values >= 24:00:00 for overnight services)
# ---------------------------------------------------------------------------
def gtfs_time_to_seconds(time_str) -> float:
    """Convert 'HH:MM:SS' (HH may be >= 24) to seconds since midnight.

    Returns NaN if the value is null or not in the expected format.
    """
    import math

    if time_str is None:
        return math.nan
    time_str = str(time_str).strip()
    if not time_str or time_str.lower() == "nan":
        return math.nan
    parts = time_str.split(":")
    if len(parts) != 3:
        return math.nan
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return math.nan
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_hour_band(seconds, *, band_size_hours: int = 1) -> str | None:
    """Group seconds-since-midnight into an hour band like '08-09'."""
    import math

    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return None
    hour = int((seconds // 3600) % 24)
    band_start = (hour // band_size_hours) * band_size_hours
    band_end = band_start + band_size_hours
    return f"{band_start:02d}-{band_end:02d}"


def classify_period(
    seconds, peak_periods: dict[str, tuple[int, int]] | None = None
) -> str | None:
    """Classify a moment (seconds since midnight) into a peak period.

    Peak periods are peak_am / peak_pm / rest_of_day, based on
    ``peak_periods`` (defaults to ``DEFAULT_PEAK_PERIODS``). Returns
    ``None`` if no time is available.
    """
    import math

    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return None
    peak_periods = peak_periods or DEFAULT_PEAK_PERIODS
    hour = int((seconds // 3600) % 24)
    for name, (start, end) in peak_periods.items():
        if start <= hour < end:
            return name
    return "rest_of_day"


_WEEKDAY_ORDER = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def _union_days_of_week(values) -> str | None:
    """Merge several day-of-week strings (one per trip) into a single summary.

    Ordered Monday to Sunday, with duplicates removed.
    """
    import math

    days: set[str] = set()
    for value in values:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        days.update(str(value).split("|"))
    days.discard("")
    if not days:
        return None
    return "|".join(day for day in _WEEKDAY_ORDER if day in days)


def _pack_key_value(pairs: pd.Series, *, decimals: int = 0) -> str | None:
    """Pack sorted (label, value) pairs into 'label:value|label:value'."""
    import math

    items = []
    for label, value in pairs.items():
        if label is None or (isinstance(value, float) and math.isnan(value)):
            continue
        items.append((str(label), round(float(value), decimals)))
    items.sort(key=lambda pair: pair[0])
    if not items:
        return None
    return "|".join(f"{label}:{value:g}" for label, value in items)


# ---------------------------------------------------------------------------
# Building the normalized layers
# ---------------------------------------------------------------------------
def build_gtfs_stops_gdf(
    stops: pd.DataFrame,
    *,
    dataset_name: str,
    layer_id: str,
    filter_geometry=None,
) -> gpd.GeoDataFrame:
    """Build the stops (nodes) layer from ``stops.txt``."""
    import geopandas as gpd
    import pandas as pd

    work = stops.copy()
    work["stop_lat"] = pd.to_numeric(work["stop_lat"], errors="coerce")
    work["stop_lon"] = pd.to_numeric(work["stop_lon"], errors="coerce")
    work = work.dropna(subset=["stop_lat", "stop_lon"]).copy()
    gdf = gpd.GeoDataFrame(
        work,
        geometry=gpd.points_from_xy(work["stop_lon"], work["stop_lat"]),
        crs=SOURCE_CRS,
    )
    gdf = _filter_by_geometry(gdf, filter_geometry)
    gdf["dataset_name"] = dataset_name
    gdf["layer_id"] = layer_id
    gdf["node_id"] = gdf["stop_id"]
    gdf["source_name"] = dataset_name
    gdf["node_type"] = "stop"
    return gdf


def _classify_day_type(runs_weekday, runs_weekend) -> str | None:
    """Combine the boolean activity flags into a readable label."""
    import pandas as pd

    wd = bool(runs_weekday) if pd.notna(runs_weekday) else False
    we = bool(runs_weekend) if pd.notna(runs_weekend) else False
    if wd and we:
        return "weekday_and_weekend"
    if wd:
        return "weekday"
    if we:
        return "weekend"
    return None


def _attach_service_days(
    trips: pd.DataFrame,
    calendar: pd.DataFrame | None,
    calendar_dates: pd.DataFrame | None,
) -> pd.DataFrame:
    """Add columns to ``trips`` describing which days the service runs.

    - ``days_active``: number of days/week with service (a count, as before).
    - ``active_days_of_week``: text like ``"friday|monday|thursday"`` with
      the specific days of the week the service is active on.
    - ``runs_weekday`` / ``runs_weekend``: booleans, ``True`` if the service
      runs on any weekday (Monday-Friday) / weekend day (Saturday-Sunday)
      respectively.
    - ``day_type``: summary label -- ``"weekday"``, ``"weekend"``,
      ``"weekday_and_weekend"`` or ``None`` if it can't be determined.

    The primary source is ``calendar.txt`` (the declared weekly pattern).
    If it doesn't exist, the days of the week are inferred from the dates
    added in ``calendar_dates.txt`` (``exception_type == 1``).
    """
    import pandas as pd

    weekday_cols = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    weekend_cols = ["saturday", "sunday"]
    all_day_cols = weekday_cols + weekend_cols

    if "service_id" not in trips.columns:
        trips = trips.copy()
        trips["days_active"] = pd.NA
        trips["active_days_of_week"] = pd.NA
        trips["runs_weekday"] = pd.NA
        trips["runs_weekend"] = pd.NA
        trips["day_type"] = pd.NA
        return trips

    trips = trips.copy()

    # --- Info declared in calendar.txt (if it exists) ---
    cal_days_active = pd.Series(dtype="float64")
    cal_dow = pd.Series(dtype="object")
    cal_runs_weekday = pd.Series(dtype="boolean")
    cal_runs_weekend = pd.Series(dtype="boolean")

    if calendar is not None and set(all_day_cols).issubset(calendar.columns):
        cal = calendar.copy()
        for col in all_day_cols:
            cal[col] = pd.to_numeric(cal[col], errors="coerce").fillna(0)
        cal["days_active"] = cal[all_day_cols].sum(axis=1)
        cal["active_days_of_week"] = cal[all_day_cols].apply(
            lambda row: "|".join(day for day in _WEEKDAY_ORDER if row[day] == 1), axis=1
        )
        cal["runs_weekday"] = cal[weekday_cols].sum(axis=1) > 0
        cal["runs_weekend"] = cal[weekend_cols].sum(axis=1) > 0
        cal = cal.drop_duplicates(subset=["service_id"]).set_index("service_id")
        cal_days_active = cal["days_active"]
        cal_dow = cal["active_days_of_week"]
        cal_runs_weekday = cal["runs_weekday"].astype("boolean")
        cal_runs_weekend = cal["runs_weekend"].astype("boolean")

    # Services with a real weekly pattern declared in calendar.txt (at
    # least one active day). Some feeds use calendar.txt as a "stub" with
    # all flags set to 0 and delegate the concrete dates to
    # calendar_dates.txt; those services are treated the same as if they
    # weren't in calendar.txt at all.
    strong_ids = set(
        cal_runs_weekday[
            cal_runs_weekday.fillna(False) | cal_runs_weekend.fillna(False)
        ].index
    )

    added_counts = pd.Series(dtype="float64")
    removed_counts = pd.Series(dtype="float64")
    inferred_dow = pd.Series(dtype="object")
    inferred_runs_weekday = pd.Series(dtype="boolean")
    inferred_runs_weekend = pd.Series(dtype="boolean")

    if calendar_dates is not None and "exception_type" in calendar_dates.columns:
        cd = calendar_dates.copy()
        cd["exception_type"] = pd.to_numeric(cd["exception_type"], errors="coerce")
        added_counts = cd[cd["exception_type"] == 1].groupby("service_id").size()
        removed_counts = cd[cd["exception_type"] == 2].groupby("service_id").size()

        # Days of the week inferred from the added dates
        # (exception_type == 1). Used for "weak" services: those absent
        # from calendar.txt, or present with all weekly flags at 0.
        if "date" in cd.columns:
            added_rows = cd[cd["exception_type"] == 1].copy()
            added_rows["weekday_name"] = (
                pd.to_datetime(added_rows["date"], format="%Y%m%d", errors="coerce")
                .dt.day_name()
                .str.lower()
            )
            added_rows = added_rows.dropna(subset=["weekday_name"])
            if not added_rows.empty:
                inferred_dow = added_rows.groupby("service_id")["weekday_name"].agg(
                    lambda names: "|".join(
                        day for day in _WEEKDAY_ORDER if day in set(names)
                    )
                )
                inferred_runs_weekday = (
                    added_rows.groupby("service_id")["weekday_name"]
                    .agg(lambda names: any(day in weekday_cols for day in names))
                    .astype("boolean")
                )
                inferred_runs_weekend = (
                    added_rows.groupby("service_id")["weekday_name"]
                    .agg(lambda names: any(day in weekend_cols for day in names))
                    .astype("boolean")
                )

    # ``days_active``: count declared in calendar.txt, plus the dates
    # added and minus the dates removed in calendar_dates.txt (if a
    # service is "weak" in calendar.txt, cal_days_active is 0 and the
    # result ends up as the plain count of dates from calendar_dates.txt).
    service_days = cal_days_active.add(added_counts, fill_value=0).sub(
        removed_counts, fill_value=0
    )
    service_days = (
        service_days.clip(lower=0) if not service_days.empty else service_days
    )

    # ``active_days_of_week`` / ``runs_weekday`` / ``runs_weekend``: for
    # "strong" services (a real weekly pattern in calendar.txt) that
    # pattern is respected. For the rest ("weak": absent from
    # calendar.txt, or present with all flags at 0), what's inferred from
    # calendar_dates.txt is used when available, and otherwise whatever
    # was in calendar.txt is kept (typically empty/False).
    all_ids = cal_dow.index.union(inferred_dow.index)
    if len(all_ids) == 0:
        service_dow = pd.Series(dtype="object")
        service_runs_weekday = pd.Series(dtype="boolean")
        service_runs_weekend = pd.Series(dtype="boolean")
    else:
        is_strong = pd.Series(all_ids.isin(strong_ids), index=all_ids)
        cal_dow_r = cal_dow.reindex(all_ids)
        cal_rw_r = cal_runs_weekday.reindex(all_ids)
        cal_rwe_r = cal_runs_weekend.reindex(all_ids)
        inf_dow_r = inferred_dow.reindex(all_ids)
        inf_rw_r = inferred_runs_weekday.reindex(all_ids)
        inf_rwe_r = inferred_runs_weekend.reindex(all_ids)

        service_dow = cal_dow_r.where(is_strong, inf_dow_r.combine_first(cal_dow_r))
        service_runs_weekday = cal_rw_r.where(
            is_strong, inf_rw_r.combine_first(cal_rw_r)
        )
        service_runs_weekend = cal_rwe_r.where(
            is_strong, inf_rwe_r.combine_first(cal_rwe_r)
        )

    trips["days_active"] = (
        pd.NA if service_days.empty else trips["service_id"].map(service_days)
    )
    trips["active_days_of_week"] = (
        pd.NA if service_dow.empty else trips["service_id"].map(service_dow)
    )
    trips["runs_weekday"] = (
        pd.NA
        if service_runs_weekday.empty
        else trips["service_id"].map(service_runs_weekday)
    )
    trips["runs_weekend"] = (
        pd.NA
        if service_runs_weekend.empty
        else trips["service_id"].map(service_runs_weekend)
    )
    trips["day_type"] = trips.apply(
        lambda row: _classify_day_type(
            row.get("runs_weekday"), row.get("runs_weekend")
        ),
        axis=1,
    )
    return trips


def _build_stop_to_stop_trip_records(
    stop_times: pd.DataFrame,
    stops: pd.DataFrame,
    trips: pd.DataFrame,
    routes: pd.DataFrame,
    *,
    dataset_name: str,
    calendar: pd.DataFrame | None = None,
    calendar_dates: pd.DataFrame | None = None,
    frequencies: pd.DataFrame | None = None,
    hour_band_size: int = 1,
    hour_range: tuple[int, int] | None = None,
    peak_periods: dict[str, tuple[int, int]] | None = None,
) -> pd.DataFrame:
    """Build the intermediate, unaggregated table of trip-segment records.

    One record per (trip, segment between consecutive stops). Reused by
    both the aggregated edges layer (``build_gtfs_stop_to_stop_edges_gdf``)
    and the flat schedule table (``build_gtfs_schedule_table``), to avoid
    duplicating the travel-time and merge logic.
    """
    import pandas as pd

    work = stop_times.copy()
    work["stop_sequence"] = pd.to_numeric(work["stop_sequence"], errors="coerce")
    work = work.dropna(subset=["trip_id", "stop_id", "stop_sequence"]).sort_values(
        ["trip_id", "stop_sequence"]
    )

    if "departure_time" in work.columns:
        work["departure_seconds"] = work["departure_time"].apply(gtfs_time_to_seconds)
    else:
        work["departure_seconds"] = float("nan")
    if "arrival_time" in work.columns:
        work["arrival_seconds"] = work["arrival_time"].apply(gtfs_time_to_seconds)
    else:
        work["arrival_seconds"] = work["departure_seconds"]

    work["next_stop_id"] = work.groupby("trip_id")["stop_id"].shift(-1)
    work["next_arrival_seconds"] = work.groupby("trip_id")["arrival_seconds"].shift(-1)
    work["next_arrival_time"] = (
        work.groupby("trip_id")["arrival_time"].shift(-1)
        if "arrival_time" in work.columns
        else None
    )
    work = work.dropna(subset=["next_stop_id"]).copy()

    work["travel_time_seconds"] = (
        work["next_arrival_seconds"] - work["departure_seconds"]
    )
    work.loc[work["travel_time_seconds"] < 0, "travel_time_seconds"] = float("nan")

    work["hour_band"] = work["departure_seconds"].apply(
        lambda s: seconds_to_hour_band(s, band_size_hours=hour_band_size)
    )
    work["period"] = work["departure_seconds"].apply(
        lambda s: classify_period(s, peak_periods)
    )

    if hour_range is not None:
        start_h, end_h = hour_range
        hour_of_day = (work["departure_seconds"] // 3600) % 24
        work = work[(hour_of_day >= start_h) & (hour_of_day < end_h)].copy()

    stop_lookup = stops[["stop_id", "stop_name", "stop_lat", "stop_lon"]].copy()
    stop_lookup["stop_lat"] = pd.to_numeric(stop_lookup["stop_lat"], errors="coerce")
    stop_lookup["stop_lon"] = pd.to_numeric(stop_lookup["stop_lon"], errors="coerce")
    stop_lookup = stop_lookup.dropna(subset=["stop_lat", "stop_lon"]).drop_duplicates(
        "stop_id"
    )

    from_lookup = stop_lookup.rename(
        columns={
            "stop_id": "from_node_id",
            "stop_name": "from_stop_name",
            "stop_lat": "from_stop_lat",
            "stop_lon": "from_stop_lon",
        }
    )
    to_lookup = stop_lookup.rename(
        columns={
            "stop_id": "to_node_id",
            "stop_name": "to_stop_name",
            "stop_lat": "to_stop_lat",
            "stop_lon": "to_stop_lon",
        }
    )

    edges = work.rename(
        columns={"stop_id": "from_node_id", "next_stop_id": "to_node_id"}
    ).copy()

    trips_work = _attach_service_days(trips, calendar, calendar_dates)

    if (
        frequencies is not None
        and not frequencies.empty
        and "trip_id" in frequencies.columns
    ):
        freq = frequencies.copy()
        freq["headway_secs"] = pd.to_numeric(freq["headway_secs"], errors="coerce")
        freq["start_seconds"] = freq["start_time"].apply(gtfs_time_to_seconds)
        freq["end_seconds"] = freq["end_time"].apply(gtfs_time_to_seconds)
        freq["trips_per_window"] = (
            (freq["end_seconds"] - freq["start_seconds"]) / freq["headway_secs"]
        ).clip(lower=0)
        freq_trip_counts = freq.groupby("trip_id")["trips_per_window"].sum()
        trips_work["frequency_trip_count"] = trips_work["trip_id"].map(freq_trip_counts)
    else:
        trips_work["frequency_trip_count"] = pd.NA

    trip_cols = _existing_columns(
        trips_work,
        [
            "trip_id",
            "route_id",
            "service_id",
            "shape_id",
            "trip_headsign",
            "days_active",
            "active_days_of_week",
            "runs_weekday",
            "runs_weekend",
            "day_type",
            "frequency_trip_count",
        ],
    )
    route_cols = _existing_columns(
        routes, ["route_id", "route_short_name", "route_long_name", "route_type"]
    )
    edges = edges.merge(
        trips_work[trip_cols].drop_duplicates(subset=["trip_id"]),
        on="trip_id",
        how="left",
    )
    if "route_id" in route_cols:
        edges = edges.merge(
            routes[route_cols].drop_duplicates(), on="route_id", how="left"
        )
    edges = edges.merge(from_lookup, on="from_node_id", how="left")
    edges = edges.merge(to_lookup, on="to_node_id", how="left")
    edges = edges.dropna(
        subset=["from_stop_lon", "from_stop_lat", "to_stop_lon", "to_stop_lat"]
    ).copy()
    edges["edge_id"] = (
        dataset_name
        + "_"
        + edges["from_node_id"].astype(str)
        + "_"
        + edges["to_node_id"].astype(str)
    )
    return edges


def build_gtfs_schedule_table(
    stop_times: pd.DataFrame,
    stops: pd.DataFrame,
    trips: pd.DataFrame,
    routes: pd.DataFrame,
    *,
    dataset_name: str,
    calendar: pd.DataFrame | None = None,
    calendar_dates: pd.DataFrame | None = None,
    frequencies: pd.DataFrame | None = None,
    hour_band_size: int = 1,
    hour_range: tuple[int, int] | None = None,
    peak_periods: dict[str, tuple[int, int]] | None = None,
) -> pd.DataFrame:
    """Option B: a flat table (no geometry), one record per trip and segment.

    Meant to be joined with the ``edges`` layer by ``edge_id`` when the
    full schedule detail is needed (every step of every trip), without
    losing that granularity in the aggregated spatial layer.
    """
    records = _build_stop_to_stop_trip_records(
        stop_times,
        stops,
        trips,
        routes,
        dataset_name=dataset_name,
        calendar=calendar,
        calendar_dates=calendar_dates,
        frequencies=frequencies,
        hour_band_size=hour_band_size,
        hour_range=hour_range,
        peak_periods=peak_periods,
    )
    columns = _existing_columns(
        records,
        [
            "edge_id",
            "trip_id",
            "route_id",
            "route_short_name",
            "trip_headsign",
            "from_node_id",
            "to_node_id",
            "departure_time",
            "arrival_time",
            "travel_time_seconds",
            "hour_band",
            "period",
            "service_id",
            "days_active",
            "active_days_of_week",
            "runs_weekday",
            "runs_weekend",
            "day_type",
        ],
    )
    return records[columns].reset_index(drop=True)


def _period_pivot_columns(
    rows: pd.DataFrame,
    group_cols: list[str],
    peak_periods: dict[str, tuple[int, int]] | None,
    *,
    suffix: str = "",
) -> pd.DataFrame:
    """Aggregate ``travel_time_seconds_mean`` / ``trip_count`` by period.

    Periods are peak_am / peak_pm / rest_of_day, pivoting ``period`` into
    columns like ``travel_time_seconds_mean_peak_am``. ``suffix`` is added
    to the end of each column name so this period breakdown can be
    combined with another breakdown (weekday/weekend). Always returns all
    period columns, even if empty, so the later merge stays consistent.
    """
    import pandas as pd

    all_period_names = list((peak_periods or DEFAULT_PEAK_PERIODS).keys()) + [
        "rest_of_day"
    ]
    period_rows = rows.dropna(subset=["period"])
    if not period_rows.empty:
        period_grouped = (
            period_rows.groupby(group_cols + ["period"], dropna=False)
            .agg(
                travel_time_seconds_mean=("travel_time_seconds", "mean"),
                trip_count=("trip_id", "nunique"),
            )
            .reset_index()
        )
        pivoted = period_grouped.pivot_table(
            index=group_cols,
            columns="period",
            values=["travel_time_seconds_mean", "trip_count"],
        )
        pivoted.columns = [
            f"{metric}_{period}{suffix}" for metric, period in pivoted.columns
        ]
        pivoted = pivoted.reset_index()
    else:
        pivoted = pd.DataFrame(columns=group_cols)

    for period_name in all_period_names:
        for metric in ("travel_time_seconds_mean", "trip_count"):
            col = f"{metric}_{period_name}{suffix}"
            if col not in pivoted.columns:
                pivoted[col] = None
    return pivoted


def build_gtfs_stop_to_stop_edges_gdf(
    stop_times: pd.DataFrame,
    stops: pd.DataFrame,
    trips: pd.DataFrame,
    routes: pd.DataFrame,
    *,
    dataset_name: str,
    layer_id: str,
    filter_geometry=None,
    calendar: pd.DataFrame | None = None,
    calendar_dates: pd.DataFrame | None = None,
    frequencies: pd.DataFrame | None = None,
    hour_band_size: int = 1,
    hour_range: tuple[int, int] | None = None,
    peak_periods: dict[str, tuple[int, int]] | None = None,
    include_hourly_summary: bool = True,
) -> gpd.GeoDataFrame:
    """Build the stop-to-stop edges layer: one row per pair of stops.

    Includes three new things, all keeping the tabular format:

    - Option A: per-period columns (``peak_am``/``peak_pm``/``rest_of_day``
      by default) for ``travel_time_seconds_mean``,
      ``travel_time_seconds_median`` and ``trip_count``, e.g.
      ``travel_time_seconds_mean_peak_am``.
    - Option C: if ``include_hourly_summary=True``, two packed text
      columns ``hourly_travel_times`` / ``hourly_trip_counts`` like
      ``"07-08:320|08-09:280|17-18:310"`` with the per-hour detail,
      without generating extra rows.
    - Weekday vs. weekend: columns
      ``travel_time_seconds_mean_weekday`` / ``trip_count_weekday`` and
      ``travel_time_seconds_mean_weekend`` / ``trip_count_weekend``,
      computed from ``calendar.txt`` (or inferred from
      ``calendar_dates.txt`` if there's no ``calendar.txt``). A service
      that runs every day contributes to both columns. In addition,
      ``days_of_week_summary`` packs into text the union of the days of
      the week with service on that edge, e.g.
      ``"monday|tuesday|friday"``.
    - Period x weekday/weekend combination: the same option A columns but
      also split by day, e.g.
      ``travel_time_seconds_mean_peak_am_weekday`` /
      ``trip_count_peak_am_weekend`` /
      ``travel_time_seconds_mean_rest_of_day_weekday``.
    - The usual overall aggregates (``trip_count``,
      ``travel_time_seconds_mean``, etc.) over the full set of trips, not
      just the peak period.

    For the full per-trip detail (not aggregated at all), use
    ``build_gtfs_schedule_table`` (option B).
    """
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString

    edges = _build_stop_to_stop_trip_records(
        stop_times,
        stops,
        trips,
        routes,
        dataset_name=dataset_name,
        calendar=calendar,
        calendar_dates=calendar_dates,
        frequencies=frequencies,
        hour_band_size=hour_band_size,
        hour_range=hour_range,
        peak_periods=peak_periods,
    )

    group_cols = _existing_columns(
        edges,
        [
            "from_node_id",
            "to_node_id",
            "route_id",
            "route_short_name",
            "route_long_name",
            "route_type",
            "from_stop_name",
            "to_stop_name",
            "from_stop_lon",
            "from_stop_lat",
            "to_stop_lon",
            "to_stop_lat",
        ],
    )

    # --- Overall aggregates (all hours together) ---
    aggregations = {
        "trip_count": ("trip_id", "nunique"),
        "travel_time_seconds_mean": ("travel_time_seconds", "mean"),
        "travel_time_seconds_min": ("travel_time_seconds", "min"),
        "travel_time_seconds_max": ("travel_time_seconds", "max"),
    }
    grouped = edges.groupby(group_cols, dropna=False).agg(**aggregations).reset_index()

    # --- Option A: per-period columns
    # (peak_am / peak_pm / rest_of_day) ---
    period_pivot = _period_pivot_columns(edges, group_cols, peak_periods)
    grouped = grouped.merge(period_pivot, on=group_cols, how="left")

    # --- Weekday vs. weekend: same overall aggregates, split by
    # ``runs_weekday`` / ``runs_weekend``, plus the combination with the
    # period (e.g. ``travel_time_seconds_mean_peak_am_weekday``). A trip
    # that runs on both day types (e.g. a daily service) counts in both
    # columns; one that only runs on weekdays or only on weekends counts
    # only in its own. ---
    all_period_names = list((peak_periods or DEFAULT_PEAK_PERIODS).keys()) + [
        "rest_of_day"
    ]
    day_type_masks = {
        "weekday": edges.get("runs_weekday"),
        "weekend": edges.get("runs_weekend"),
    }
    for day_type_name, mask in day_type_masks.items():
        if mask is None:
            for metric in ("travel_time_seconds_mean", "trip_count"):
                grouped[f"{metric}_{day_type_name}"] = None
                for period_name in all_period_names:
                    grouped[f"{metric}_{period_name}_{day_type_name}"] = None
            continue
        day_rows = edges[mask.fillna(False)]
        if day_rows.empty:
            for metric in ("travel_time_seconds_mean", "trip_count"):
                grouped[f"{metric}_{day_type_name}"] = None
                for period_name in all_period_names:
                    grouped[f"{metric}_{period_name}_{day_type_name}"] = None
            continue
        day_grouped = (
            day_rows.groupby(group_cols, dropna=False)
            .agg(
                travel_time_seconds_mean=("travel_time_seconds", "mean"),
                trip_count=("trip_id", "nunique"),
            )
            .reset_index()
            .rename(
                columns={
                    "travel_time_seconds_mean": (
                        f"travel_time_seconds_mean_{day_type_name}"
                    ),
                    "trip_count": f"trip_count_{day_type_name}",
                }
            )
        )
        grouped = grouped.merge(day_grouped, on=group_cols, how="left")

        day_period_pivot = _period_pivot_columns(
            day_rows, group_cols, peak_periods, suffix=f"_{day_type_name}"
        )
        grouped = grouped.merge(day_period_pivot, on=group_cols, how="left")

    # --- Summary of days of the week with service, as packed text (the
    # union of the days across every trip that uses that edge, e.g.
    # "monday|tuesday|wednesday|thursday|friday"). Doesn't generate new
    # rows. ---
    if "active_days_of_week" in edges.columns:
        days_summary = (
            edges.groupby(group_cols, dropna=False)["active_days_of_week"]
            .apply(_union_days_of_week)
            .reset_index()
            .rename(columns={"active_days_of_week": "days_of_week_summary"})
        )
        grouped = grouped.merge(days_summary, on=group_cols, how="left")
    else:
        grouped["days_of_week_summary"] = None

    # --- Option C: hourly summary packed as text, no new rows ---
    if include_hourly_summary:
        hour_rows = edges.dropna(subset=["hour_band"])
        if not hour_rows.empty:
            hour_grouped = (
                hour_rows.groupby(group_cols + ["hour_band"], dropna=False)
                .agg(
                    travel_time_seconds_mean=("travel_time_seconds", "mean"),
                    trip_count=("trip_id", "nunique"),
                )
                .reset_index()
            )
            packed = (
                hour_grouped.groupby(group_cols)
                .apply(
                    lambda df: pd.Series(
                        {
                            "hourly_travel_times": _pack_key_value(
                                df.set_index("hour_band")["travel_time_seconds_mean"]
                            ),
                            "hourly_trip_counts": _pack_key_value(
                                df.set_index("hour_band")["trip_count"]
                            ),
                        }
                    ),
                    include_groups=False,
                )
                .reset_index()
            )
            grouped = grouped.merge(packed, on=group_cols, how="left")
        for col in ("hourly_travel_times", "hourly_trip_counts"):
            if col not in grouped.columns:
                grouped[col] = None

    grouped["geometry"] = grouped.apply(
        lambda row: LineString(
            [
                (row["from_stop_lon"], row["from_stop_lat"]),
                (row["to_stop_lon"], row["to_stop_lat"]),
            ]
        ),
        axis=1,
    )
    gdf = gpd.GeoDataFrame(grouped, geometry="geometry", crs=SOURCE_CRS)
    gdf = _filter_by_geometry(gdf, filter_geometry)
    gdf["dataset_name"] = dataset_name
    gdf["layer_id"] = layer_id
    gdf["edge_id"] = (
        dataset_name
        + "_"
        + gdf["from_node_id"].astype(str)
        + "_"
        + gdf["to_node_id"].astype(str)
    )
    return gdf


def normalize_gtfs_feed(
    feed_path: str | Path,
    *,
    dataset_name: str,
    layer_id: str,
    filter_geometry=None,
    target_crs: str | None = SOURCE_CRS,
    hour_band_size: int = 1,
    hour_range: tuple[int, int] | None = None,
    peak_periods: dict[str, tuple[int, int]] | None = None,
    include_hourly_summary: bool = True,
    include_schedule_table: bool = True,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame | None]:
    """Normalize a GTFS feed into (stops, edges, schedule).

    The third returned element, ``schedule`` (option B), is a flat table
    (``pandas.DataFrame``, no geometry) with the trip-by-trip detail; it's
    ``None`` if ``include_schedule_table=False``.

    ``peak_periods`` (option A) defaults to separating peak_am (07-09),
    peak_pm (17-20) and rest_of_day (everything else); a custom dict with
    the same format can be passed to change the bands.

    Both the edges layer and the schedule table also include a
    weekday/weekend split (``*_weekday`` / ``*_weekend`` columns in edges,
    ``active_days_of_week`` / ``runs_weekday`` / ``runs_weekend`` /
    ``day_type`` columns in the schedule), derived from ``calendar.txt``
    (falling back to ``calendar_dates.txt`` if it doesn't exist).
    """
    stops = read_gtfs_table(feed_path, "stops.txt")
    stop_times = read_gtfs_table(feed_path, "stop_times.txt")
    trips = read_gtfs_table(feed_path, "trips.txt")
    routes = read_gtfs_table(feed_path, "routes.txt")
    calendar = _read_optional_gtfs_table(feed_path, "calendar.txt")
    calendar_dates = _read_optional_gtfs_table(feed_path, "calendar_dates.txt")
    frequencies = _read_optional_gtfs_table(feed_path, "frequencies.txt")

    stops_gdf = build_gtfs_stops_gdf(
        stops,
        dataset_name=dataset_name,
        layer_id=layer_id,
        filter_geometry=filter_geometry,
    )
    stop_edges_gdf = build_gtfs_stop_to_stop_edges_gdf(
        stop_times,
        stops,
        trips,
        routes,
        dataset_name=dataset_name,
        layer_id=layer_id,
        filter_geometry=filter_geometry,
        calendar=calendar,
        calendar_dates=calendar_dates,
        frequencies=frequencies,
        hour_band_size=hour_band_size,
        hour_range=hour_range,
        peak_periods=peak_periods,
        include_hourly_summary=include_hourly_summary,
    )

    schedule_df = None
    if include_schedule_table:
        schedule_df = build_gtfs_schedule_table(
            stop_times,
            stops,
            trips,
            routes,
            dataset_name=dataset_name,
            calendar=calendar,
            calendar_dates=calendar_dates,
            frequencies=frequencies,
            hour_band_size=hour_band_size,
            hour_range=hour_range,
            peak_periods=peak_periods,
        )

    if target_crs:
        stops_gdf = stops_gdf.to_crs(target_crs)
        stop_edges_gdf = stop_edges_gdf.to_crs(target_crs)

    return stops_gdf, stop_edges_gdf, schedule_df
