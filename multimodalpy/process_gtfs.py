"""Estandarizacion de datos GTFS. Limpieza de datos de bus + tren.

Este modulo tiene dos bloques:

1. Estandarizacion de un feed GTFS (ZIP o carpeta) en capas GeoPandas:
   - ``nodes_stops``              : paradas como puntos.
   - ``edges_stop_to_stop``       : aristas parada-a-parada (secuencia de viajes).
   - ``edges_shapes_reference``   : geometria de recorrido (shapes.txt).

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


def build_gtfs_stop_to_stop_edges_gdf(
    stop_times: "pd.DataFrame",
    stops: "pd.DataFrame",
    trips: "pd.DataFrame",
    routes: "pd.DataFrame",
    *,
    dataset_name: str,
    layer_id: str,
    filter_geometry=None,
) -> "gpd.GeoDataFrame":
    """Capa de aristas parada-a-parada a partir de ``stop_times.txt``."""
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString

    work = stop_times.copy()
    work["stop_sequence"] = pd.to_numeric(work["stop_sequence"], errors="coerce")
    work = work.dropna(subset=["trip_id", "stop_id", "stop_sequence"]).sort_values(
        ["trip_id", "stop_sequence"]
    )
    work["next_stop_id"] = work.groupby("trip_id")["stop_id"].shift(-1)
    work = work.dropna(subset=["next_stop_id"]).copy()

    stop_lookup = stops[["stop_id", "stop_name", "stop_lat", "stop_lon"]].copy()
    stop_lookup["stop_lat"] = pd.to_numeric(stop_lookup["stop_lat"], errors="coerce")
    stop_lookup["stop_lon"] = pd.to_numeric(stop_lookup["stop_lon"], errors="coerce")
    stop_lookup = stop_lookup.dropna(subset=["stop_lat", "stop_lon"]).drop_duplicates("stop_id")

    from_lookup = stop_lookup.rename(
        columns={
            "stop_id": "from_stop_id",
            "stop_name": "from_stop_name",
            "stop_lat": "from_stop_lat",
            "stop_lon": "from_stop_lon",
        }
    )
    to_lookup = stop_lookup.rename(
        columns={
            "stop_id": "to_stop_id",
            "stop_name": "to_stop_name",
            "stop_lat": "to_stop_lat",
            "stop_lon": "to_stop_lon",
        }
    )

    edges = work.rename(columns={"stop_id": "from_stop_id", "next_stop_id": "to_stop_id"}).copy()
    trip_cols = _existing_columns(trips, ["trip_id", "route_id", "service_id", "shape_id", "trip_headsign"])
    route_cols = _existing_columns(routes, ["route_id", "route_short_name", "route_long_name", "route_type"])
    edges = edges.merge(trips[trip_cols].drop_duplicates(), on="trip_id", how="left")
    if "route_id" in route_cols:
        edges = edges.merge(routes[route_cols].drop_duplicates(), on="route_id", how="left")
    edges = edges.merge(from_lookup, on="from_stop_id", how="left")
    edges = edges.merge(to_lookup, on="to_stop_id", how="left")
    edges = edges.dropna(subset=["from_stop_lon", "from_stop_lat", "to_stop_lon", "to_stop_lat"]).copy()

    group_cols = _existing_columns(
        edges,
        [
            "from_stop_id",
            "to_stop_id",
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
    aggregations = {"trip_count": ("trip_id", "nunique")}
    if "service_id" in edges.columns:
        aggregations["service_count"] = ("service_id", "nunique")

    grouped = edges.groupby(group_cols, dropna=False).agg(**aggregations).reset_index()
    if "service_count" not in grouped.columns:
        grouped["service_count"] = None

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


def build_gtfs_shapes_gdf(
    shapes: "pd.DataFrame | None",
    trips: "pd.DataFrame",
    routes: "pd.DataFrame",
    *,
    dataset_name: str,
    layer_id: str,
    filter_geometry=None,
) -> "gpd.GeoDataFrame":
    """Capa de recorridos (shapes) a partir de ``shapes.txt``."""
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString

    if shapes is None or shapes.empty:
        return gpd.GeoDataFrame(columns=["shape_id", "geometry"], geometry="geometry", crs=SOURCE_CRS)

    work = shapes.copy()
    work["shape_pt_lat"] = pd.to_numeric(work["shape_pt_lat"], errors="coerce")
    work["shape_pt_lon"] = pd.to_numeric(work["shape_pt_lon"], errors="coerce")
    work["shape_pt_sequence"] = pd.to_numeric(work["shape_pt_sequence"], errors="coerce")
    work = work.dropna(subset=["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"])
    work = work.sort_values(["shape_id", "shape_pt_sequence"])

    records = []
    for shape_id, group in work.groupby("shape_id", sort=False):
        coords = list(zip(group["shape_pt_lon"], group["shape_pt_lat"]))
        if len(coords) >= 2:
            records.append({"shape_id": shape_id, "geometry": LineString(coords)})

    if not records:
        return gpd.GeoDataFrame(columns=["shape_id", "geometry"], geometry="geometry", crs=SOURCE_CRS)

    shape_gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=SOURCE_CRS)

    if "shape_id" in trips.columns and "route_id" in trips.columns:
        route_cols = _existing_columns(routes, ["route_id", "route_short_name", "route_long_name", "route_type"])
        shape_routes = trips[["shape_id", "route_id"]].dropna().drop_duplicates()
        if "route_id" in route_cols:
            shape_routes = shape_routes.merge(routes[route_cols].drop_duplicates(), on="route_id", how="left")
        agg_cols = [column for column in shape_routes.columns if column != "shape_id"]
        shape_meta = shape_routes.groupby("shape_id")[agg_cols].agg(_unique_join).reset_index()
        shape_gdf = shape_gdf.merge(shape_meta, on="shape_id", how="left")

    shape_gdf = _filter_by_geometry(shape_gdf, filter_geometry)
    shape_gdf["dataset_name"] = dataset_name
    shape_gdf["layer_id"] = layer_id
    shape_gdf["edge_id"] = dataset_name + "_shape_" + shape_gdf["shape_id"].astype(str)
    return shape_gdf


def normalize_gtfs_feed(
    feed_path: str | Path,
    *,
    dataset_name: str,
    layer_id: str,
    filter_geometry=None,
    target_crs: str | None = SOURCE_CRS,
) -> tuple["gpd.GeoDataFrame", "gpd.GeoDataFrame", "gpd.GeoDataFrame"]:
    """Normaliza un feed GTFS en (paradas, aristas parada-a-parada, recorridos)."""
    stops = read_gtfs_table(feed_path, "stops.txt")
    stop_times = read_gtfs_table(feed_path, "stop_times.txt")
    trips = read_gtfs_table(feed_path, "trips.txt")
    routes = read_gtfs_table(feed_path, "routes.txt")
    shapes = _read_optional_gtfs_table(feed_path, "shapes.txt")

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
    )
    shape_edges_gdf = build_gtfs_shapes_gdf(
        shapes,
        trips,
        routes,
        dataset_name=dataset_name,
        layer_id=layer_id,
        filter_geometry=filter_geometry,
    )

    if target_crs:
        stops_gdf = stops_gdf.to_crs(target_crs)
        stop_edges_gdf = stop_edges_gdf.to_crs(target_crs)
        shape_edges_gdf = shape_edges_gdf.to_crs(target_crs)

    return stops_gdf, stop_edges_gdf, shape_edges_gdf


# ---------------------------------------------------------------------------
# Limpieza de bus + tren: snap de paradas y division del recorrido en tramos
# ---------------------------------------------------------------------------
def compute_snapped_stops(
    df_shapes: "gpd.GeoDataFrame",
    df_stops: "gpd.GeoDataFrame",
) -> "gpd.GeoSeries":
    """Proyecta cada parada sobre el punto mas cercano del recorrido (snap).

    Devuelve una ``GeoSeries`` de puntos proyectados, indexada por ``stop_id``.
    Ambas capas deben estar en un CRS metrico (p. ej. EPSG:25830) para que las
    distancias tengan sentido.
    """
    import geopandas as gpd
    from shapely.ops import nearest_points

    lines_union = df_shapes.geometry.union_all()
    snapped_points = df_stops.geometry.apply(lambda pt: nearest_points(pt, lines_union)[1])
    return gpd.GeoSeries(snapped_points.values, index=df_stops["stop_id"], crs=df_stops.crs)


def split_lines_with_points(
    lines_gdf: "gpd.GeoDataFrame",
    points: "gpd.GeoSeries",
    *,
    tolerance: float = 1.0,
) -> "gpd.GeoDataFrame":
    """Divide las lineas del recorrido en tramos usando las paradas proyectadas.

    Cada tramo resultante lleva ``from_stop_id`` y ``to_stop_id`` de las paradas
    que lo delimitan. Los extremos sin parada asignada toman la parada mas cercana.
    """
    import geopandas as gpd
    from shapely.ops import split, snap

    all_segments = [(geom, None, None) for geom in lines_gdf.geometry]

    for stop_id, point in points.items():
        new_segments = []
        for (line, from_stop_id, to_stop_id) in all_segments:
            if point.distance(line) > tolerance:
                new_segments.append((line, from_stop_id, to_stop_id))
                continue

            projected_dist = line.project(point)
            projected_point = line.interpolate(projected_dist)
            try:
                snapped_line = snap(line, projected_point, tolerance=1e-6)
                result = split(snapped_line, projected_point)
                parts = list(result.geoms)
                if len(parts) == 2:
                    new_segments.append((parts[0], from_stop_id, stop_id))
                    new_segments.append((parts[1], stop_id, to_stop_id))
                else:
                    new_segments.append((line, from_stop_id, to_stop_id))
            except Exception:
                new_segments.append((line, from_stop_id, to_stop_id))
        all_segments = new_segments

    fixed_segments = []
    for (line, from_stop_id, to_stop_id) in all_segments:
        if from_stop_id is None:
            start_point = line.interpolate(0)
            from_stop_id = min(points.index, key=lambda sid: start_point.distance(points[sid]))
        if to_stop_id is None:
            end_point = line.interpolate(1, normalized=True)
            to_stop_id = min(points.index, key=lambda sid: end_point.distance(points[sid]))
        fixed_segments.append((line, from_stop_id, to_stop_id))

    geometries, from_ids, to_ids = zip(*fixed_segments)
    return gpd.GeoDataFrame(
        {"from_stop_id": from_ids, "to_stop_id": to_ids},
        geometry=list(geometries),
        crs=lines_gdf.crs,
    )


def clean_gtfs_lines(
    shape_edges_gdf: "gpd.GeoDataFrame",
    stops_gdf: "gpd.GeoDataFrame",
    *,
    working_crs: str = "EPSG:25830",
    tolerance: float = 1.0,
) -> tuple["gpd.GeoSeries", "gpd.GeoDataFrame"]:
    """Limpieza de bus/tren: snap de paradas + division del recorrido en tramos.

    Recibe las capas normalizadas de recorridos (``shape_edges_gdf``) y paradas
    (``stops_gdf``) y devuelve ``(paradas_proyectadas, tramos)`` en ``working_crs``.
    El CRS de trabajo debe ser metrico para que las operaciones geometricas sean
    correctas; por defecto EPSG:25830 (UTM 30N, valido para la Espana peninsular).
    """
    if shape_edges_gdf is None or shape_edges_gdf.empty:
        raise ValueError(
            "No hay geometria de recorridos (shapes) para limpiar. "
            "El feed GTFS no incluye shapes.txt o esta vacio."
        )
    if "stop_id" not in stops_gdf.columns:
        raise ValueError("La capa de paradas debe incluir la columna 'stop_id'.")

    shapes = shape_edges_gdf.to_crs(working_crs).copy()
    shapes["geometry"] = shapes.geometry.make_valid()
    stops = stops_gdf.to_crs(working_crs).copy()

    snapped_points = compute_snapped_stops(shapes, stops)
    segments = split_lines_with_points(shapes, snapped_points, tolerance=tolerance)
    segments = segments.drop_duplicates(subset=["geometry"])
    return snapped_points, segments
