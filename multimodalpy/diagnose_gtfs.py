"""Diagnostico rapido para depurar por que las columnas semanales / de hora
punta salen todo None. Ejecutar con: python diagnose_gtfs.py /ruta/al/feed
"""
import sys
from process_gtfs import (
    read_gtfs_table, _read_optional_gtfs_table, gtfs_time_to_seconds,
    classify_period, _attach_service_days, _build_stop_to_stop_trip_records,
)

feed = sys.argv[1] if len(sys.argv) > 1 else "testfeed"

print(f"=== Inspeccionando feed: {feed} ===\n")

# --- 1. calendar.txt / calendar_dates.txt ---
calendar = _read_optional_gtfs_table(feed, "calendar.txt")
calendar_dates = _read_optional_gtfs_table(feed, "calendar_dates.txt")
print("calendar.txt encontrado:", calendar is not None)
if calendar is not None:
    print("  columnas:", list(calendar.columns))
    print("  primeras filas:")
    print(calendar.head(3).to_string())
print()
print("calendar_dates.txt encontrado:", calendar_dates is not None)
if calendar_dates is not None:
    print("  columnas:", list(calendar_dates.columns))
    print("  primeras filas:")
    print(calendar_dates.head(3).to_string())
print()

# --- 2. stop_times.txt: formato de las horas ---
stop_times = read_gtfs_table(feed, "stop_times.txt")
print("stop_times.txt columnas:", list(stop_times.columns))
if "departure_time" in stop_times.columns:
    sample = stop_times["departure_time"].dropna().head(5).tolist()
    print("  muestra departure_time (crudo):", sample)
    parsed = stop_times["departure_time"].head(200).apply(gtfs_time_to_seconds)
    n_nan = parsed.isna().sum()
    print(f"  de {len(parsed)} valores muestreados, {n_nan} no se pudieron convertir a segundos")
print()

# --- 3. trips.txt: service_id presente? ---
trips = read_gtfs_table(feed, "trips.txt")
print("trips.txt columnas:", list(trips.columns))
print("  'service_id' presente:", "service_id" in trips.columns)
print()

# --- 4. _attach_service_days: que sale ---
trips_work = _attach_service_days(trips, calendar, calendar_dates)
print("Tras _attach_service_days:")
print("  runs_weekday value_counts (incl. NA):")
print(trips_work["runs_weekday"].value_counts(dropna=False))
print("  runs_weekend value_counts (incl. NA):")
print(trips_work["runs_weekend"].value_counts(dropna=False))
print("  active_days_of_week muestra:", trips_work["active_days_of_week"].dropna().head(5).tolist())
print()

# --- 5. Registros de tramo a tramo: period / hour_band ---
stops = read_gtfs_table(feed, "stops.txt")
routes = read_gtfs_table(feed, "routes.txt")
records = _build_stop_to_stop_trip_records(
    stop_times, stops, trips, routes, dataset_name="diag",
    calendar=calendar, calendar_dates=calendar_dates,
)
print("Tras _build_stop_to_stop_trip_records (edges sin agregar):")
print("  filas totales:", len(records))
print("  'period' value_counts (incl. NA):")
print(records["period"].value_counts(dropna=False))
print("  'hour_band' value_counts (incl. NA), primeras 10:")
print(records["hour_band"].value_counts(dropna=False).head(10))
print("  'runs_weekday' value_counts (incl. NA):")
print(records["runs_weekday"].value_counts(dropna=False))