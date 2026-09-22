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
def _strip_gtfs_whitespace(df: "pd.DataFrame") -> "pd.DataFrame":
    """Quita el relleno de espacios de cabeceras y valores de una tabla GTFS.

    Algunos feeds oficiales publican los CSV con las columnas alineadas a un
    ancho fijo (p. ej. el de Cercanias de RENFE, cuya cabecera literal es
    ``"stop_sequence            ..."`` y cuyos valores son ``"005        ..."``).
    Sin recortar, cualquier acceso por nombre de columna revienta con KeyError
    y los identificadores no casan entre tablas.
    """
    from pandas.api.types import is_object_dtype, is_string_dtype

    df.columns = [str(column).strip() for column in df.columns]
    for column in df.columns:
        series = df[column]
        # Las tablas se leen con dtype=str, pero el dtype concreto depende de
        # la version de pandas ("object" hasta 2.x, "str" desde 3.0).
        if is_string_dtype(series) or is_object_dtype(series):
            df[column] = series.str.strip()
    return df


def read_gtfs_table(feed_path: str | Path, table_name: str) -> "pd.DataFrame":
    """Lee una tabla GTFS desde un ZIP o desde una carpeta GTFS extraida."""
    import pandas as pd

    feed_path = Path(feed_path)
    if feed_path.is_dir():
        return _strip_gtfs_whitespace(
            pd.read_csv(feed_path / table_name, dtype=str, low_memory=False)
        )

    with ZipFile(feed_path) as zf:
        if table_name not in zf.namelist():
            raise FileNotFoundError(f"{table_name} no se encuentra en {feed_path}")
        with zf.open(table_name) as file:
            return _strip_gtfs_whitespace(
                pd.read_csv(file, dtype=str, low_memory=False)
            )


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


_WEEKDAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _union_days_of_week(values) -> str | None:
    """Une varios textos 'lunes|martes' (uno por viaje) en un unico resumen,
    ordenado de lunes a domingo, sin duplicados."""
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


