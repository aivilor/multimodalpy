# Data schema

Reference for everything `multimodalpy.get_network.main()` writes to disk: where
each layer goes, what every column means and how each value is computed.

## Output layout

```
output_path/
├── study_area_boundary.geojson
├── driving/                 nodes  edges
├── walking/                 nodes  edges
├── bike/                    nodes  edges
├── bus/<dataset>/           nodes  edges  schedule.csv
├── train/<dataset>/         nodes  edges  schedule.csv
├── gtfs_zips/               only with gtfs_zips=True
└── <area>.gpkg              only with output_file_type="geopackage"
```

GeoJSON and Shapefile write one file per layer inside its mode folder (a
Shapefile is five files: `.shp`, `.shx`, `.dbf`, `.prj`, `.cpg`). GeoPackage is a
single file at the root holding every layer, so it is not split into folders.
`schedule.csv` is only written with `schedule=True`. NetworkX writes one `graph.json`
 per mode folder instead of `nodes` and `edges`
(see [NetworkX graphs](#networkx-graphs-graphjson)).



The same layer has a file path in GeoJSON and Shapefile and a layer name inside
the GeoPackage:

| Content | File (GeoJSON / Shapefile) | GeoPackage layer |
|---|---|---|
| Study area | `study_area_boundary` | `study_area_boundary` |
| OSM nodes | `<mode>/nodes` | `osm_<mode>_nodes` |
| OSM edges | `<mode>/edges` | `osm_<mode>_edges` |
| GTFS stops | `<bus\|train>/<dataset>/nodes` | `gtfs_<dataset>_nodes` |
| GTFS edges | `<bus\|train>/<dataset>/edges` | `gtfs_<dataset>_edges` |
| GTFS schedule | `<bus\|train>/<dataset>/schedule.csv` | `gtfs_<dataset>_schedule` (attribute table) |

`<mode>` is `driving`, `walking` or `bike`. `<dataset>` is the name of the GTFS
file downloaded from the NAP, normalised (e.g. `20260921_040013_renfe_cerca`).

A GTFS dataset goes under `train/` when most of its routes have a rail
`route_type` in `routes.txt` (tram 0, subway 1, rail 2, cable tram 5,
funicular 7, monorail 12) and under `bus/` otherwise. The feed itself is read
rather than the NAP metadata, because the NAP declares some datasets as bus and
rail at once (Cercanías Renfe). An unreadable feed is treated as bus.

## `main()` parameters that shape the output

| Parameter | Default | Effect |
|---|---|---|
| `output_file_type` | `"geojson"` | A format or a list of formats. With a list, download and normalisation run once and only the writing step repeats. |
| `crs` | `"EPSG:4326"` | CRS of every output layer. |
| `connect_parts` | `True` | Add the roads joining the parts of a multi-part municipality (see "Municipalities in several parts"). |
| `schedule` | `False` | Also write the trip-by-trip timetable of each GTFS feed. Off by default because it is by far the largest part of a download. |
| `gtfs_zips` | `False` | Keep the zips downloaded from the NAP. Off by default: they are only the raw input and can be downloaded again. |
| `multimodal` | `False` | Reserved for the multimodal network, not implemented yet. `True` raises `NotImplementedError`. |

`main()` prints nothing. It returns a manifest with the files written and, when a
public transport mode was requested but produced no data, a `gtfs_status` field
saying why. When parts of a multi-part municipality could not be connected, an
`unconnected_parts` field lists them per mode, e.g. `{"driving": [3]}`. Diagnostic messages go through `logging` and are silent unless the
application enables them, e.g. `logging.basicConfig(level=logging.INFO)`.

## Column names

No column name is longer than **10 characters**. That is the Shapefile limit:
longer names get truncated and the resulting collisions are resolved with
numeric suffixes (`trip_count`, `trip_cou_1`, …), so the same column used to end
up with a different name depending on the format. The short names are applied to
all three formats alike, so a column is called the same everywhere.

The abbreviations are listed at the end of this document.

---

## OSM nodes

| Column | Type | Meaning |
|---|---|---|
| `node_id` | text | Node identifier, unique within the layer |
| `node_role` | text | Topological role of the node, see below |
| `geometry` | Point | Node position |

`node_role` is derived from the number of edge segments meeting at the node:

| Value | Segments | Meaning |
|---|---|---|
| `isolated_vertex` | 0 | Not connected to any edge |
| `endpoint` | 1 | Dead end |
| `linear_vertex` | 2 | Interior vertex of a single edge geometry |
| `through_endpoint` | 2 | Two edges meeting end to end, with no branching |
| `intersection` | 3 or more | Junction |

## OSM edges

| Column | Type | Meaning |
|---|---|---|
| `edge_id` | text | OSM way id (`osmid`), see note below |
| `from_node` | text | `node_id` of the start node |
| `to_node` | text | `node_id` of the end node |
| `highway` | text | A single valid OSM `highway` tag |
| `hwy_raw` | text | Original tag(s), joined with `;` when there were several |
| `lanes` | text | Number of lanes as tagged in OSM, possibly `;`-joined |
| `maxspeed` | number | Speed limit in **km/h** |
| `spd_raw` | text | Original `maxspeed` tag |
| `name` | text | Street name |
| `oneway` | boolean | One-way segment |
| `reversed` | boolean | Geometry drawn against the direction of travel |
| `length` | number | Length in metres |
| `tts` | number | Travel time in seconds, see below |
| `connector` | boolean | Road outside the municipality, added to join its parts (see "Municipalities in several parts"); `false` everywhere else |
| `geometry` | LineString | Edge geometry |

These 15 columns are always present, in this order, in every output format
(GeoPackage, GeoJSON, Shapefile and the NetworkX `graph.json`, where they are
the edge attributes and `geometry` is WKT). A column the network has no tag for
(e.g. `lanes` on a walking network) is written empty rather than dropped.

**One row per OSM way.** When osmnx simplifies the graph it can merge several OSM
ways into a single edge. That edge is written once per original way, each row
with its own `edge_id` and the same geometry and nodes.

### `highway` and `maxspeed`

A merged edge carries the tags of every way it came from, which used to be
flattened into values that do not exist in OSM: `footway;steps`,
`unclassified;track`, `residential;tertiary`. On the walking network of Gijón
there were 87 distinct `highway` values, 69 of them compound, affecting 28,578 of
266,286 edges. `maxspeed` had the same problem with text such as `"30 mph"`,
`"30;50"` or `"walk"`.

- `highway` keeps the **first** tag of a compound value; a single tag is left
  untouched. The full original value stays in `hwy_raw`.
- `maxspeed` is parsed to km/h: `mph` values are converted, and when several
  values are given the **highest** one is kept (`"30;50"` → 50). Values with no
  number (`"walk"`, `"signals"`, `"none"`, `"variable"`) become empty. The
  original text stays in `spd_raw`.

> Note for maintainers: the exported `highway` is produced by
> `_primary_highway()`, **not** by `_normalize_highway()`. The latter groups ways
> by free-flow speed and collapses anything walkable (`track`, `path`,
> `living_street`, `bridleway`, …) into `footway`. That is right for picking a
> speed and wrong for labelling.

### Travel time (`tts`)

`tts = length / free-flow speed + stop penalty at the arrival node`.

| Mode | Free-flow speed | Stop penalty |
|---|---|---|
| `walking` | 5 km/h, constant | none |
| `bike` | 15 km/h, constant | yes, see below |
| `driving` | `maxspeed` if valid, otherwise by road type, otherwise 30 km/h | yes, see below |

For `bike` the profile also defines a 25 km/h cap, but since cycling speed does
not read `maxspeed` it is always 15 km/h and the cap never applies.

Default `driving` speeds when there is no valid `maxspeed` (km/h):

| Road type | km/h | Road type | km/h |
|---|---|---|---|
| `motorway` | 100 | `tertiary` | 40 |
| `motorway_link` | 70 | `tertiary_link` | 30 |
| `trunk` | 80 | `residential` | 30 |
| `trunk_link` | 50 | `living_street` | 15 |
| `primary` | 60 | `unclassified` | 30 |
| `primary_link` | 40 | `service` | 20 |
| `secondary` | 50 | anything else | 30 |
| `secondary_link` | 40 | | |

The stop penalty depends on the `highway` tag of the node the edge arrives at. If
the node has no known tag but is an `intersection`, the generic penalty applies.

| Arrival node | `driving` (s) | `bike` (s) |
|---|---|---|
| `traffic_signals` | 15 | 8 |
| `stop` | 4 | 3 |
| `mini_roundabout` | 3 | 2 |
| `crossing` | 3 | 2 |
| `give_way` | 2 | 1 |
| any other `intersection` | 6 | 3 |

---

## GTFS stops

| Column | Type | Meaning |
|---|---|---|
| `node_id` | text | `stop_id` in the GTFS feed |
| `stop_name` | text | Stop name |
| `geometry` | Point | Stop position |

Only stops inside the study area are kept.

## GTFS edges

One edge per pair of consecutive stops of a route, with travel time statistics
aggregated over every trip that runs it. Columns are listed with their former,
long name.

### Identification

| Column | Former name | Meaning |
|---|---|---|
| `edge_id` | — | Edge identifier |
| `from_node` | — | Origin stop |
| `to_node` | — | Destination stop |
| `route_id` | — | Route identifier |
| `route_sh` | `route_short_name` | Short route name (e.g. `"L1"`) |
| `route_ln` | `route_long_name` | Long route name |
| `geometry` | — | Edge geometry |

### Whole day

| Column | Former name | Meaning |
|---|---|---|
| `trips` | `trip_count` | Number of trips running the edge |
| `tts_m` | `travel_time_seconds_mean` | Mean travel time, seconds |
| `tts_md` | `travel_time_seconds_median` | Median |
| `tts_mn` | `travel_time_seconds_min` | Minimum |
| `tts_mx` | `travel_time_seconds_max` | Maximum |

### By period of the day

Default periods: morning peak 07:00–09:00, evening peak 17:00–20:00, rest of the
day everything else. They can be changed with `gtfs_peak_periods`.

| Column | Former name | Meaning |
|---|---|---|
| `tts_m_pam` | `travel_time_seconds_mean_peak_am` | Mean, morning peak |
| `tts_m_ppm` | `travel_time_seconds_mean_peak_pm` | Mean, evening peak |
| `tts_m_rod` | `travel_time_seconds_mean_rest_of_day` | Mean, rest of day |
| `tts_md_pam` | `travel_time_seconds_median_peak_am` | Median, morning peak |
| `tts_md_ppm` | `travel_time_seconds_median_peak_pm` | Median, evening peak |
| `tts_md_rod` | `travel_time_seconds_median_rest_of_day` | Median, rest of day |
| `trips_pam` | `trip_count_peak_am` | Trips, morning peak |
| `trips_ppm` | `trip_count_peak_pm` | Trips, evening peak |
| `trips_rod` | `trip_count_rest_of_day` | Trips, rest of day |

### By type of day

| Column | Former name | Meaning |
|---|---|---|
| `tts_m_wd` | `travel_time_seconds_mean_weekday` | Mean, weekday |
| `tts_m_we` | `travel_time_seconds_mean_weekend` | Mean, weekend |
| `trips_wd` | `trip_count_weekday` | Trips, weekday |
| `trips_we` | `trip_count_weekend` | Trips, weekend |

### Period × type of day

The most abbreviated ones, because the four parts do not fit in ten characters
any other way. `tm_` is mean travel time and `trp_` is number of trips.

| Column | Former name | Meaning |
|---|---|---|
| `tm_pam_wd` | `travel_time_seconds_mean_peak_am_weekday` | Mean, morning peak, weekday |
| `tm_pam_we` | `travel_time_seconds_mean_peak_am_weekend` | Mean, morning peak, weekend |
| `tm_ppm_wd` | `travel_time_seconds_mean_peak_pm_weekday` | Mean, evening peak, weekday |
| `tm_ppm_we` | `travel_time_seconds_mean_peak_pm_weekend` | Mean, evening peak, weekend |
| `tm_rod_wd` | `travel_time_seconds_mean_rest_of_day_weekday` | Mean, rest of day, weekday |
| `tm_rod_we` | `travel_time_seconds_mean_rest_of_day_weekend` | Mean, rest of day, weekend |
| `trp_pam_wd` | `trip_count_peak_am_weekday` | Trips, morning peak, weekday |
| `trp_pam_we` | `trip_count_peak_am_weekend` | Trips, morning peak, weekend |
| `trp_ppm_wd` | `trip_count_peak_pm_weekday` | Trips, evening peak, weekday |
| `trp_ppm_we` | `trip_count_peak_pm_weekend` | Trips, evening peak, weekend |
| `trp_rod_wd` | `trip_count_rest_of_day_weekday` | Trips, rest of day, weekday |
| `trp_rod_we` | `trip_count_rest_of_day_weekend` | Trips, rest of day, weekend |

### Packed summaries

| Column | Former name | Meaning |
|---|---|---|
| `days_week` | `days_of_week_summary` | Days the edge runs, `\|`-joined (`monday\|tuesday`) |
| `h_tts` | `hourly_travel_times` | Mean travel time per hour band, `"08-09:420\|09-10:395"` |
| `h_trips` | `hourly_trip_counts` | Trips per hour band, same format |

## GTFS schedule

Written only with `schedule=True`: one row per trip and edge, with no geometry.
It is **not** clipped to the study area, so it holds the full timetable of every
downloaded feed, including operators with no stop inside the area.

## NetworkX graphs (`graph.json`)

With `output_file_type="networkx"` each network is written as one
`graph.json`: `<mode>/graph.json` for OSM and `<bus|train>/<dataset>/graph.json`
for GTFS, next to `study_area_boundary.geojson`. Each file is a NetworkX
`MultiDiGraph` in node-link JSON:

```json
{
  "directed": true,
  "multigraph": true,
  "graph": {"crs": "EPSG:4326"},
  "nodes": [{"id": 1, "x": -0.37, "y": 39.47, "node_role": "intersection"}],
  "edges": [{"source": 1, "target": 2, "key": 0, "edge_id": 10, "...": "...",
             "geometry": "LINESTRING (-0.37 39.47, -0.36 39.47)"}]
}
```

- The edge list is always under `"edges"`, whatever networkx version wrote it.
  Files written before this was fixed may use `"links"`.
- `graph.crs` is the CRS of the node `x` / `y` and of the edge geometries, the
  same as the `crs` passed to `main()`.
- `geometry` is WKT text.
- Several edges can join the same pair of nodes; `key` tells them apart.

| | OSM graph | GTFS graph |
|---|---|---|
| Node `id` | `node_id` (integer) | `stop_id` (text) |
| Node attributes | `x`, `y`, `node_role` | `x`, `y`, `stop_name` |
| Edge attributes | the 14 [OSM edge](#osm-edges) columns | the [GTFS edge](#gtfs-edges) columns, with their long names |

`from_node` / `to_node` repeat `source` / `target`. They are kept so every edge
carries the same columns as in the other formats.

A GTFS edge that crosses the study-area boundary keeps its stop outside the
area, which the stops layer drops. In the graph that stop is still a node, with
its position taken from the edge geometry and its name from the feed.

Read a file with `multimodalpy.read_graph_json`, which accepts either edge key
and turns `geometry` into shapely objects:

```python
from multimodalpy import read_graph_json

G = read_graph_json("output/walking/graph.json")
G.graph["crs"]                          # 'EPSG:4326'
u, v, k, attrs = next(iter(G.edges(keys=True, data=True)))
attrs["geometry"]                       # shapely LineString
```

Without multimodalpy, use networkx directly:
`json_graph.node_link_graph(json.load(f), edges="edges")`.

---

## From municipality to data

**Municipality → polygon.** The name is looked up in the boundaries file in
increasingly loose steps, and the first step with a hit decides: the whole name,
one part of a bilingual name (`Elche` → `Elx/Elche`), whole words inside a name
(`Castellón` → `Castelló de la Plana/Castellón de la Plana`, not `Castellonet`),
any substring, and finally a small typo (at least 80% similar, a swap of two
adjacent letters counting as one edit). Accents and case are ignored.

The rows found must be **one** municipality (same `NATCODE`); a municipality
drawn as several polygons is dissolved into one. If a name matches several
different municipalities (`Castejón` exists in Cuenca and Navarra, `Alcalá` is in
16 names), the lookup fails with `AmbiguousAreaError` listing them, instead of
merging them into one area spanning several provinces. Pass `province=` (name or
INE code) or `area_code=` (5-digit INE code or full `NATCODE`) to choose. Matches
looser than an official name emit a `UserWarning` naming the area picked, and so
do the 7 non-municipal territories in the file (Gibraltar, Isla del Perejil, …),
which belong to no province.

The default file covers peninsular Spain, the Balearic Islands, Ceuta and
Melilla, **not the Canary Islands**; use `boundaries_path=` for other areas.

**Boundary buffer.** The exported `study_area_boundary` is always the exact
municipal boundary. `buffer_m` (default 0) widens only the area used to download
OSM and clip GTFS, so streets and routes crossing the boundary are not cut. OSM
streets with at least one end inside the area are kept (`truncate_by_edge`).

**Municipalities in several parts.** Some municipalities are several separate
polygons, e.g. Donostia/San Sebastián and its exclaves. Downloading only inside
them leaves the walking, bike and driving networks split into islands, because
the roads between the parts run through neighbouring municipalities. With
`connect_parts=True` (the default) the network is downloaded once over the
convex hull of the parts plus 1 km, cut to the municipality as usual, and the
shortest roads (by length) from the largest part to each other part and back
are added; both directions are needed because of one-way streets. Only those
roads are added, marked `connector=True`. Parts are numbered from 1, the largest;
a part with streets but no road to the others within that area (an island, say)
stays disconnected, is listed in the manifest's `unconnected_parts` and raises a
`UserWarning`. Single-polygon municipalities are downloaded exactly as before.

**Municipality → GTFS feeds.** The NAP endpoint `/conjunto-dato/region/{id}`
only accepts **INE province codes, 1 to 52**. The `/region` listing also returns
about 8,300 municipality and autonomous-community entries that share the same id
space: the municipality ones return 404, and the autonomous-community ones fall
inside the province range and silently return **another province's data**. So
the province is never matched by name. It is read from the boundary's
`NATCODE`, shaped `<2 country><2 region><2 province><5 municipality>`, and used
directly as the region id.

## Known limitations

- In the **networkx** export, GTFS graphs keep the long attribute names
  (`trip_count`, `travel_time_seconds_mean`, …), since JSON has no name length
  limit. OSM graphs follow the edge schema above.
- The **GTFS schedule** is not clipped to the study area (see above).
- `lanes` is still exported as text and can hold `;`-joined values.
- In the **networkx** export, OSM node ids are integers and GTFS node ids are
  text, so the graphs cannot be joined on node id as they are.

## Abbreviations

| Abbreviation | Meaning |
|---|---|
| `tts` | travel time in seconds |
| `tm` | mean travel time (four-part columns) |
| `trips`, `trp` | number of trips |
| `m`, `md`, `mn`, `mx` | mean, median, minimum, maximum |
| `pam`, `ppm`, `rod` | morning peak, evening peak, rest of day |
| `wd`, `we` | weekday, weekend |
| `h_` | per hour band |
| `sh`, `ln` | short name, long name |
| `raw` | original OSM value, not normalised |