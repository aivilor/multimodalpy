"""GTFS networks: standardization of bus and train feeds, and their download.

Private module: the public entry point is ``multimodalpy.network``.

This module has two parts:

1. Standardization: turns a GTFS feed (zip or folder) into GeoPandas
   layers -- ``nodes`` (the stops the edges use, as points), ``edges``
   (straight stop-to-stop edges, one per route and pair of consecutive
   stops, with travel times and trip counts overall, per period and per
   hour band) and ``schedule`` (a flat, non-spatial table with one record
   per trip and edge, for anyone who needs the full schedule detail).
2. Download: ``download_gtfs`` fetches the feeds of a study area
   from the NAP (see ``_nap``) and the ``gtfs_feeds`` of the user, and
   standardizes each one. The short column names of the edge layer
   (``GTFS_EDGE_RENAME``) are also defined here.

No file is written here; that's done by ``_writers``.
"""

from __future__ import annotations

import datetime
import logging
import numbers
import re
import warnings
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zipfile import BadZipFile, ZipFile

if TYPE_CHECKING:
    import geopandas as gpd
    import pandas as pd

from ._boundaries import study_area_geometry
from ._nap import download_gtfs_nap_zips
from ._text import slugify

logger = logging.getLogger(__name__)


SOURCE_CRS = "EPSG:4326"

# Default peak periods for the option A breakdown.
# "rest_of_day" is automatically assigned to any hour not covered here.
DEFAULT_PEAK_PERIODS: dict[str, tuple[int, int]] = {
    "peak_am": (7, 9),  # morning peak, 07:00-09:00
    "peak_pm": (17, 20),  # evening peak, 17:00-20:00
}

#: Longest believable travel time between two consecutive stops of a trip.
#: Longer ones are errors in the feed (a time typed on the wrong day or
#: hour: one Valencia interurban segment "took" 10.9 h); their travel time
#: is left empty so it doesn't distort the edge statistics and the matrix.
MAX_SEGMENT_SECONDS = 3 * 3600


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
# else (bus 3, trolleybus 11, gondola 6...) is treated as bus.
RAIL_ROUTE_TYPES: frozenset[int] = frozenset({0, 1, 2, 5, 7, 12})

# Extended route types (the European HVT codes many feeds use instead):
# railway 100-199 (e.g. 101 high-speed rail, used by OUIGO, 102 long
# distance), suburban railway 300-399, urban railway 400-499, metro
# 500-599, underground 600-699, tram 900-999 and funicular 1400-1499.
# Coach (200-299), bus (700-799), trolleybus (800), water, air, aerial
# lift and taxi are treated as bus.
RAIL_EXTENDED_ROUTE_TYPES: tuple[range, ...] = (
    range(100, 200),
    range(300, 700),
    range(900, 1000),
    range(1400, 1500),
)

# NAP transport ids of the two GTFS modes (see network.GTFS_MODE_TO_NAP).
_NAP_ID_OF_MODE = {"bus": 1, "train": 2}


def is_rail_route_type(route_type: int) -> bool:
    """Whether a GTFS ``route_type`` (basic or extended) is rail-based."""
    return route_type in RAIL_ROUTE_TYPES or any(
        route_type in codes for codes in RAIL_EXTENDED_ROUTE_TYPES
    )