def _classify_day_type(runs_weekday, runs_weekend) -> str | None:
    """Combina los flags booleanos de actividad en una etiqueta legible."""
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
    trips: "pd.DataFrame",
    calendar: "pd.DataFrame | None",
    calendar_dates: "pd.DataFrame | None",
) -> "pd.DataFrame":
    """Anade a ``trips`` columnas sobre en que dias circula el servicio:

    - ``days_active``: num. dias/semana con servicio (recuento, como antes).
    - ``active_days_of_week``: texto tipo ``"friday|monday|thursday"`` con los
      dias de la semana concretos en que el servicio esta activo.
    - ``runs_weekday`` / ``runs_weekend``: booleanos, ``True`` si el servicio
      circula algun dia laborable (lunes-viernes) / de fin de semana
      (sabado-domingo) respectivamente.
    - ``day_type``: etiqueta resumen -- ``"weekday"``, ``"weekend"``,
      ``"weekday_and_weekend"`` o ``None`` si no se puede determinar.

    La fuente principal es ``calendar.txt`` (el patron semanal declarado). Si
    no existe, se infieren los dias de la semana a partir de las fechas
    anadidas en ``calendar_dates.txt`` (``exception_type == 1``).
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

    # --- Info declarada en calendar.txt (si existe) ---
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

    # Servicios con un patron semanal real declarado en calendar.txt (al menos
    # un dia activo). Algunos feeds usan calendar.txt como "stub" con todas
    # las banderas a 0 y delegan las fechas concretas a calendar_dates.txt;
    # esos servicios se tratan igual que si no estuvieran en calendar.txt.
    strong_ids = set(
        cal_runs_weekday[cal_runs_weekday.fillna(False) | cal_runs_weekend.fillna(False)].index
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

        # Dias de la semana inferidos a partir de las fechas anadidas
        # (exception_type == 1). Se usan para los servicios "debiles": los
        # que no aparecen en calendar.txt, o aparecen con todas las banderas
        # semanales a 0.
        if "date" in cd.columns:
            added_rows = cd[cd["exception_type"] == 1].copy()
            added_rows["weekday_name"] = pd.to_datetime(
                added_rows["date"], format="%Y%m%d", errors="coerce"
            ).dt.day_name().str.lower()
            added_rows = added_rows.dropna(subset=["weekday_name"])
            if not added_rows.empty:
                inferred_dow = added_rows.groupby("service_id")["weekday_name"].agg(
                    lambda names: "|".join(day for day in _WEEKDAY_ORDER if day in set(names))
                )
                inferred_runs_weekday = added_rows.groupby("service_id")["weekday_name"].agg(
                    lambda names: any(day in weekday_cols for day in names)
                ).astype("boolean")
                inferred_runs_weekend = added_rows.groupby("service_id")["weekday_name"].agg(
                    lambda names: any(day in weekend_cols for day in names)
                ).astype("boolean")

    # ``days_active``: recuento declarado en calendar.txt, mas las fechas
    # anadidas y menos las eliminadas en calendar_dates.txt (si un servicio
    # es "debil" en calendar.txt, cal_days_active es 0 y el resultado queda
    # como el recuento puro de fechas de calendar_dates.txt).
    service_days = cal_days_active.add(added_counts, fill_value=0).sub(removed_counts, fill_value=0)
    service_days = service_days.clip(lower=0) if not service_days.empty else service_days

    # ``active_days_of_week`` / ``runs_weekday`` / ``runs_weekend``: para los
    # servicios "fuertes" (patron semanal real en calendar.txt) se respeta
    # ese patron. Para el resto ("debiles": ausentes de calendar.txt, o
    # presentes con todas las banderas a 0) se usa lo inferido de
    # calendar_dates.txt si hay datos, y si no, se deja lo que hubiera en
    # calendar.txt (tipicamente vacio/False).
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
        service_runs_weekday = cal_rw_r.where(is_strong, inf_rw_r.combine_first(cal_rw_r))
        service_runs_weekend = cal_rwe_r.where(is_strong, inf_rwe_r.combine_first(cal_rwe_r))


    trips["days_active"] = pd.NA if service_days.empty else trips["service_id"].map(service_days)
    trips["active_days_of_week"] = pd.NA if service_dow.empty else trips["service_id"].map(service_dow)
    trips["runs_weekday"] = (
        pd.NA if service_runs_weekday.empty else trips["service_id"].map(service_runs_weekday)
    )
    trips["runs_weekend"] = (
        pd.NA if service_runs_weekend.empty else trips["service_id"].map(service_runs_weekend)
    )
    trips["day_type"] = trips.apply(
        lambda row: _classify_day_type(row.get("runs_weekday"), row.get("runs_weekend")), axis=1
    )
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
        "stop_id": "from_node_id", "stop_name": "from_stop_name",
        "stop_lat": "from_stop_lat", "stop_lon": "from_stop_lon",
    })
    to_lookup = stop_lookup.rename(columns={
        "stop_id": "to_node_id", "stop_name": "to_stop_name",
        "stop_lat": "to_stop_lat", "stop_lon": "to_stop_lon",
    })

    edges = work.rename(columns={"stop_id": "from_node_id", "next_stop_id": "to_node_id"}).copy()

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
        [
            "trip_id", "route_id", "service_id", "shape_id", "trip_headsign",
            "days_active", "active_days_of_week", "runs_weekday", "runs_weekend",
            "day_type", "frequency_trip_count",
        ],
    )
    route_cols = _existing_columns(routes, ["route_id", "route_short_name", "route_long_name", "route_type"])
    edges = edges.merge(trips_work[trip_cols].drop_duplicates(subset=["trip_id"]), on="trip_id", how="left")
    if "route_id" in route_cols:
        edges = edges.merge(routes[route_cols].drop_duplicates(), on="route_id", how="left")
    edges = edges.merge(from_lookup, on="from_node_id", how="left")
    edges = edges.merge(to_lookup, on="to_node_id", how="left")
    edges = edges.dropna(subset=["from_stop_lon", "from_stop_lat", "to_stop_lon", "to_stop_lat"]).copy()
    edges["edge_id"] = dataset_name + "_" + edges["from_node_id"].astype(str) + "_" + edges["to_node_id"].astype(str)
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
        "from_node_id", "to_node_id", "departure_time", "arrival_time",
        "travel_time_seconds", "hour_band", "period", "service_id", "days_active",
        "active_days_of_week", "runs_weekday", "runs_weekend", "day_type",
    ])
    return records[columns].reset_index(drop=True)


def _period_pivot_columns(
    rows: "pd.DataFrame",
    group_cols: list[str],
    peak_periods: dict[str, tuple[int, int]] | None,
    *,
    suffix: str = "",
) -> "pd.DataFrame":
    """Agrega ``travel_time_seconds_mean`` / ``trip_count`` por periodo
    (punta_manana / punta_tarde / resto_del_dia), pivotando ``period`` a
    columnas tipo ``travel_time_seconds_mean_peak_am``. ``suffix`` se anade al
    final de cada nombre de columna (p.ej. ``"_weekday"``) para poder
    combinar este desglose por periodo con otro desglose (laborables/fin de
    semana). Devuelve siempre todas las columnas de periodo, aunque esten
    vacias, para que el merge posterior sea consistente."""
    import pandas as pd

    all_period_names = list((peak_periods or DEFAULT_PEAK_PERIODS).keys()) + ["rest_of_day"]
    period_rows = rows.dropna(subset=["period"])
    if not period_rows.empty:
        period_grouped = period_rows.groupby(group_cols + ["period"], dropna=False).agg(
            travel_time_seconds_mean=("travel_time_seconds", "mean"),
            trip_count=("trip_id", "nunique"),
        ).reset_index()
        pivoted = period_grouped.pivot_table(
            index=group_cols, columns="period",
            values=["travel_time_seconds_mean", "trip_count"],
        )
        pivoted.columns = [f"{metric}_{period}{suffix}" for metric, period in pivoted.columns]
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
    - Laborables vs. fin de semana: columnas ``travel_time_seconds_mean_weekday``
      / ``trip_count_weekday`` y ``travel_time_seconds_mean_weekend`` /
      ``trip_count_weekend``, calculadas a partir de ``calendar.txt`` (o
      inferidas de ``calendar_dates.txt`` si no hay ``calendar.txt``). Un
      servicio que circula todos los dias aporta a ambas columnas. Ademas,
      ``days_of_week_summary`` empaqueta en texto la union de dias de la
      semana con servicio en esa arista, p.ej. ``"monday|tuesday|friday"``.
    - Combinacion periodo x laborables/fin de semana: las mismas columnas de
      la opcion A pero separadas ademas por dia, p.ej.
      ``travel_time_seconds_mean_peak_am_weekday`` /
      ``trip_count_peak_am_weekend`` / ``travel_time_seconds_mean_rest_of_day_weekday``.
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
            "from_node_id", "to_node_id", "route_id", "route_short_name", "route_long_name",
            "route_type", "from_stop_name", "to_stop_name",
            "from_stop_lon", "from_stop_lat", "to_stop_lon", "to_stop_lat",
        ],
    )

    # --- Agregados globales (todas las horas juntas) ---
    aggregations = {
        "trip_count": ("trip_id", "nunique"),
        "travel_time_seconds_mean": ("travel_time_seconds", "mean"),
        "travel_time_seconds_min": ("travel_time_seconds", "min"),
        "travel_time_seconds_max": ("travel_time_seconds", "max"),
    }
    grouped = edges.groupby(group_cols, dropna=False).agg(**aggregations).reset_index()

    # --- Opcion A: columnas por periodo (punta_manana / punta_tarde / resto_del_dia) ---
    period_pivot = _period_pivot_columns(edges, group_cols, peak_periods)
    grouped = grouped.merge(period_pivot, on=group_cols, how="left")

    # --- Laborables vs. fin de semana: mismos agregados globales, separados
    # por ``runs_weekday`` / ``runs_weekend``, mas la combinacion con el
    # periodo (p.ej. ``travel_time_seconds_mean_peak_am_weekday``). Un viaje
    # que circula ambos tipos de dia (p.ej. servicio diario) cuenta en las
    # dos columnas; uno que solo circula entre semana o solo en fin de semana
    # cuenta solo en la suya. ---
    all_period_names = list((peak_periods or DEFAULT_PEAK_PERIODS).keys()) + ["rest_of_day"]
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
        day_grouped = day_rows.groupby(group_cols, dropna=False).agg(
            travel_time_seconds_mean=("travel_time_seconds", "mean"),
            trip_count=("trip_id", "nunique"),
        ).reset_index().rename(columns={
            "travel_time_seconds_mean": f"travel_time_seconds_mean_{day_type_name}",
            "trip_count": f"trip_count_{day_type_name}",
        })
        grouped = grouped.merge(day_grouped, on=group_cols, how="left")

        day_period_pivot = _period_pivot_columns(
            day_rows, group_cols, peak_periods, suffix=f"_{day_type_name}"
        )
        grouped = grouped.merge(day_period_pivot, on=group_cols, how="left")

    # --- Resumen de dias de la semana con servicio, como texto empaquetado
    # (union de los dias de todos los viajes que usan esa arista, p.ej.
    # "monday|tuesday|wednesday|thursday|friday"). No genera filas nuevas. ---
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
                }),
                include_groups=False,
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
    gdf["edge_id"] = dataset_name + "_" + gdf["from_node_id"].astype(str) + "_" + gdf["to_node_id"].astype(str)
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
) -> tuple["gpd.GeoDataFrame", "gpd.GeoDataFrame", "pd.DataFrame | None"]:
    """Normaliza un feed GTFS en (paradas, aristas, horario).

    El tercer elemento devuelto, ``schedule`` (opcion B), es una tabla plana
    (``pandas.DataFrame``, sin geometria) con el detalle viaje-a-viaje; es
    ``None`` si ``include_schedule_table=False``.

    ``peak_periods`` (opcion A) por defecto separa punta_manana (07-09),
    punta_tarde (17-20) y resto_del_dia (todo lo demas); se puede pasar un
    dict propio con el mismo formato para cambiar los tramos.

    Tanto la capa de aristas como la tabla de horario incluyen ademas una
    separacion laborables/fin de semana (columnas ``*_weekday`` / ``*_weekend``
    en aristas, columnas ``active_days_of_week`` / ``runs_weekday`` /
    ``runs_weekend`` / ``day_type`` en el horario), derivada de ``calendar.txt``
    (con fallback a ``calendar_dates.txt`` si no existe).
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
