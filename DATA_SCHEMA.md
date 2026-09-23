# Esquema de datos

Diccionario completo de las capas que escribe `multimodalpy.get_network.main()`:
qué columnas tiene cada una, qué significa cada valor y cómo se calcula.

Los nombres de columna **no pasan de 10 caracteres**. Es el límite de Shapefile,
que trunca cualquier nombre más largo y resuelve las colisiones resultantes con
sufijos numéricos (`trip_count`, `trip_cou_1`, …). Para que un mismo dato se
llame igual en GeoJSON, Shapefile y GeoPackage, los nombres cortos se aplican a
los tres formatos por igual, no solo a Shapefile.

---

## Capas que se generan

| Capa | Contenido | Geometría |
|---|---|---|
| `study_area_boundary` | Límite del municipio, disuelto en un único polígono | Polygon |
| `osm_<modo>_nodes` | Nodos de la red OSM, un modo por capa | Point |
| `osm_<modo>_edges` | Tramos de la red OSM | LineString |
| `gtfs_<dataset>_nodes` | Paradas de un feed GTFS, recortadas al municipio | Point |
| `gtfs_<dataset>_edges` | Tramos parada a parada de un feed GTFS | LineString |
| `gtfs_<dataset>_schedule` | Horario viaje a viaje, sin geometría | — (CSV o tabla) |

`<modo>` es `walking`, `bike` o `driving`. `<dataset>` es el nombre del fichero
GTFS descargado del NAP, normalizado (p. ej. `20260921_040013_renfe_cerca`).

---

## `osm_<modo>_nodes`

| Columna | Tipo | Significado |
|---|---|---|
| `node_id` | texto | Identificador único del nodo dentro de la capa |
| `node_role` | texto | Papel del nodo en la topología, ver tabla siguiente |
| `geometry` | Point | Posición del nodo |

Valores de `node_role`:

| Valor | Significado |
|---|---|
| `endpoint` | Extremo de un tramo que no continúa |
| `intersection` | Cruce de tres o más tramos |
| `linear_vertex` | Vértice intermedio de la geometría, sin cruce |
| `through_endpoint` | Extremo de un tramo que además es vértice de otro |
| `isolated_vertex` | Vértice sin conexión con ningún tramo |

---

## `osm_<modo>_edges`

| Columna | Tipo | Significado |
|---|---|---|
| `edge_id` | texto | Identificador del tramo, derivado del `osmid` original de OSM |
| `from_node` | texto | `node_id` del nodo origen. **Antes `from_node_id`** |
| `to_node_id` | texto | `node_id` del nodo destino |
| `highway` | texto | **Una única** etiqueta `highway` válida de OSM |
| `hwy_raw` | texto | Etiqueta o etiquetas originales, unidas con `;` si eran varias |
| `lanes` | texto | Número de carriles según OSM |
| `maxspeed` | número | Velocidad máxima en **km/h**, ya convertida a número |
| `spd_raw` | texto | Valor original del tag `maxspeed` de OSM |
| `name` | texto | Nombre de la vía |
| `oneway` | booleano | Si el tramo es de sentido único |
| `reversed` | booleano | Si la geometría va en sentido contrario al de circulación |
| `length` | número | Longitud del tramo en metros |
| `tts` | número | Tiempo de viaje en segundos, ver más abajo |
| `geometry` | LineString | Geometría del tramo |

### Por qué existen `hwy_raw` y `spd_raw`

Cuando osmnx simplifica el grafo, fusiona varios tramos de OSM en una sola
arista y guarda **una lista** con las etiquetas de todos ellos. Como los
ficheros no admiten listas en una columna, la librería las une con `;`.

El resultado eran valores que **no existen en OSM**: `footway;steps`,
`unclassified;track`, `residential;tertiary`. En la red peatonal de Gijón
aparecían 87 valores distintos de `highway`, de los cuales 69 eran compuestos y
afectaban a 28.578 de 266.286 tramos.

Ahora `highway` contiene siempre una etiqueta real (la primera de la lista) y
`hwy_raw` conserva el valor completo, de modo que no se pierde información.
Lo mismo con `maxspeed`, que ahora es un número en km/h, y `spd_raw`, que
guarda el texto original (`"30 mph"`, `"30;50"`, `"walk"`, …).

> **Nota para quien toque el código:** no confundir esto con
> `_normalize_highway()`. Esa función agrupa las vías por velocidad y colapsa
> cualquier vía transitable a pie a `footway`, lo cual sirve para elegir una
> velocidad libre pero **no** para etiquetar: convertiría `track`, `path`,
> `living_street` y `bridleway` en `footway`. La normalización de la columna
> exportada la hace `_primary_highway()`, que solo resuelve los valores
> compuestos y nunca toca uno simple.

### Cómo se calcula `tts`

Tiempo de viaje en segundos = longitud ÷ velocidad libre + penalización de
parada en el nodo de llegada. La velocidad libre depende del modo:

| Modo | Velocidad | Usa el tag `maxspeed` | Tope |
|---|---|---|---|
| `walking` | 5 km/h constante | No | 5 km/h |
| `bike` | 15 km/h por defecto | No | 25 km/h |
| `driving` | `maxspeed` si es válido; si no, según tipo de vía | Sí | sin tope |

Velocidades por defecto para `driving` cuando no hay `maxspeed` válido (km/h):

| Tipo de vía | km/h | Tipo de vía | km/h |
|---|---|---|---|
| `motorway` | 100 | `tertiary` | 40 |
| `motorway_link` | 70 | `tertiary_link` | 30 |
| `trunk` | 80 | `residential` | 30 |
| `trunk_link` | 50 | `living_street` | 15 |
| `primary` | 60 | `unclassified` | 30 |
| `primary_link` | 40 | `service` | 20 |
| `secondary` | 50 | cualquier otro | 30 |
| `secondary_link` | 40 | | |

---

## `gtfs_<dataset>_nodes`

| Columna | Tipo | Significado |
|---|---|---|
| `node_id` | texto | `stop_id` de la parada en el feed GTFS |
| `stop_name` | texto | Nombre de la parada |
| `geometry` | Point | Posición de la parada |

Solo se conservan las paradas que caen dentro del límite del municipio.

---

## `gtfs_<dataset>_edges`

Un tramo por cada par de paradas consecutivas de una línea.

### Identificación

| Columna | Antes | Significado |
|---|---|---|
| `edge_id` | — | Identificador del tramo |
| `from_node` | `from_node_id` | Parada de origen |
| `to_node_id` | — | Parada de destino |
| `route_id` | — | Identificador de la línea |
| `route_sh` | `route_short_name` | Nombre corto de la línea (p. ej. `"L1"`) |
| `route_ln` | `route_long_name` | Nombre largo de la línea |
| `geometry` | — | Geometría del tramo |

### Agregados del día completo

| Columna | Antes | Significado |
|---|---|---|
| `trips` | `trip_count` | Número de viajes que recorren el tramo |
| `tts_m` | `travel_time_seconds_mean` | Tiempo de viaje medio, en segundos |
| `tts_md` | `travel_time_seconds_median` | Mediana |
| `tts_mn` | `travel_time_seconds_min` | Mínimo |
| `tts_mx` | `travel_time_seconds_max` | Máximo |

### Desglose por periodo del día

| Columna | Antes | Significado |
|---|---|---|
| `tts_m_pam` | `travel_time_seconds_mean_peak_am` | Media en punta de mañana |
| `tts_m_ppm` | `travel_time_seconds_mean_peak_pm` | Media en punta de tarde |
| `tts_m_rod` | `travel_time_seconds_mean_rest_of_day` | Media en el resto del día |
| `tts_md_pam` | `travel_time_seconds_median_peak_am` | Mediana en punta de mañana |
| `tts_md_ppm` | `travel_time_seconds_median_peak_pm` | Mediana en punta de tarde |
| `tts_md_rod` | `travel_time_seconds_median_rest_of_day` | Mediana en el resto del día |
| `trips_pam` | `trip_count_peak_am` | Viajes en punta de mañana |
| `trips_ppm` | `trip_count_peak_pm` | Viajes en punta de tarde |
| `trips_rod` | `trip_count_rest_of_day` | Viajes en el resto del día |

Periodos por defecto: punta de mañana de 07:00 a 09:00, punta de tarde de
17:00 a 20:00, resto del día todo lo demás. Se pueden cambiar con el parámetro
`gtfs_peak_periods` de `main()`.

### Desglose por tipo de día

| Columna | Antes | Significado |
|---|---|---|
| `tts_m_wd` | `travel_time_seconds_mean_weekday` | Media en día laborable |
| `tts_m_we` | `travel_time_seconds_mean_weekend` | Media en fin de semana |
| `trips_wd` | `trip_count_weekday` | Viajes en día laborable |
| `trips_we` | `trip_count_weekend` | Viajes en fin de semana |

### Periodo × tipo de día

Estas son las más abreviadas, porque juntar las cuatro partes no cabe en 10
caracteres de ninguna otra forma. `tm_` es tiempo medio y `trp_` número de
viajes.