def infer_transport_mode(feed_path: str | Path) -> str:
    """Return ``"train"`` or ``"bus"`` based on the feed's ``route_type`` values.

    Used to decide which folder a GTFS dataset's layers are saved under.
    The feed itself is checked rather than the NAP metadata, because there
    the same dataset can be declared as both bus and rail at once (e.g.
    Cercanias Renfe). Basic and extended route types are both understood
    (see :func:`is_rail_route_type`). If the feed mixes both, the majority
    type wins; if it can't be read, ``"bus"`` is assumed.
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
    rail = sum(is_rail_route_type(int(code)) for code in route_types)
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


_SEGMENT_COORD_COLUMNS = [
    "from_stop_lon",
    "from_stop_lat",
    "to_stop_lon",
    "to_stop_lat",
]


def _filter_records_by_geometry(records: pd.DataFrame, filter_geometry) -> pd.DataFrame:
    """Keep the trip records whose stop-to-stop segment touches ``filter_geometry``.

    Applies the same test as the final ``_filter_by_geometry`` on the edges
    layer (the straight from-stop -> to-stop line intersects the area), but
    before aggregating, so the trips of the rest of the province are not
    aggregated only to be thrown away. Each distinct segment is tested
    once. A segment that crosses the area without stopping in it is kept,
    as before.
    """
    import pandas as pd
    import shapely

    if filter_geometry is None or records.empty:
        return records
    segments = records[_SEGMENT_COORD_COLUMNS].drop_duplicates()
    lines = shapely.linestrings(segments.to_numpy(dtype=float).reshape(-1, 2, 2))
    inside = segments[shapely.intersects(lines, filter_geometry)]
    keep = pd.MultiIndex.from_frame(records[_SEGMENT_COORD_COLUMNS]).isin(
        pd.MultiIndex.from_frame(inside)
    )
    return records[keep]


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


def seconds_to_gtfs_time(seconds) -> str | None:
    """Convert seconds since midnight to 'HH:MM:SS' (HH may be >= 24)."""
    import math

    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return None
    total = int(round(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


#: Band sizes that split the day into equal bands (``gtfs_hour_band_size``).
HOUR_BAND_SIZES = (1, 2, 3, 4, 6, 8, 12, 24)


def hour_in_range(hour, start: int, end: int):
    """Whether ``hour`` (0-23, a number or a Series) is in ``[start, end)``.

    A range that crosses midnight, such as ``(22, 2)``, holds 22, 23, 0 and
    1. ``end`` may be 24 (the end of the day), and ``0`` means the same.
    """
    if start < end:
        return (hour >= start) & (hour < end)
    return (hour >= start) | (hour < end)


def _check_hours(value: object, what: str) -> tuple[int, int]:
    """Return ``value`` as ``(start, end)`` hours, or raise a clear ``ValueError``."""
    start: Any
    end: Any
    try:
        start, end = value  # type: ignore[misc]
    except (TypeError, ValueError):
        raise ValueError(
            f"{what} must be two hours (start, end), e.g. (7, 9). Received: {value!r}"
        ) from None
    for hour in (start, end):
        if isinstance(hour, bool) or not isinstance(hour, numbers.Integral):
            raise ValueError(
                f"{what}: hours must be whole numbers. Received: {value!r}"
            )
    if not 0 <= start <= 23 or not 0 <= end <= 24:
        raise ValueError(
            f"{what}: the start must be an hour from 0 to 23 and the end from 0 "
            f"to 24. Received: {value!r}"
        )
    if start == end % 24:
        raise ValueError(
            f"{what}: start and end are the same hour, so the range is empty "
            f"or the whole day (for the whole day, leave it out). "
            f"Received: {value!r}"
        )
    return int(start), int(end)


def check_hour_options(
    hour_range: object = None,
    hour_band_size: object = 1,
    peak_periods: object = None,
) -> None:
    """Check the GTFS hour options before anything is downloaded.

    - ``hour_range``: ``None`` or ``(start, end)`` whole hours, the start
      from 0 to 23 and the end from 0 to 24; it may cross midnight
      (``(22, 2)``).
    - ``hour_band_size``: hours per band of the hourly summary, one of
      ``HOUR_BAND_SIZES`` so the bands split the day evenly.
    - ``peak_periods``: ``None`` or ``{name: (start, end)}`` with the same
      hours as ``hour_range`` (a period may cross midnight too), names that
      are text and not ``rest_of_day``, and no hour in two periods.

    Raises ``ValueError`` saying what is wrong.
    """
    if hour_range is not None:
        _check_hours(hour_range, "gtfs_hour_range")
    if (
        isinstance(hour_band_size, bool)
        or not isinstance(hour_band_size, numbers.Integral)
        or hour_band_size not in HOUR_BAND_SIZES
    ):
        sizes = ", ".join(map(str, HOUR_BAND_SIZES))
        raise ValueError(
            "gtfs_hour_band_size must split the day into equal bands: one of "
            f"{sizes} hours. Received: {hour_band_size!r}"
        )
    if peak_periods is None:
        return
    if not isinstance(peak_periods, Mapping) or not peak_periods:
        raise ValueError(
            "gtfs_peak_periods must be a dict {name: (start, end)}, e.g. "
            f"{{'morning': (7, 9)}}. Received: {peak_periods!r}"
        )
    owner: dict[int, str] = {}
    for name, hours in peak_periods.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"gtfs_peak_periods: names must be text. Received: {name!r}"
            )
        if name == "rest_of_day":
            raise ValueError(
                "gtfs_peak_periods: 'rest_of_day' is the name of the hours outside "
                "every period; choose another name."
            )
        start, end = _check_hours(hours, f"gtfs_peak_periods[{name!r}]")
        for hour in range(24):
            if not hour_in_range(hour, start, end):
                continue
            if hour in owner:
                raise ValueError(
                    f"gtfs_peak_periods: {owner[hour]!r} and {name!r} both "
                    f"include the hour {hour:02d}-{hour + 1:02d}; periods "
                    "cannot overlap."
                )
            owner[hour] = name


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
        if hour_in_range(hour, start, end):
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


def _pack_by_group(
    rows: pd.DataFrame,
    group_cols: list[str],
    label_col: str,
    value_col: str,
    *,
    decimals: int = 0,
) -> pd.Series:
    """Vectorized ``_pack_key_value`` for every group at once.

    Returns one ``'label:value|label:value'`` string per group (indexed by
    ``group_cols``), with the same format, rounding and label order as
    ``_pack_key_value``. Groups whose values are all NaN are left out.
    Calling ``_pack_key_value`` once per group through ``groupby.apply``
    took most of the GTFS processing time on large feeds.
    """
    work = rows[group_cols + [label_col, value_col]].dropna(
        subset=[label_col, value_col]
    )
    work = work.sort_values(label_col, kind="stable")
    values = work[value_col].astype(float).round(decimals)
    tokens = work[label_col].astype(str) + ":" + [f"{value:g}" for value in values]
    return tokens.groupby(
        [work[column] for column in group_cols], dropna=False, sort=False
    ).agg("|".join)


# ---------------------------------------------------------------------------
# Building the normalized layers
# ---------------------------------------------------------------------------
def build_gtfs_stops_gdf(
    stops: pd.DataFrame,
    *,
    dataset_name: str,
    layer_id: str,
    filter_geometry=None,
    stop_ids=None,
) -> gpd.GeoDataFrame:
    """Build the stops (nodes) layer from ``stops.txt``.

    With ``stop_ids`` (the stops the edges use, as :func:`normalize_gtfs_feed`
    passes them), the layer holds exactly those stops: a stop no trip
    serves, a parent station or an entrance is left out, and a stop just
    outside ``filter_geometry`` that ends an edge crossing it is kept.
    ``in_area`` says whether each stop is inside ``filter_geometry``.
    Without ``stop_ids``, every stop inside ``filter_geometry`` is kept.
    """
    import geopandas as gpd
    import pandas as pd

    work = stops.copy()
    work["stop_lat"] = pd.to_numeric(work["stop_lat"], errors="coerce")
    work["stop_lon"] = pd.to_numeric(work["stop_lon"], errors="coerce")
    work = work.dropna(subset=["stop_lat", "stop_lon"]).drop_duplicates("stop_id")
    if stop_ids is not None:
        work = work[work["stop_id"].isin(set(stop_ids))]
    gdf = gpd.GeoDataFrame(
        work.copy(),
        geometry=gpd.points_from_xy(work["stop_lon"], work["stop_lat"]),
        crs=SOURCE_CRS,
    )
    if stop_ids is None:
        gdf = _filter_by_geometry(gdf, filter_geometry)
        gdf["in_area"] = True
    else:
        gdf["in_area"] = (
            True
            if filter_geometry is None
            else gdf.geometry.intersects(filter_geometry)
        )
    gdf["dataset_name"] = dataset_name
    gdf["layer_id"] = layer_id
    gdf["node_id"] = gdf["stop_id"]
    gdf["source_name"] = dataset_name
    gdf["node_type"] = "stop"
    return gdf


# ---------------------------------------------------------------------------
# Service date: the trips that run on one day
# ---------------------------------------------------------------------------
_WEEKDAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def parse_service_date(value: object) -> datetime.date:
    """Read a service date: a ``date``, ``"2026-11-25"`` or ``"20261125"``."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"gtfs_service_date must be a date, 'YYYY-MM-DD' or 'YYYYMMDD'. Received: "
        f"{value!r}"
    )


def _gtfs_dates(values: pd.Series) -> pd.Series:
    import pandas as pd

    return pd.to_datetime(values, format="%Y%m%d", errors="coerce").dt.date


