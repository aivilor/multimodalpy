"""Estandarizacion de datos GTFS. Limpieza de datos de bus + tren.

Este modulo tiene dos bloques:

1. Estandarizacion de un feed GTFS (ZIP o carpeta) en capas GeoPandas:
   - ``nodes_stops``              : paradas como puntos.
   - ``edges``                    : aristas parada-a-parada (secuencia de viajes),
                                     una fila por par de paradas, con tiempo de
                                     viaje y desglose por periodo horario.
   - ``edges_shapes_reference``   : geometria de recorrido (shapes.txt).
   - ``schedule`` (tabla plana, no espacial): un registro por viaje y tramo,
     para quien necesite el detalle de horarios sin perder granularidad.

2. Limpieza de bus + tren: proyeccion de paradas sobre el recorrido (snap) y
   division de las lineas del recorrido en tramos entre paradas consecutivas,
   para obtener una topologia parada-tramo-parada coherente.

No se escribe ningun fichero aqui; eso lo hace ``get_network``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

if TYPE_CHECKING:
    import geopandas as gpd
    import pandas as pd


SOURCE_CRS = "EPSG:4326"

# Periodos horarios por defecto para el desglose de la opcion A.
# "rest_of_day" se asigna automaticamente a cualquier hora no cubierta aqui.
DEFAULT_PEAK_PERIODS: dict[str, tuple[int, int]] = {
    "peak_am": (7, 9),    # punta manana, 07:00-09:00
    "peak_pm": (17, 20),  # punta tarde, 17:00-20:00
}


# ---------------------------------------------------------------------------
# Lectura de tablas GTFS
# ---------------------------------------------------------------------------
def read_gtfs_table(feed_path: str | Path, table_name: str) -> "pd.DataFrame":
    """Lee una tabla GTFS desde un ZIP o desde una carpeta GTFS extraida."""
    import pandas as pd

    feed_path = Path(feed_path)
    if feed_path.is_dir():
        return pd.read_csv(feed_path / table_name, dtype=str, low_memory=False)

    with ZipFile(feed_path) as zf:
        if table_name not in zf.namelist():
            raise FileNotFoundError(f"{table_name} no se encuentra en {feed_path}")
        with zf.open(table_name) as file:
            return pd.read_csv(file, dtype=str, low_memory=False)


def _read_optional_gtfs_table(feed_path: str | Path, table_name: str) -> "pd.DataFrame | None":
    try:
        return read_gtfs_table(feed_path, table_name)
    except FileNotFoundError:
        return None


def _existing_columns(df: "pd.DataFrame", columns: list[str]) -> list[str]:
    return [column for column in columns if column in df.columns]


def _unique_join(values) -> str | None:
    clean = sorted({str(value) for value in values if value is not None and str(value) != "nan"})
    return "|".join(clean) if clean else None


def _filter_by_geometry(gdf: "gpd.GeoDataFrame", filter_geometry) -> "gpd.GeoDataFrame":
    if filter_geometry is None:
        return gdf
    return gdf[gdf.geometry.intersects(filter_geometry)].copy()


# ---------------------------------------------------------------------------
# Horas GTFS (permiten valores >= 24:00:00 para servicios nocturnos)
# ---------------------------------------------------------------------------
def gtfs_time_to_seconds(time_str) -> "float":
    """Convierte 'HH:MM:SS' (con HH pudiendo ser >= 24) a segundos desde medianoche.

    Devuelve NaN si el valor es nulo o no tiene el formato esperado.
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