| Columna | Antes | Significado |
|---|---|---|
| `tm_pam_wd` | `travel_time_seconds_mean_peak_am_weekday` | Media, punta mañana, laborable |
| `tm_pam_we` | `travel_time_seconds_mean_peak_am_weekend` | Media, punta mañana, fin de semana |
| `tm_ppm_wd` | `travel_time_seconds_mean_peak_pm_weekday` | Media, punta tarde, laborable |
| `tm_ppm_we` | `travel_time_seconds_mean_peak_pm_weekend` | Media, punta tarde, fin de semana |
| `tm_rod_wd` | `travel_time_seconds_mean_rest_of_day_weekday` | Media, resto del día, laborable |
| `tm_rod_we` | `travel_time_seconds_mean_rest_of_day_weekend` | Media, resto del día, fin de semana |
| `trp_pam_wd` | `trip_count_peak_am_weekday` | Viajes, punta mañana, laborable |
| `trp_pam_we` | `trip_count_peak_am_weekend` | Viajes, punta mañana, fin de semana |
| `trp_ppm_wd` | `trip_count_peak_pm_weekday` | Viajes, punta tarde, laborable |
| `trp_ppm_we` | `trip_count_peak_pm_weekend` | Viajes, punta tarde, fin de semana |
| `trp_rod_wd` | `trip_count_rest_of_day_weekday` | Viajes, resto del día, laborable |
| `trp_rod_we` | `trip_count_rest_of_day_weekend` | Viajes, resto del día, fin de semana |

### Resúmenes empaquetados

| Columna | Antes | Significado |
|---|---|---|
| `days_week` | `days_of_week_summary` | Días en que circula, unidos con `\|` (p. ej. `monday\|tuesday`) |
| `h_tts` | `hourly_travel_times` | Tiempo medio por franja horaria, `"08-09:420\|09-10:395"` |
| `h_trips` | `hourly_trip_counts` | Viajes por franja horaria, mismo formato |

---

## Estructura de carpetas de salida

```
output_path/
  study_area_boundary.geojson
  driving/   nodes.geojson  edges.geojson
  walking/   nodes.geojson  edges.geojson
  bike/      nodes.geojson  edges.geojson
  bus/<dataset>/    nodes.geojson  edges.geojson  schedule.csv
  train/<dataset>/  nodes.geojson  edges.geojson  schedule.csv
  gtfs_zips/        (solo si gtfs_zips=True)
  <area>.gpkg       (solo si se pide geopackage)
```

Con Shapefile cada capa son cinco ficheros (`.shp`, `.shx`, `.dbf`, `.prj`,
`.cpg`) en su misma carpeta. GeoPackage es un unico fichero en la raiz con
todas las capas dentro, asi que no se reparte en carpetas.

Que un dataset GTFS vaya a `bus/` o a `train/` se decide leyendo los
`route_type` de su `routes.txt`, no los metadatos del NAP: alli un mismo
conjunto puede declararse a la vez como bus y como ferroviario, como pasa con
Cercanias Renfe.

## Parametros de `main()` que afectan a la salida

| Parametro | Por defecto | Efecto |
|---|---|---|
| `output_file_type` | `"geojson"` | Formato o lista de formatos. Con una lista, la descarga y la normalizacion se hacen una sola vez y solo se repite la escritura |
| `schedule` | `False` | Escribe la tabla de horario viaje a viaje de cada feed. Desactivado por defecto porque es lo que mas ocupa con diferencia: en Gijon, mas de 1 GB de los 1,4 GB de la descarga |
| `gtfs_zips` | `False` | Conserva los ZIP descargados del NAP. Desactivado por defecto: son solo la materia prima y se pueden volver a descargar |
| `multimodal` | `False` | Reservado para la red multimodal, aun sin implementar. Con `True` lanza `NotImplementedError` en vez de devolver una red incompleta en silencio |
| `crs` | `"EPSG:4326"` | CRS de salida de todas las capas |

---

## Diccionario de abreviaturas

| Abreviatura | Significa |
|---|---|
| `tts` | travel time seconds, tiempo de viaje en segundos |
| `tm` | tiempo de viaje medio (en las columnas de 4 partes) |
| `trp`, `trips` | número de viajes |
| `m` | mean, media |
| `md` | median, mediana |
| `mn` | min, mínimo |
| `mx` | max, máximo |
| `pam` | peak_am, punta de mañana |
| `ppm` | peak_pm, punta de tarde |
| `rod` | rest_of_day, resto del día |
| `wd` | weekday, día laborable |
| `we` | weekend, fin de semana |
| `h_` | hourly, desglosado por franja horaria |
| `raw` | valor original de OSM, sin normalizar |
| `sh`, `ln` | short name, long name |

---

## Cómo se resuelve el área y el operador de transporte

**Municipio → polígono.** Se busca el nombre en el fichero de límites, con
tolerancia a acentos y erratas. Se disuelven todas las filas que coinciden en
un único polígono.

**Municipio → datos GTFS.** El endpoint del NAP
`/conjunto-dato/region/{id}` solo acepta **códigos INE de provincia, del 1 al
52**. El listado `/region` devuelve además unas 8.300 entradas de municipio y de
comunidad autónoma que comparten ese mismo espacio de identificadores: las de
municipio dan 404, y las de comunidad autónoma caen dentro del rango provincial
y devuelven **los datos de otra provincia sin avisar**.

Por eso la provincia **no se busca por nombre**: se deduce del `NATCODE` del
límite administrativo, que tiene la forma
`<2 país><2 ccaa><2 provincia><5 municipio>`, y se usa directamente como
identificador de región.