def service_date_counts(
    calendar: pd.DataFrame | None,
    calendar_dates: pd.DataFrame | None,
) -> pd.Series:
    """Count the dates each service runs on, indexed by ``service_id``.

    As the GTFS reference defines the dates of a service: the days of the
    week ``calendar.txt`` marks between its ``start_date`` and
    ``end_date``, plus the dates ``calendar_dates.txt`` adds
    (``exception_type`` 1), minus the ones it removes (2). A date added
    that was already in the pattern, or removed that wasn't, changes
    nothing. Services with no date are left out. Counted without listing
    the dates, so a feed with thousands of services takes no time.
    """
    import numpy as np
    import pandas as pd

    weekdays = list(_WEEKDAY_COLUMNS)
    needed = {"service_id", "start_date", "end_date", *weekdays}
    if calendar is not None and needed <= set(calendar.columns):
        cal = calendar.drop_duplicates("service_id").assign(
            _start=pd.to_datetime(
                calendar["start_date"], format="%Y%m%d", errors="coerce"
            ),
            _end=pd.to_datetime(calendar["end_date"], format="%Y%m%d", errors="coerce"),
        )
        cal = cal[cal["_start"].notna() & (cal["_end"] >= cal["_start"])]
        flags = cal[weekdays].apply(pd.to_numeric, errors="coerce").to_numpy() == 1
        # Dates of each day of the week in [start, end]: one per full week,
        # plus one if it falls in the days left over after them.
        days = (cal["_end"] - cal["_start"]).dt.days.to_numpy() + 1
        first = cal["_start"].dt.day_of_week.to_numpy()
        offset = (np.arange(7)[None, :] - first[:, None]) % 7
        per_day = days[:, None] // 7 + (offset < (days % 7)[:, None])
        pattern = pd.Series((per_day * flags).sum(axis=1), index=cal["service_id"])
    else:
        cal = pd.DataFrame(columns=["service_id", "_start", "_end", *weekdays])
        flags = np.zeros((0, 7), dtype=bool)
        pattern = pd.Series(dtype="int64")
    counts = pattern.astype("int64")

    if calendar_dates is not None and {"service_id", "date", "exception_type"} <= set(
        calendar_dates.columns
    ):
        exceptions = calendar_dates.assign(
            _date=pd.to_datetime(
                calendar_dates["date"], format="%Y%m%d", errors="coerce"
            ),
            _kind=pd.to_numeric(calendar_dates["exception_type"], errors="coerce"),
        )
        exceptions = exceptions[
            exceptions["_date"].notna() & exceptions["_kind"].isin([1, 2])
        ]
        # One row per service and date: added, removed (removing wins if a
        # feed does both, as when the dates are listed and then dropped).
        exceptions = (
            exceptions.assign(
                _added=exceptions["_kind"] == 1, _removed=exceptions["_kind"] == 2
            )
            .groupby(["service_id", "_date"], as_index=False)[["_added", "_removed"]]
            .any()
        )
        # Whether each exception's date is already in its service's pattern.
        ranges = pd.DataFrame(
            {
                "service_id": cal["service_id"],
                "_start": cal["_start"],
                "_end": cal["_end"],
            }
        ).reset_index(drop=True)
        ranges["_row"] = np.arange(len(ranges))
        found = exceptions.merge(ranges, on="service_id", how="left")
        weekday = found["_date"].dt.day_of_week.to_numpy()
        row = found["_row"].to_numpy(dtype=float)
        has_row = ~np.isnan(row)
        in_pattern = np.zeros(len(found), dtype=bool)
        rows = row[has_row].astype(int)
        date = found["_date"].to_numpy()[has_row]
        in_pattern[has_row] = (
            flags[rows, weekday[has_row]]
            & (date >= found["_start"].to_numpy()[has_row])
            & (date <= found["_end"].to_numpy()[has_row])
        )
        runs = (in_pattern | found["_added"].to_numpy()) & ~found["_removed"].to_numpy()
        change = runs.astype(int) - in_pattern.astype(int)
        delta = pd.Series(change, index=found["service_id"]).groupby(level=0).sum()
        counts = counts.add(delta, fill_value=0).astype("int64")
    counts.index.name = "service_id"
    return counts[counts > 0]


def active_service_ids(
    calendar: pd.DataFrame | None,
    calendar_dates: pd.DataFrame | None,
    service_date: datetime.date,
) -> set[str]:
    """``service_id`` of the services that run on ``service_date``.

    As the GTFS reference says: the services of ``calendar.txt`` whose
    ``start_date``-``end_date`` range holds the date and that run on its day
    of the week, plus the ones ``calendar_dates.txt`` adds that day
    (``exception_type`` 1), minus the ones it removes (2).
    """
    import pandas as pd

    active: set[str] = set()
    weekday = _WEEKDAY_COLUMNS[service_date.weekday()]
    needed = {"service_id", "start_date", "end_date", weekday}
    if calendar is not None and needed <= set(calendar.columns):
        start = _gtfs_dates(calendar["start_date"])
        end = _gtfs_dates(calendar["end_date"])
        runs = pd.to_numeric(calendar[weekday], errors="coerce") == 1
        in_range = (start <= service_date) & (end >= service_date)
        active = set(calendar.loc[runs & in_range.fillna(False), "service_id"])
    if calendar_dates is not None and {
        "service_id",
        "date",
        "exception_type",
    } <= set(calendar_dates.columns):
        that_day = calendar_dates[_gtfs_dates(calendar_dates["date"]) == service_date]
        kind = pd.to_numeric(that_day["exception_type"], errors="coerce")
        active |= set(that_day.loc[kind == 1, "service_id"])
        active -= set(that_day.loc[kind == 2, "service_id"])
    return active


def service_period(
    calendar: pd.DataFrame | None, calendar_dates: pd.DataFrame | None
) -> tuple[datetime.date, datetime.date] | None:
    """First and last day the feed's calendar covers, or ``None`` if unknown."""
    import pandas as pd

    days: list[pd.Series] = []
    if calendar is not None:
        days += [
            _gtfs_dates(calendar[col])
            for col in ("start_date", "end_date")
            if col in calendar.columns
        ]
    if calendar_dates is not None and "date" in calendar_dates.columns:
        days.append(_gtfs_dates(calendar_dates["date"]))
    every = pd.concat(days).dropna() if days else pd.Series(dtype=object)
    if every.empty:
        return None
    return min(every), max(every)


def _no_service_message(feed_path: str | Path, service_date: datetime.date) -> str:
    """Why a feed has no trip on ``service_date``, or ``""`` if it has some."""
    calendar = _read_optional_gtfs_table(feed_path, "calendar.txt")
    calendar_dates = _read_optional_gtfs_table(feed_path, "calendar_dates.txt")
    trips = read_gtfs_table(feed_path, "trips.txt")
    if "service_id" not in trips.columns:
        return ""
    active = active_service_ids(calendar, calendar_dates, service_date)
    if trips["service_id"].isin(active).any():
        return ""
    message = f"no trips on {service_date.isoformat()}"
    period = service_period(calendar, calendar_dates)
    if period is not None:
        message += (
            f"; its calendar runs from {period[0].isoformat()} "
            f"to {period[1].isoformat()}"
        )
    return message


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

    - ``days_active``: number of dates the service runs on, from
      :func:`service_date_counts`.
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
    cal_dow = pd.Series(dtype="object")
    cal_runs_weekday = pd.Series(dtype="boolean")
    cal_runs_weekend = pd.Series(dtype="boolean")

    if calendar is not None and set(all_day_cols).issubset(calendar.columns):
        cal = calendar.copy()
        for col in all_day_cols:
            cal[col] = pd.to_numeric(cal[col], errors="coerce").fillna(0)
        cal["active_days_of_week"] = cal[all_day_cols].apply(
            lambda row: "|".join(day for day in _WEEKDAY_ORDER if row[day] == 1), axis=1
        )
        cal["runs_weekday"] = cal[weekday_cols].sum(axis=1) > 0
        cal["runs_weekend"] = cal[weekend_cols].sum(axis=1) > 0
        cal = cal.drop_duplicates(subset=["service_id"]).set_index("service_id")
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

    inferred_dow = pd.Series(dtype="object")
    inferred_runs_weekday = pd.Series(dtype="boolean")
    inferred_runs_weekend = pd.Series(dtype="boolean")

    if calendar_dates is not None and "exception_type" in calendar_dates.columns:
        cd = calendar_dates.copy()
        cd["exception_type"] = pd.to_numeric(cd["exception_type"], errors="coerce")

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

    # ``days_active``: how many dates the service runs on.
    service_days = service_date_counts(calendar, calendar_dates)

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


def _edge_ids(dataset_name: str, frame: pd.DataFrame) -> pd.Series:
    """``<dataset>_<route_id>_<from_node>_<to_node>`` for each row.

    The edges are one per pair of stops *and* route, so the route is part
    of the id: several routes often share a pair of stops, and an id made
    of the stops alone repeated, mixing their schedule rows up.
    """
    route = (
        frame["route_id"].where(frame["route_id"].notna(), "").astype(str)
        if "route_id" in frame.columns
        else ""
    )
    return (
        dataset_name
        + "_"
        + route
        + "_"
        + frame["from_node"].astype(str)
        + "_"
        + frame["to_node"].astype(str)
    )