def seconds_to_hour_band(seconds, *, band_size_hours: int = 1) -> "str | None":
    """Agrupa segundos-desde-medianoche en una franja horaria tipo '08-09'."""
    import math

    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return None
    hour = int((seconds // 3600) % 24)
    band_start = (hour // band_size_hours) * band_size_hours
    band_end = band_start + band_size_hours
    return f"{band_start:02d}-{band_end:02d}"


def classify_period(seconds, peak_periods: dict[str, tuple[int, int]] | None = None) -> "str | None":
    """Clasifica un instante (segundos desde medianoche) en punta_manana /
    punta_tarde / resto_del_dia, segun ``peak_periods`` (por defecto
    ``DEFAULT_PEAK_PERIODS``). Devuelve ``None`` si no hay hora disponible.
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


def _pack_key_value(pairs: "pd.Series", *, decimals: int = 0) -> "str | None":
    """Empaqueta pares (etiqueta, valor) ordenados en 'etiqueta:valor|etiqueta:valor'."""
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
# Construccion de capas normalizadas
# ---------------------------------------------------------------------------
def build_gtfs_stops_gdf(
    stops: "pd.DataFrame",
    *,
    dataset_name: str,
    layer_id: str,
    filter_geometry=None,
) -> "gpd.GeoDataFrame":
    """Capa de paradas (nodos) a partir de ``stops.txt``."""
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


def _attach_service_days(
    trips: "pd.DataFrame",
    calendar: "pd.DataFrame | None",
    calendar_dates: "pd.DataFrame | None",
) -> "pd.DataFrame":
    """Anade a ``trips`` una columna ``days_active`` (num. dias/semana con servicio)."""
    import pandas as pd

    if "service_id" not in trips.columns:
        trips = trips.copy()
        trips["days_active"] = pd.NA
        return trips

    trips = trips.copy()
    weekday_cols = [
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    ]

    if calendar is not None and set(weekday_cols).issubset(calendar.columns):
        cal = calendar.copy()
        for col in weekday_cols:
            cal[col] = pd.to_numeric(cal[col], errors="coerce").fillna(0)
        cal["days_active"] = cal[weekday_cols].sum(axis=1)
        service_days = cal.set_index("service_id")["days_active"]
    else:
        service_days = pd.Series(dtype="float64")

    if calendar_dates is not None and "exception_type" in calendar_dates.columns:
        cd = calendar_dates.copy()
        cd["exception_type"] = pd.to_numeric(cd["exception_type"], errors="coerce")
        added = cd[cd["exception_type"] == 1].groupby("service_id").size()
        removed = cd[cd["exception_type"] == 2].groupby("service_id").size()
        service_days = service_days.add(added, fill_value=0).sub(removed, fill_value=0).clip(lower=0)

    trips["days_active"] = pd.NA if service_days.empty else trips["service_id"].map(service_days)
    return trips


def _build_stop_to_stop_trip_records(
    stop_times: "pd.DataFrame",
    stops: "pd.DataFrame",
    trips: "pd.DataFrame",
    routes: "pd.DataFrame",
    *,
    dataset_name: str,
    calendar: "pd.DataFrame | None" = None,
    calendar_dates: "pd.DataFrame | None" = None,
    frequencies: "pd.DataFrame | None" = None,
    hour_band_size: int = 1,
    hour_range: tuple[int, int] | None = None,
    peak_periods: dict[str, tuple[int, int]] | None = None,
) -> "pd.DataFrame":
    """Construye la tabla intermedia, sin agregar, de un registro por (viaje,
    tramo entre paradas consecutivas). La reutilizan tanto la capa agregada de
    aristas (``build_gtfs_stop_to_stop_edges_gdf``) como la tabla plana de
    horarios (``build_gtfs_schedule_table``), para no duplicar la logica de
    calculo de tiempos y merges.
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
    work["next_arrival_time"] = work.groupby("trip_id")["arrival_time"].shift(-1) if "arrival_time" in work.columns else None
    work = work.dropna(subset=["next_stop_id"]).copy()

    work["travel_time_seconds"] = work["next_arrival_seconds"] - work["departure_seconds"]
    work.loc[work["travel_time_seconds"] < 0, "travel_time_seconds"] = float("nan")

    work["hour_band"] = work["departure_seconds"].apply(
        lambda s: seconds_to_hour_band(s, band_size_hours=hour_band_size)
    )
    work["period"] = work["departure_seconds"].apply(lambda s: classify_period(s, peak_periods))

    if hour_range is not None:
        start_h, end_h = hour_range
        hour_of_day = (work["departure_seconds"] // 3600) % 24
        work = work[(hour_of_day >= start_h) & (hour_of_day < end_h)].copy()

    stop_lookup = stops[["stop_id", "stop_name", "stop_lat", "stop_lon"]].copy()
    stop_lookup["stop_lat"] = pd.to_numeric(stop_lookup["stop_lat"], errors="coerce")
    stop_lookup["stop_lon"] = pd.to_numeric(stop_lookup["stop_lon"], errors="coerce")
    stop_lookup = stop_lookup.dropna(subset=["stop_lat", "stop_lon"]).drop_duplicates("stop_id")

    from_lookup = stop_lookup.rename(columns={
        "stop_id": "from_stop_id", "stop_name": "from_stop_name",
        "stop_lat": "from_stop_lat", "stop_lon": "from_stop_lon",
    })
    to_lookup = stop_lookup.rename(columns={
        "stop_id": "to_stop_id", "stop_name": "to_stop_name",
        "stop_lat": "to_stop_lat", "stop_lon": "to_stop_lon",
    })

    edges = work.rename(columns={"stop_id": "from_stop_id", "next_stop_id": "to_stop_id"}).copy()

    trips_work = _attach_service_days(trips, calendar, calendar_dates)

    if frequencies is not None and not frequencies.empty and "trip_id" in frequencies.columns:
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
        ["trip_id", "route_id", "service_id", "shape_id", "trip_headsign", "days_active", "frequency_trip_count"],
    )
    route_cols = _existing_columns(routes, ["route_id", "route_short_name", "route_long_name", "route_type"])
    edges = edges.merge(trips_work[trip_cols].drop_duplicates(subset=["trip_id"]), on="trip_id", how="left")
    if "route_id" in route_cols:
        edges = edges.merge(routes[route_cols].drop_duplicates(), on="route_id", how="left")
    edges = edges.merge(from_lookup, on="from_stop_id", how="left")
    edges = edges.merge(to_lookup, on="to_stop_id", how="left")
    edges = edges.dropna(subset=["from_stop_lon", "from_stop_lat", "to_stop_lon", "to_stop_lat"]).copy()
    edges["edge_id"] = dataset_name + "_" + edges["from_stop_id"].astype(str) + "_" + edges["to_stop_id"].astype(str)
    return edges


def build_gtfs_schedule_table(
    stop_times: "pd.DataFrame",
    stops: "pd.DataFrame",
    trips: "pd.DataFrame",
    routes: "pd.DataFrame",
    *,
    dataset_name: str,
    calendar: "pd.DataFrame | None" = None,
    calendar_dates: "pd.DataFrame | None" = None,
    frequencies: "pd.DataFrame | None" = None,
    hour_band_size: int = 1,
    hour_range: tuple[int, int] | None = None,
    peak_periods: dict[str, tuple[int, int]] | None = None,
) -> "pd.DataFrame":
    """Opcion B: tabla plana (sin geometria), un registro por viaje y tramo.

    Pensada para unirse con la capa ``edges`` por ``edge_id`` cuando se necesita
    el detalle completo de horarios (cada paso de cada viaje), sin perder esa
    granularidad en la capa espacial agregada.
    """
    records = _build_stop_to_stop_trip_records(
        stop_times, stops, trips, routes,
        dataset_name=dataset_name,
        calendar=calendar, calendar_dates=calendar_dates, frequencies=frequencies,
        hour_band_size=hour_band_size, hour_range=hour_range, peak_periods=peak_periods,
    )
    columns = _existing_columns(records, [
        "edge_id", "trip_id", "route_id", "route_short_name", "trip_headsign",
        "from_stop_id", "to_stop_id", "departure_time", "arrival_time",
        "travel_time_seconds", "hour_band", "period", "service_id", "days_active",
    ])
    return records[columns].reset_index(drop=True)


def build_gtfs_stop_to_stop_edges_gdf(
    stop_times: "pd.DataFrame",
    stops: "pd.DataFrame",
    trips: "pd.DataFrame",
    routes: "pd.DataFrame",
    *,
    dataset_name: str,
    layer_id: str,
    filter_geometry=None,
    calendar: "pd.DataFrame | None" = None,
    calendar_dates: "pd.DataFrame | None" = None,
    frequencies: "pd.DataFrame | None" = None,
    hour_band_size: int = 1,
    hour_range: tuple[int, int] | None = None,
    peak_periods: dict[str, tuple[int, int]] | None = None,
    include_hourly_summary: bool = True,
) -> "gpd.GeoDataFrame":
    """Capa de aristas parada-a-parada: una fila por par de paradas.

    Incluye tres cosas nuevas, todas manteniendo el formato tabular:

    - Opcion A: columnas por periodo (``peak_am``/``peak_pm``/``rest_of_day``
      por defecto) para ``travel_time_seconds_mean``, ``travel_time_seconds_median``
      y ``trip_count``, p.ej. ``travel_time_seconds_mean_peak_am``.
    - Opcion C: si ``include_hourly_summary=True``, dos columnas de texto
      empaquetado ``hourly_travel_times`` / ``hourly_trip_counts`` tipo
      ``"07-08:320|08-09:280|17-18:310"`` con el detalle por hora, sin generar
      filas adicionales.
    - Los agregados globales de siempre (``trip_count``, ``travel_time_seconds_mean``,
      etc.) sobre el conjunto completo de viajes, no solo el periodo punta.

    Para el detalle completo por viaje (sin agregar en absoluto), usar
    ``build_gtfs_schedule_table`` (opcion B).
    """
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString

    edges = _build_stop_to_stop_trip_records(
        stop_times, stops, trips, routes,
        dataset_name=dataset_name,
        calendar=calendar, calendar_dates=calendar_dates, frequencies=frequencies,
        hour_band_size=hour_band_size, hour_range=hour_range, peak_periods=peak_periods,
    )

    group_cols = _existing_columns(
        edges,
        [
            "from_stop_id", "to_stop_id", "route_id", "route_short_name", "route_long_name",
            "route_type", "from_stop_name", "to_stop_name",
            "from_stop_lon", "from_stop_lat", "to_stop_lon", "to_stop_lat",
        ],
    )

    # --- Agregados globales (todas las horas juntas) ---
    aggregations = {
        "trip_count": ("trip_id", "nunique"),
        "travel_time_seconds_mean": ("travel_time_seconds", "mean"),
        "travel_time_seconds_median": ("travel_time_seconds", "median"),
        "travel_time_seconds_min": ("travel_time_seconds", "min"),
        "travel_time_seconds_max": ("travel_time_seconds", "max"),
    }
    if "service_id" in edges.columns:
        aggregations["service_count"] = ("service_id", "nunique")
    if "days_active" in edges.columns:
        aggregations["days_active_mean"] = ("days_active", "mean")
    if "frequency_trip_count" in edges.columns:
        aggregations["frequency_trip_count_sum"] = ("frequency_trip_count", "sum")

    grouped = edges.groupby(group_cols, dropna=False).agg(**aggregations).reset_index()
    for optional_col in ("service_count", "days_active_mean", "frequency_trip_count_sum"):
        if optional_col not in grouped.columns:
            grouped[optional_col] = None

    # --- Opcion A: columnas por periodo (punta_manana / punta_tarde / resto_del_dia) ---
    period_rows = edges.dropna(subset=["period"])
    if not period_rows.empty:
        period_grouped = period_rows.groupby(group_cols + ["period"], dropna=False).agg(
            travel_time_seconds_mean=("travel_time_seconds", "mean"),
            travel_time_seconds_median=("travel_time_seconds", "median"),
            trip_count=("trip_id", "nunique"),
        ).reset_index()
        pivoted = period_grouped.pivot_table(
            index=group_cols, columns="period",
            values=["travel_time_seconds_mean", "travel_time_seconds_median", "trip_count"],
        )
        pivoted.columns = [f"{metric}_{period}" for metric, period in pivoted.columns]
        pivoted = pivoted.reset_index()
        grouped = grouped.merge(pivoted, on=group_cols, how="left")

    all_period_names = list((peak_periods or DEFAULT_PEAK_PERIODS).keys()) + ["rest_of_day"]
    for period_name in all_period_names:
        for metric in ("travel_time_seconds_mean", "travel_time_seconds_median", "trip_count"):
            col = f"{metric}_{period_name}"
            if col not in grouped.columns:
                grouped[col] = None

    # --- Opcion C: resumen horario empaquetado en texto, sin filas nuevas ---
    if include_hourly_summary:
        hour_rows = edges.dropna(subset=["hour_band"])
        if not hour_rows.empty:
            hour_grouped = hour_rows.groupby(group_cols + ["hour_band"], dropna=False).agg(
                travel_time_seconds_mean=("travel_time_seconds", "mean"),
                trip_count=("trip_id", "nunique"),
            ).reset_index()
            packed = hour_grouped.groupby(group_cols).apply(
                lambda df: pd.Series({
                    "hourly_travel_times": _pack_key_value(
                        df.set_index("hour_band")["travel_time_seconds_mean"]
                    ),
                    "hourly_trip_counts": _pack_key_value(
                        df.set_index("hour_band")["trip_count"]
                    ),
                })
            ).reset_index()
            grouped = grouped.merge(packed, on=group_cols, how="left")
        for col in ("hourly_travel_times", "hourly_trip_counts"):
            if col not in grouped.columns:
                grouped[col] = None

    grouped["geometry"] = grouped.apply(
        lambda row: LineString(
            [(row["from_stop_lon"], row["from_stop_lat"]), (row["to_stop_lon"], row["to_stop_lat"])]
        ),
        axis=1,
    )
    gdf = gpd.GeoDataFrame(grouped, geometry="geometry", crs=SOURCE_CRS)
    gdf = _filter_by_geometry(gdf, filter_geometry)
    gdf["dataset_name"] = dataset_name
    gdf["layer_id"] = layer_id
    gdf["edge_id"] = dataset_name + "_" + gdf["from_stop_id"].astype(str) + "_" + gdf["to_stop_id"].astype(str)
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
) -> tuple["gpd.GeoDataFrame", "gpd.GeoDataFrame", "gpd.GeoDataFrame", "pd.DataFrame | None"]:
    """Normaliza un feed GTFS en (paradas, aristas, recorridos, horario).

    El cuarto elemento devuelto, ``schedule`` (opcion B), es una tabla plana
    (``pandas.DataFrame``, sin geometria) con el detalle viaje-a-viaje; es
    ``None`` si ``include_schedule_table=False``.

    ``peak_periods`` (opcion A) por defecto separa punta_manana (07-09),
    punta_tarde (17-20) y resto_del_dia (todo lo demas); se puede pasar un
    dict propio con el mismo formato para cambiar los tramos.
    """
    stops = read_gtfs_table(feed_path, "stops.txt")
    stop_times = read_gtfs_table(feed_path, "stop_times.txt")
    trips = read_gtfs_table(feed_path, "trips.txt")
    routes = read_gtfs_table(feed_path, "routes.txt")
    calendar = _read_optional_gtfs_table(feed_path, "calendar.txt")
    calendar_dates = _read_optional_gtfs_table(feed_path, "calendar_dates.txt")
    frequencies = _read_optional_gtfs_table(feed_path, "frequencies.txt")

    stops_gdf = build_gtfs_stops_gdf(
        stops, dataset_name=dataset_name, layer_id=layer_id, filter_geometry=filter_geometry,
    )
    stop_edges_gdf = build_gtfs_stop_to_stop_edges_gdf(
        stop_times, stops, trips, routes,
        dataset_name=dataset_name, layer_id=layer_id, filter_geometry=filter_geometry,
        calendar=calendar, calendar_dates=calendar_dates, frequencies=frequencies,
        hour_band_size=hour_band_size, hour_range=hour_range, peak_periods=peak_periods,
        include_hourly_summary=include_hourly_summary,
    )


    schedule_df = None
    if include_schedule_table:
        schedule_df = build_gtfs_schedule_table(
            stop_times, stops, trips, routes,
            dataset_name=dataset_name,
            calendar=calendar, calendar_dates=calendar_dates, frequencies=frequencies,
            hour_band_size=hour_band_size, hour_range=hour_range, peak_periods=peak_periods,
        )

    if target_crs:
        stops_gdf = stops_gdf.to_crs(target_crs)
        stop_edges_gdf = stop_edges_gdf.to_crs(target_crs)

    return stops_gdf, stop_edges_gdf, schedule_df