def _expand_frequencies(
    stop_times: pd.DataFrame,
    trips: pd.DataFrame,
    frequencies: pd.DataFrame | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Turn the trips defined in ``frequencies.txt`` into one trip per departure.

    A frequency-based trip is a template: its ``stop_times`` give the times
    between stops, and each ``frequencies.txt`` row says it leaves every
    ``headway_secs`` from ``start_time`` until (not including) ``end_time``.
    Each departure becomes a trip of its own, ``<trip_id>@<HH:MM:SS>``, with
    the template's times shifted to it, so it counts in ``trip_count``, its
    period and its hour band like any other trip. The templates are
    replaced; a template with no valid row (no headway, no times) is kept
    as it is.
    """
    import numpy as np
    import pandas as pd

    needed = {"trip_id", "start_time", "end_time", "headway_secs"}
    if frequencies is None or frequencies.empty or not needed <= set(frequencies):
        return stop_times, trips

    freq = frequencies.loc[:, ["trip_id", "start_time", "end_time", "headway_secs"]]
    freq = freq.assign(
        start=freq["start_time"].map(gtfs_time_to_seconds),
        end=freq["end_time"].map(gtfs_time_to_seconds),
        headway=pd.to_numeric(freq["headway_secs"], errors="coerce"),
    ).dropna(subset=["start", "end", "headway"])
    freq = freq[(freq["headway"] > 0) & (freq["end"] > freq["start"])]
    freq = freq[freq["trip_id"].isin(set(stop_times["trip_id"]))]
    if freq.empty:
        return stop_times, trips

    template = stop_times[stop_times["trip_id"].isin(set(freq["trip_id"]))].copy()
    template["_seq"] = pd.to_numeric(template["stop_sequence"], errors="coerce")
    times = {
        col: template[col].map(gtfs_time_to_seconds)
        for col in ("arrival_time", "departure_time")
        if col in template.columns
    }
    # The template's first time: its departure from the first stop (or its
    # arrival there, if the departure is missing).
    first = template.assign(**times).sort_values("_seq").groupby("trip_id").first()
    base = pd.Series(np.nan, index=first.index)
    for col in ("departure_time", "arrival_time"):
        if col in first.columns:
            base = base.fillna(first[col])
    base = base.dropna()
    freq = freq[freq["trip_id"].isin(base.index)]
    if freq.empty:
        return stop_times, trips

    # One row per departure: start, start + headway, ... before end.
    departures = freq.assign(
        departure=[
            np.arange(start, end, headway)
            for start, end, headway in freq[["start", "end", "headway"]].itertuples(
                index=False
            )
        ]
    ).explode("departure")[["trip_id", "departure"]]
    departures["departure"] = departures["departure"].astype(float)
    departures = departures.drop_duplicates()
    departures["new_trip_id"] = (
        departures["trip_id"] + "@" + departures["departure"].map(seconds_to_gtfs_time)
    )
    departures["offset"] = departures["departure"] - departures["trip_id"].map(base)

    expanded = template.drop(columns="_seq").merge(
        departures[["trip_id", "new_trip_id", "offset"]], on="trip_id"
    )
    for col in times:
        expanded[col] = (
            expanded[col].map(gtfs_time_to_seconds) + expanded["offset"]
        ).map(seconds_to_gtfs_time)
    expanded = expanded.drop(columns=["trip_id", "offset"]).rename(
        columns={"new_trip_id": "trip_id"}
    )

    expanded_trips = trips.merge(
        departures[["trip_id", "new_trip_id"]].drop_duplicates(), on="trip_id"
    )
    expanded_trips = expanded_trips.drop(columns="trip_id").rename(
        columns={"new_trip_id": "trip_id"}
    )[list(trips.columns)]

    replaced = set(departures["trip_id"])
    stop_times = pd.concat(
        [
            stop_times[~stop_times["trip_id"].isin(replaced)],
            expanded[stop_times.columns],
        ],
        ignore_index=True,
    )
    trips = pd.concat(
        [trips[~trips["trip_id"].isin(replaced)], expanded_trips], ignore_index=True
    )
    return stop_times, trips


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
    service_date: datetime.date | None = None,
) -> pd.DataFrame:
    """Build the intermediate, unaggregated table of trip-segment records.

    One record per (trip, segment between consecutive stops). Reused by
    both the aggregated edges layer (``build_gtfs_stop_to_stop_edges_gdf``)
    and the flat schedule table (``build_gtfs_schedule_table``), to avoid
    duplicating the travel-time and merge logic.

    With ``service_date``, only the trips that run that day are kept (see
    :func:`active_service_ids`).
    """
    import pandas as pd

    if service_date is not None and "service_id" in trips.columns:
        active = active_service_ids(calendar, calendar_dates, service_date)
        trips = trips[trips["service_id"].isin(active)]
        stop_times = stop_times[stop_times["trip_id"].isin(set(trips["trip_id"]))]
    stop_times, trips = _expand_frequencies(stop_times, trips, frequencies)
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

    # float even with no rows (e.g. no trip on the service date), so the
    # aggregations still work on an empty table.
    work["travel_time_seconds"] = (
        work["next_arrival_seconds"] - work["departure_seconds"]
    ).astype(float)
    work.loc[work["travel_time_seconds"] < 0, "travel_time_seconds"] = float("nan")
    # Impossible times (see MAX_SEGMENT_SECONDS): left empty and marked, so
    # normalize_gtfs_feed can say how many there were in the study area.
    work["implausible_time"] = work["travel_time_seconds"] > MAX_SEGMENT_SECONDS
    work.loc[work["implausible_time"], "travel_time_seconds"] = float("nan")

    work["hour_band"] = work["departure_seconds"].apply(
        lambda s: seconds_to_hour_band(s, band_size_hours=hour_band_size)
    )
    work["period"] = work["departure_seconds"].apply(
        lambda s: classify_period(s, peak_periods)
    )

    if hour_range is not None:
        start_h, end_h = hour_range
        hour_of_day = (work["departure_seconds"] // 3600) % 24
        work = work[hour_in_range(hour_of_day, start_h, end_h)].copy()

    stop_lookup = stops[["stop_id", "stop_name", "stop_lat", "stop_lon"]].copy()
    stop_lookup["stop_lat"] = pd.to_numeric(stop_lookup["stop_lat"], errors="coerce")
    stop_lookup["stop_lon"] = pd.to_numeric(stop_lookup["stop_lon"], errors="coerce")
    stop_lookup = stop_lookup.dropna(subset=["stop_lat", "stop_lon"]).drop_duplicates(
        "stop_id"
    )

    from_lookup = stop_lookup.rename(
        columns={
            "stop_id": "from_node",
            "stop_name": "from_stop_name",
            "stop_lat": "from_stop_lat",
            "stop_lon": "from_stop_lon",
        }
    )
    to_lookup = stop_lookup.rename(
        columns={
            "stop_id": "to_node",
            "stop_name": "to_stop_name",
            "stop_lat": "to_stop_lat",
            "stop_lon": "to_stop_lon",
        }
    )

    edges = work.rename(
        columns={"stop_id": "from_node", "next_stop_id": "to_node"}
    ).copy()

    trips_work = _attach_service_days(trips, calendar, calendar_dates)

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
    edges = edges.merge(from_lookup, on="from_node", how="left")
    edges = edges.merge(to_lookup, on="to_node", how="left")
    edges = edges.dropna(
        subset=["from_stop_lon", "from_stop_lat", "to_stop_lon", "to_stop_lat"]
    ).copy()
    edges["edge_id"] = _edge_ids(dataset_name, edges)
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
    records: pd.DataFrame | None = None,
    filter_geometry=None,
    service_date: datetime.date | None = None,
) -> pd.DataFrame:
    """Option B: a flat table (no geometry), one record per trip and segment.

    Meant to be joined with the ``edges`` layer by ``edge_id`` when the
    full schedule detail is needed (every step of every trip), without
    losing that granularity in the aggregated spatial layer. With
    ``filter_geometry``, only the segments that touch it are kept: the
    same test as the edges layer, so every row has its edge.

    ``records`` takes the output of ``_build_stop_to_stop_trip_records``
    if it was already built with the same arguments (as
    ``normalize_gtfs_feed`` does), so it is not built a second time.
    """
    if records is None:
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
            service_date=service_date,
        )
    records = _filter_records_by_geometry(records, filter_geometry)
    columns = _existing_columns(
        records,
        [
            "edge_id",
            "trip_id",
            "route_id",
            "route_short_name",
            "trip_headsign",
            "from_node",
            "to_node",
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
    median: bool = False,
) -> pd.DataFrame:
    """Aggregate ``travel_time_seconds_mean`` / ``trip_count`` by period.

    Periods are peak_am / peak_pm / rest_of_day, pivoting ``period`` into
    columns like ``travel_time_seconds_mean_peak_am``; with ``median``,
    also ``travel_time_seconds_median_peak_am``. ``suffix`` is added
    to the end of each column name so this period breakdown can be
    combined with another breakdown (weekday/weekend). Always returns all
    period columns, even if empty, so the later merge stays consistent.
    """
    import pandas as pd

    all_period_names = list((peak_periods or DEFAULT_PEAK_PERIODS).keys()) + [
        "rest_of_day"
    ]
    metrics = ["travel_time_seconds_mean", "trip_count"]
    aggregations = {
        "travel_time_seconds_mean": ("travel_time_seconds", "mean"),
        "trip_count": ("trip_id", "nunique"),
    }
    if median:
        metrics.insert(1, "travel_time_seconds_median")
        aggregations["travel_time_seconds_median"] = ("travel_time_seconds", "median")
    period_rows = rows.dropna(subset=["period"])
    if not period_rows.empty:
        # Pivot on a number per group, not on the grouping columns:
        # ``pivot_table`` drops every group with an empty key, so the edges
        # of a route without ``route_short_name`` (GTFS only asks for a
        # short or a long name) lost all their per-period columns.
        group = period_rows.groupby(group_cols, dropna=False, sort=False).ngroup()
        period_rows = period_rows.assign(_group=group)
        period_grouped = period_rows.groupby(["_group", "period"]).agg(**aggregations)
        values = period_grouped[metrics].unstack("period")
        values.columns = [
            f"{metric}_{period}{suffix}" for metric, period in values.columns
        ]
        keys = period_rows.drop_duplicates("_group").set_index("_group")[group_cols]
        pivoted = keys.join(values).reset_index(drop=True)
    else:
        pivoted = pd.DataFrame(columns=group_cols)

    for period_name in all_period_names:
        for metric in metrics:
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
    records: pd.DataFrame | None = None,
    service_date: datetime.date | None = None,
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

    ``records`` takes the output of ``_build_stop_to_stop_trip_records``
    if it was already built with the same arguments, so it is not built
    again (see ``normalize_gtfs_feed``).

    With ``service_date``, the records are the trips of that day, and the
    weekday/weekend columns count them under that day's type only: a
    Wednesday fills the ``*_weekday`` columns and leaves the ``*_weekend``
    ones empty.
    """
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString

    if records is None:
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
            service_date=service_date,
        )
    if records.empty:
        # No trip at all (none runs on the service date): an empty layer
        # with the usual columns.
        columns = [col for col in gtfs_edge_columns(peak_periods) if col != "geometry"]
        empty = gpd.GeoDataFrame(
            {col: pd.Series(dtype=object) for col in [*columns, "route_type"]},
            geometry=gpd.GeoSeries([], crs=SOURCE_CRS),
        )
        empty["dataset_name"] = pd.Series(dtype=object)
        empty["layer_id"] = pd.Series(dtype=object)
        return empty
    edges = records
    in_area = _filter_records_by_geometry(edges, filter_geometry)
    # If nothing touches the area, aggregate a single record instead of
    # none: the aggregation steps below expect at least one row, and the
    # final ``_filter_by_geometry`` drops it again, leaving an empty layer
    # with the usual columns.
    edges = in_area if not in_area.empty else edges.iloc[:1]

    group_cols = _existing_columns(
        edges,
        [
            "from_node",
            "to_node",
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
        "travel_time_seconds_median": ("travel_time_seconds", "median"),
        "travel_time_seconds_min": ("travel_time_seconds", "min"),
        "travel_time_seconds_max": ("travel_time_seconds", "max"),
    }
    grouped = edges.groupby(group_cols, dropna=False).agg(**aggregations).reset_index()

    # --- Option A: per-period columns
    # (peak_am / peak_pm / rest_of_day) ---
    period_pivot = _period_pivot_columns(edges, group_cols, peak_periods, median=True)
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
    if service_date is not None:
        on_weekend = service_date.weekday() >= 5
        day_type_masks = {
            "weekday": pd.Series(not on_weekend, index=edges.index),
            "weekend": pd.Series(on_weekend, index=edges.index),
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
            # Grouped with dropna=False: edges whose route has no
            # route_short_name (or another empty grouping column) used to
            # lose their hourly summary.
            packed = pd.concat(
                {
                    "hourly_travel_times": _pack_by_group(
                        hour_grouped,
                        group_cols,
                        "hour_band",
                        "travel_time_seconds_mean",
                    ),
                    "hourly_trip_counts": _pack_by_group(
                        hour_grouped, group_cols, "hour_band", "trip_count"
                    ),
                },
                axis=1,
            )
            packed.index.names = group_cols
            grouped = grouped.merge(packed.reset_index(), on=group_cols, how="left")
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
    gdf["edge_id"] = _edge_ids(dataset_name, gdf)
    return gdf


def _warn_implausible_times(records: pd.DataFrame, filter_geometry, dataset_name):
    """Warn about the impossible segment times left empty in the area."""
    if (
        "implausible_time" not in records.columns
        or not records["implausible_time"].any()
    ):
        return
    count = int(
        _filter_records_by_geometry(records, filter_geometry)["implausible_time"].sum()
    )
    if count:
        warnings.warn(
            f"GTFS feed {dataset_name}: {count} segment(s) between consecutive "
            f"stops take more than {MAX_SEGMENT_SECONDS // 3600} h, probably "
            "errors in the feed; their travel time is left empty.",
            UserWarning,
            stacklevel=3,
        )


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
    service_date: datetime.date | None = None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame | None]:
    """Normalize a GTFS feed into (stops, edges, schedule).

    The stops (nodes) are the stops the edges use: every stop that starts
    or ends an edge, with ``in_area`` telling whether it is inside
    ``filter_geometry`` (see :func:`build_gtfs_stops_gdf`).

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

    With ``service_date`` (a ``datetime.date``), only the trips that run
    that day are used, so ``trip_count`` is that day's number of trips;
    without it, every trip of the feed counts once, whatever days it runs.
    """
    import pandas as pd

    stops = read_gtfs_table(feed_path, "stops.txt")
    stop_times = read_gtfs_table(feed_path, "stop_times.txt")
    trips = read_gtfs_table(feed_path, "trips.txt")
    routes = read_gtfs_table(feed_path, "routes.txt")
    calendar = _read_optional_gtfs_table(feed_path, "calendar.txt")
    calendar_dates = _read_optional_gtfs_table(feed_path, "calendar_dates.txt")
    frequencies = _read_optional_gtfs_table(feed_path, "frequencies.txt")

    # The trip-segment records (times parsed, trips/routes/stops joined)
    # are the expensive part; build them once for both the edges layer and
    # the schedule table instead of once each.
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
        service_date=service_date,
    )
    stop_edges_gdf = build_gtfs_stop_to_stop_edges_gdf(
        stop_times,
        stops,
        trips,
        routes,
        dataset_name=dataset_name,
        layer_id=layer_id,
        filter_geometry=filter_geometry,
        peak_periods=peak_periods,
        include_hourly_summary=include_hourly_summary,
        records=records,
        service_date=service_date,
    )
    _warn_implausible_times(records, filter_geometry, dataset_name)
    # The nodes are the stops the edges use, and only those: stops.txt
    # often lists stops no trip serves (a regional catalogue, stations,
    # entrances), and an edge crossing the boundary ends at a stop outside
    # the area, which is kept with in_area=False.
    stops_gdf = build_gtfs_stops_gdf(
        stops,
        dataset_name=dataset_name,
        layer_id=layer_id,
        filter_geometry=filter_geometry,
        stop_ids=pd.concat([stop_edges_gdf["from_node"], stop_edges_gdf["to_node"]]),
    )

    schedule_df = None
    if include_schedule_table:
        # Clipped like the edges: the trips of the rest of the feed's
        # region (a whole province or more) are not in the area.
        schedule_df = build_gtfs_schedule_table(
            stop_times,
            stops,
            trips,
            routes,
            dataset_name=dataset_name,
            records=records,
            filter_geometry=filter_geometry,
        )

    if target_crs:
        stops_gdf = stops_gdf.to_crs(target_crs)
        stop_edges_gdf = stop_edges_gdf.to_crs(target_crs)

    return stops_gdf, stop_edges_gdf, schedule_df


# ---------------------------------------------------------------------------
# Short column names of the GTFS edge layer (Shapefile's 10-character limit)
# ---------------------------------------------------------------------------
# Shapefile truncates longer names and resolves collisions with numeric
# suffixes, so the same column ended up named differently depending on the
# format. The short names are applied to all three formats alike.
# The full mapping is in DATA_SCHEMA.md.
_GTFS_PERIOD_ABBR = {"peak_am": "pam", "peak_pm": "ppm", "rest_of_day": "rod"}


def period_codes(
    peak_periods: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Short code of each GTFS period, for the column names.

    The default periods keep ``pam``, ``ppm`` and ``rod``. Other names
    (``gtfs_peak_periods`` with names of your own) get their first three
    letters (``"morning"`` -> ``mor``) if those are unique; otherwise they
    are numbered ``p1``, ``p2``... in order. ``rest_of_day`` is always
    ``rod``. A code has at most three characters, so every column fits the
    Shapefile limit of ten (``tm_mor_wd``).
    """
    names = [*(peak_periods or DEFAULT_PEAK_PERIODS), "rest_of_day"]
    codes = {
        name: _GTFS_PERIOD_ABBR[name] for name in names if name in _GTFS_PERIOD_ABBR
    }
    custom = [name for name in names if name not in _GTFS_PERIOD_ABBR]
    if not custom:
        return {name: codes[name] for name in names}

    def three_letters(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", slugify(name))[:3]

    letters = [three_letters(name) for name in custom]
    taken = set(_GTFS_PERIOD_ABBR.values())  # pam/ppm/rod always mean the defaults
    usable = (
        all(len(code) == 3 for code in letters)
        and len(set(letters)) == len(letters)
        and taken.isdisjoint(letters)
        and not any(re.fullmatch(r"p\d+", code) for code in letters)
    )
    if usable:
        codes.update(zip(custom, letters, strict=True))
    else:
        codes.update({name: f"p{i}" for i, name in enumerate(custom, start=1)})
    return {name: codes[name] for name in names}


_GTFS_DAY_TYPE_ABBR = {"weekday": "wd", "weekend": "we"}


def gtfs_edge_rename(
    peak_periods: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Build the long-to-short name map for the GTFS edges layer.

    The per-period columns use the codes of :func:`period_codes`, so
    periods with names of your own are renamed (and kept) too.
    """
    abbreviations = period_codes(peak_periods)
    rename = {
        "route_short_name": "route_sh",
        "route_long_name": "route_ln",
        "trip_count": "trips",
        "days_of_week_summary": "days_week",
        "hourly_travel_times": "h_tts",
        "hourly_trip_counts": "h_trips",
        "travel_time_seconds_mean": "tts_m",
        "travel_time_seconds_median": "tts_md",
        "travel_time_seconds_min": "tts_mn",
        "travel_time_seconds_max": "tts_mx",
    }
    for period, short_period in abbreviations.items():
        rename[f"travel_time_seconds_mean_{period}"] = f"tts_m_{short_period}"
        rename[f"travel_time_seconds_median_{period}"] = f"tts_md_{short_period}"
        rename[f"trip_count_{period}"] = f"trips_{short_period}"
    for day_type, short_day in _GTFS_DAY_TYPE_ABBR.items():
        rename[f"travel_time_seconds_mean_{day_type}"] = f"tts_m_{short_day}"
        rename[f"trip_count_{day_type}"] = f"trips_{short_day}"
    # Period x day-type is abbreviated further (tm_/trp_) because fitting
    # all four parts doesn't work within ten characters any other way.
    for period, short_period in abbreviations.items():
        for day_type, short_day in _GTFS_DAY_TYPE_ABBR.items():
            suffix = f"{short_period}_{short_day}"
            rename[f"travel_time_seconds_mean_{period}_{day_type}"] = f"tm_{suffix}"
            rename[f"trip_count_{period}_{day_type}"] = f"trp_{suffix}"
    return rename


#: Long-to-short names with the default periods (see :func:`gtfs_edge_rename`).
GTFS_EDGE_RENAME: dict[str, str] = gtfs_edge_rename()


def gtfs_edge_columns(
    peak_periods: Mapping[str, object] | None = None,
) -> list[str]:
    """Long names of the GTFS edge columns written, in order.

    Includes the per-period columns of the periods actually used
    (``gtfs_peak_periods``, or the defaults), so none is dropped.
    """
    periods = list(period_codes(peak_periods))
    day_types = list(_GTFS_DAY_TYPE_ABBR)
    return [
        "edge_id",
        "from_node",
        "to_node",
        "route_id",
        "route_short_name",
        "route_long_name",
        "trip_count",
        "travel_time_seconds_mean",
        "travel_time_seconds_median",
        "travel_time_seconds_min",
        "travel_time_seconds_max",
        *(f"travel_time_seconds_mean_{period}" for period in periods),
        *(f"travel_time_seconds_median_{period}" for period in periods),
        *(f"trip_count_{period}" for period in periods),
        *(
            f"{metric}_{day_type}"
            for day_type in day_types
            for metric in ("travel_time_seconds_mean", "trip_count")
        ),
        "days_of_week_summary",
        "hourly_travel_times",
        "hourly_trip_counts",
        "geometry",
        *(
            f"{metric}_{period}_{day_type}"
            for period in periods
            for day_type in day_types
            for metric in ("travel_time_seconds_mean", "trip_count")
        ),
    ]


# ---------------------------------------------------------------------------
# Download (NAP) and standardize the GTFS layers
# ---------------------------------------------------------------------------
# The NAP names its files after the moment they were generated
# (``20261119_030028_GenValenciana_Interurbano.zip``).
_DOWNLOAD_STAMP = re.compile(r"^\d{8}_\d{6}_")


def dataset_name_from_file(zip_path: str | Path, taken: set[str] | None = None) -> str:
    """Stable dataset name of a GTFS file: its name without the NAP timestamp.

    ``20261119_030028_GenValenciana_Interurbano.zip`` gives
    ``genvalenciana_interurbano``, the same on every download, so folders
    and edge ids don't change from one download to the next. If ``taken``
    already holds the name (two files of the same dataset), ``_2``,
    ``_3``... is added; the name returned is added to ``taken``.
    """
    stem = Path(zip_path).stem
    rest = _DOWNLOAD_STAMP.sub("", stem)
    name = slugify(rest if rest.strip("_ ") else stem)
    return name if taken is None else _unique_name(name, taken)


def _unique_name(name: str, taken: set[str]) -> str:
    """``name``, or ``name_2``, ``name_3``... if taken; added to ``taken``."""
    unique, number = name, 2
    while unique in taken:
        unique, number = f"{name}_{number}", number + 1
    taken.add(unique)
    return unique


# ---------------------------------------------------------------------------
# GTFS feeds from outside the NAP (gtfs_feeds)
# ---------------------------------------------------------------------------
#: What ``gtfs_feeds`` accepts: ``{name: source}``, or a list of sources or
#: ``(name, source)`` pairs. A source is an http(s) address or a local GTFS
#: zip or folder.
GTFSFeeds = Mapping[str, "str | Path"] | Sequence["str | Path | tuple[str, str | Path]"]


def _is_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def user_feed_sources(feeds: GTFSFeeds | None) -> list[tuple[str | None, str]]:
    """Check ``gtfs_feeds`` and return it as ``[(name or None, source)]``.

    Raises ``ValueError`` for an empty source, a name that is not text or an
    address other than http(s), and ``FileNotFoundError`` for a local file or
    folder that doesn't exist, so a mistake fails before anything is
    downloaded.
    """
    if feeds is None:
        return []
    if isinstance(feeds, (str, Path)):
        feeds = [feeds]
    items: list[tuple[object, object]] = (
        list(feeds.items())
        if isinstance(feeds, Mapping)
        else [
            item if isinstance(item, tuple) and len(item) == 2 else (None, item)
            for item in feeds
        ]
    )
    sources: list[tuple[str | None, str]] = []
    for name, source in items:
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError(
                f"gtfs_feeds: a feed name must be text. Received: {name!r}"
            )
        text = str(source).strip() if source is not None else ""
        if not text:
            raise ValueError(f"gtfs_feeds: empty source for feed {name!r}.")
        if _is_url(text):
            sources.append((name, text))
            continue
        if "://" in text:
            raise ValueError(
                "gtfs_feeds: only http(s) addresses can be downloaded. "
                f"Received: {text}"
            )
        path = Path(text).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"gtfs_feeds: no such GTFS file or folder: {text}")
        sources.append((name, str(path)))
    return sources


def _feed_file_name(source: str) -> str:
    """File name of a source: the last part of its address or path."""
    from urllib.parse import urlparse

    path = urlparse(source).path if _is_url(source) else source
    return Path(path.rstrip("/")).name or "gtfs"


def edge_network_key(edges: pd.DataFrame) -> frozenset[tuple[str, str, str]]:
    """Return the network an edges layer describes: its ``(route_id, from, to)``.

    Two feeds with the same key give the same routes between the same
    stops in the area, whatever their file, dates covered or trips: one
    repeats the other. Empty for a layer without edges.
    """
    if edges.empty or not {"route_id", "from_node", "to_node"} <= set(edges.columns):
        return frozenset()
    rows = edges[["route_id", "from_node", "to_node"]].astype(str)
    return frozenset(rows.itertuples(index=False, name=None))


def _fetch_user_feeds(
    sources: list[tuple[str | None, str]],
    zip_dir: Path,
    report: list[dict[str, object]],
    taken: Iterable[str] = (),
) -> list[tuple[Path, str | None, dict[str, object] | None]]:
    """Download the ``gtfs_feeds`` addresses into ``zip_dir``; keep local paths.

    Returns ``(path, name, report row)`` per feed obtained. Every feed gets a
    report row with ``source`` ``"user"``; a download that fails is
    ``failed``, with the error, and is not returned. ``taken`` are the file
    stems already in use (the NAP's zips of this download), which a feed is
    numbered to avoid rather than overwrite.
    """
    import requests

    from ._overpass import user_agent

    fetched: list[tuple[Path, str | None, dict[str, object] | None]] = []
    files_taken: set[str] = set(taken)
    for name, source in sources:
        file_name = _feed_file_name(source)
        row: dict[str, object] = {
            "source": "user",
            "dataset_name": name or file_name,
            "file_name": file_name,
        }
        report.append(row)
        if not _is_url(source):
            row.update(status="downloaded", path=source)
            fetched.append((Path(source), name, row))
            continue
        stem = _unique_name(slugify(name or Path(file_name).stem), files_taken)
        target = Path(zip_dir) / f"{stem}.zip"
        try:
            response = requests.get(
                source, headers={"User-Agent": user_agent()}, timeout=(30, 300)
            )
            response.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
        except Exception as exc:  # noqa: BLE001 - reported, the rest go on
            logger.warning("Could not download GTFS feed %s: %s", source, exc)
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            continue
        row.update(status="downloaded", path=str(target))
        fetched.append((target, name, row))
    return fetched


def download_gtfs(
    area_name: str,
    boundary: gpd.GeoDataFrame,
    zip_dir: str | Path,
    *,
    modes: Iterable[int],
    output_crs: str = "EPSG:4326",
    clip_to_boundary: bool = True,
    api_key: str | None = None,
    hour_band_size: int = 1,
    hour_range: tuple[int, int] | None = None,
    peak_periods: dict[str, tuple[int, int]] | None = None,
    include_schedule_table: bool = True,
    province_codes: Iterable[int] | None = None,
    buffer_m: float = 0.0,
    skipped: dict[str, str] | None = None,
    report: list[dict[str, object]] | None = None,
    service_date: datetime.date | str | None = None,
    feeds: GTFSFeeds | None = None,
    use_nap: bool = True,
) -> dict[str, dict[str, object]]:
    """Download NAP GTFS data and return normalized layers per dataset.

    Returns ``{dataset: {"nodes": gdf, "edges": gdf, "schedule": DataFrame
    | None, "mode": "bus" | "train"}}`` (see :func:`normalize_gtfs_feed`).

    ``province_codes`` are the INE codes of the provinces whose feeds are
    downloaded; if not given, they're taken from the ``boundary``'s
    ``prov_codes`` column (filled in by ``find_area``). A dataset published
    for several of them is downloaded once. See
    ``_nap.download_gtfs_nap_zips``.

    ``hour_band_size`` / ``hour_range`` / ``peak_periods`` /
    ``include_schedule_table`` are forwarded to the GTFS standardization
    step; the resulting columns are described in the "GTFS edges" and
    "GTFS schedule" sections of ``DATA_SCHEMA.md``.

    ``buffer_m`` widens the clipping area by that many metres (see
    :func:`study_area_geometry`), so stops just across the municipal
    boundary are kept.

    Only feeds of the requested ``modes`` are kept, judging by the feed's
    own route types (:func:`infer_transport_mode`): the NAP declares some
    feeds as both bus and rail (e.g. Cercanías Renfe), so asking for bus
    may also download a train feed. Such a feed is skipped and logged and,
    if a ``skipped`` dict is given, added to it as ``{dataset: mode}``.

    If a ``report`` list is given, it gets one row per NAP dataset or file
    (see ``_nap.download_gtfs_nap_zips``), completed with what happened to
    each downloaded file: ``status`` ``written``, ``nothing_in_area``,
    ``skipped_mode`` or ``failed_processing`` (with ``error``), its
    ``dataset`` name and ``mode``, and how many ``routes``, ``stops`` and
    ``edges`` it has in the area.

    ``service_date`` (a date, ``"YYYY-MM-DD"`` or ``"YYYYMMDD"``) keeps only
    the trips that run that day (see :func:`normalize_gtfs_feed`). A feed
    with no trip that day is not written: its report row says
    ``no_service_on_date``, with the dates its calendar covers in ``error``.

    ``feeds`` adds GTFS feeds from outside the NAP: ``{name: source}``, or a
    list of sources or ``(name, source)`` pairs, a source being an http(s)
    address (downloaded into ``zip_dir``) or a local zip or folder. They are
    processed like the NAP's, after them; a feed's ``dataset`` is its name
    (or its file name), numbered if another feed already has it, and its
    report row has ``source`` ``"user"`` (``"nap"`` for the NAP's). With
    ``use_nap=False`` the NAP is skipped and only ``feeds`` are used, so no
    API key is needed.
    """
    day = None if service_date is None else parse_service_date(service_date)
    if province_codes is None and "prov_codes" in getattr(boundary, "columns", []):
        value = boundary["prov_codes"].iloc[0]
        province_codes = list(value) if isinstance(value, (list, tuple)) else None

    check_hour_options(hour_range, hour_band_size, peak_periods)
    sources = user_feed_sources(feeds)
    report_rows: list[dict[str, object]] = [] if report is None else report
    zips: list[Path] = []
    if use_nap:
        zips = list(
            download_gtfs_nap_zips(
                area_name,
                zip_dir,
                modes=modes,
                api_key=api_key,
                province_codes=province_codes,
                report=report_rows,
            )
        )
        for nap_row in report_rows:
            nap_row.setdefault("source", "nap")
    by_path = {str(row["path"]): row for row in report_rows if row.get("path")}
    # (file, name given or None, its report row): the NAP's, then the user's.
    feed_files: list[tuple[Path, str | None, dict[str, object] | None]] = [
        (Path(path), None, by_path.get(str(path))) for path in zips
    ]
    feed_files += _fetch_user_feeds(
        sources, Path(zip_dir), report_rows, taken={path.stem for path in zips}
    )

    def outcome(
        zip_path: Path, row: dict[str, object] | None, **fields: object
    ) -> None:
        """Complete the report row of a downloaded file."""
        if row is None:
            row = {"file_name": Path(zip_path).name, "path": str(zip_path)}
            report_rows.append(row)
        row.update(fields)

    filter_geometry = None
    if clip_to_boundary and not boundary.empty:
        filter_geometry = study_area_geometry(boundary, buffer_m=buffer_m)

    wanted = {int(mode) for mode in modes}
    results: dict[str, dict[str, object]] = {}
    taken: set[str] = set()
    # The network of each feed written so far, to spot a feed that repeats
    # one (e.g. a gtfs_feeds address that serves a NAP dataset).
    networks: dict[frozenset[tuple[str, str, str]], str] = {}
    for zip_path, given_name, row in feed_files:
        dataset_name = (
            _unique_name(slugify(given_name), taken)
            if given_name
            else dataset_name_from_file(zip_path, taken)
        )
        # "bus" or "train", from the feed's route_type: decides the output
        # folder, and whether the feed was asked for at all.
        mode = infer_transport_mode(zip_path)
        if _NAP_ID_OF_MODE[mode] not in wanted:
            logger.info(
                "Skipping feed %s: its routes are %s, which was not requested.",
                zip_path.name,
                mode,
            )
            if skipped is not None:
                skipped[dataset_name] = mode
            outcome(
                zip_path, row, dataset=dataset_name, mode=mode, status="skipped_mode"
            )
            continue
        try:
            no_service = "" if day is None else _no_service_message(zip_path, day)
            if no_service:
                outcome(
                    zip_path,
                    row,
                    dataset=dataset_name,
                    mode=mode,
                    status="no_service_on_date",
                    error=no_service,
                )
                continue
            stops, stop_edges, schedule = normalize_gtfs_feed(
                zip_path,
                dataset_name=dataset_name,
                layer_id=dataset_name,
                filter_geometry=filter_geometry,
                target_crs=output_crs,
                hour_band_size=hour_band_size,
                hour_range=hour_range,
                peak_periods=peak_periods,
                include_schedule_table=include_schedule_table,
                service_date=day,
            )
        except Exception as exc:  # noqa: BLE001 - a corrupt feed shouldn't break everything
            logger.warning("Could not normalize feed %s: %s", zip_path.name, exc)
            outcome(
                zip_path,
                row,
                dataset=dataset_name,
                mode=mode,
                status="failed_processing",
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        # Only a feed of gtfs_feeds is skipped: the NAP's are what the NAP
        # lists, and come first.
        network_key = edge_network_key(stop_edges)
        from_user = row is not None and row.get("source") == "user"
        if from_user and network_key and network_key in networks:
            same = networks[network_key]
            logger.warning(
                "Skipping feed %s: it has the same routes and stops in the area as %s.",
                zip_path.name,
                same,
            )
            outcome(
                zip_path,
                row,
                dataset=dataset_name,
                mode=mode,
                status="duplicate",
                error=f"same routes and stops in the area as {same}",
            )
            continue
        if network_key:
            networks[network_key] = dataset_name
        outcome(
            zip_path,
            row,
            dataset=dataset_name,
            mode=mode,
            status="written" if not stop_edges.empty else "nothing_in_area",
            routes=(
                int(stop_edges["route_id"].nunique())
                if "route_id" in stop_edges.columns
                else None
            ),
            stops=int(len(stops)),
            edges=int(len(stop_edges)),
        )
        results[dataset_name] = {
            "nodes": stops,
            "edges": stop_edges,
            "schedule": schedule,
            "mode": mode,
        }
    return results
