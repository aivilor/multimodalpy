# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- CI runs the tests on macOS with the oldest and newest supported Python (3.11
  and 3.14) only; Ubuntu still runs all four. GitHub's macOS runners are scarce,
  and a job waiting for one could fail the whole run.

## [0.5.1] - 2026-11-19

### Added
- `gtfs_report` in the manifest, also written as `gtfs_report.csv` in the output
  folder: what happened to every NAP dataset of the area's provinces (written
  with its routes, stops and edges in the area, nothing in the area, skipped for
  its mode, not downloaded because of its declared transport type, failed to
  download or to process, with the error). A dataset that could not be used at
  all raises a `UserWarning`. Before, a feed that failed to process disappeared
  without a trace, since the library's log is silent unless the application
  configures logging, and the download report was inside `gtfs_zips/`, deleted
  unless `keep_gtfs_zips=True` (#90).

### Changed
- GTFS dataset names no longer carry the timestamp the NAP puts in front of its
  file names: `20261119_030028_GenValenciana_Interurbano.zip` gives
  `genvalenciana_interurbano` instead of `20261119_030028_genvalenciana_interurbano`.
  The output folders (`bus/<dataset>/`, `train/<dataset>/`), the manifest's
  `gtfs_datasets` and the edge ids are now the same on every download, so two
  downloads can be compared. Two files that end up with the same name get `_2`,
  `_3`… (#90).

### Fixed
- The GTFS nodes did not match the edges (#90). The nodes were every stop of
  `stops.txt` inside the area, whether or not a trip served it, while the edges
  came from the trips: in the Valencia interurban feed, whose `stops.txt` is a
  regional stop catalogue, 1,209 of 1,374 nodes had no edge, and 40 stops ending
  edges that cross the boundary had no node. The nodes are now the stops the
  edges use, and only those; a stop just outside the area that ends an edge is
  kept with the new column `in_area = False` (as the matrix already did). The
  NetworkX graph carries `in_area` too.
- The GTFS schedule (`write_schedule=True`) was not clipped to the study area:
  it held the whole timetable of each feed (86,503 rows for the Valencia
  interurban feed, of which 1,415 belong to edges in the area). It is now
  clipped like the edges, so every row has its edge (#90).
- GTFS `edge_id` was not unique: edges are one per pair of stops and route, but
  the id was built from the stops alone, so routes sharing a pair of stops (106
  pairs in the Valencia interurban feed) repeated it, and joining the schedule
  to the edges by `edge_id` matched the wrong rows. It is now
  `<dataset>_<route_id>_<from_node>_<to_node>` (#90).
- A GTFS segment with an impossible travel time between two consecutive stops,
  an error in the feed (one Valencia interurban segment "took" 10.9 hours),
  inflated the mean and maximum times of its edge and the matrix weights.
  Segments over 3 hours (`MAX_SEGMENT_SECONDS`) now have no travel time: they
  still count as trips, and a `UserWarning` says how many there were in the
  area (#90).
- The GTFS median travel times (`tts_md`, `tts_md_pam`, `tts_md_ppm`,
  `tts_md_rod`, and the matrix weights of the same names) were documented but
  never computed, so they were missing from every output. They are now
  computed, for the whole day and per period (#90).

[0.5.1]: https://github.com/aivilor/multimodalpy/compare/v0.5.0...v0.5.1

## [0.5.0] - 2026-11-18

### Added
- `boundaries_path` (and `--boundaries`) also accepts a GeoPackage (`.gpkg`).
  One with a single layer is read as it is; with several, its only polygon
  layer; several polygon layers raise an error listing them (#86).
- The manifest's `skipped_datasets` and `gtfs_period_codes`, and the matrix
  metadata's `gtfs_period_codes` (see below) (#86).
- CI runs the tests on Ubuntu and macOS with Python 3.11 to 3.14, and a lint
  job (`ruff check`, `ruff format --check`, `mypy`). ruff is pinned in the
  `lint` dependency group and no longer formats the Markdown files (#86).
- `download_network()` and `download_osm()` take `overpass_url`, to download the
  OpenStreetMap networks from another Overpass server, and `osm_timeout`, the
  seconds to wait for each answer (`--overpass-url` and `--osm-timeout` on the
  command line) (#85). There is no automatic switch to another server.
- `OSMDownloadError` (in `multimodalpy.network`): an OpenStreetMap network could
  not be downloaded. It names the network, the server and the HTTP status, and
  says whether the failure is usually temporary (#85).
- The manifest's `failed_modes` lists the OpenStreetMap networks that could not
  be downloaded, with the reason (#85).

### Changed
- `osmnx>=2.0` is required: the code already used the osmnx 2 API, so osmnx 1
  failed with an `AttributeError` (#85).
- While downloading, the requests to Overpass carry a multimodalpy User-Agent,
  and osmnx's cache is kept next to the municipal boundaries
  (`pooch.os_cache("multimodalpy")`) instead of a `cache` folder in the working
  directory, so a rerun from any folder reuses what was downloaded. osmnx
  settings you chose yourself are kept (#85).
- **Breaking:** `boundaries_path` is now the study area itself, not a file to
  look a municipality name up in (#83). Before, `download_network()` and
  `find_area()` searched `area_name` in the file's name columns, so a file
  without the columns the library knows (e.g. the polygon of a *Plan de Acción
  Territorial*, with `pat` and `objectid`) failed with
  `No name column found in the boundaries file`. Now:
  - the file's polygons are merged into one (`union_all()`) and are the study
    area, whatever their columns: `area_name`, `area_code`, `name_columns` and
    `code_columns` are not needed;
  - any CRS is accepted and converted, but the file must define one: a file
    without a CRS (e.g. a shapefile without `.prj`) raises `ValueError`;
  - the area is crossed with the packaged municipal boundaries to find the
    provinces and municipalities it overlaps. A province counts when it holds
    at least 5% of the area, so the slivers along the edge of a polygon drawn
    with another precision do not add neighbouring provinces. Its bus and train
    feeds are downloaded, and everything is clipped to your polygon;
  - an area outside the covered territory (peninsular Spain, the Balearic
    Islands, Ceuta and Melilla; not the Canary Islands) raises `ValueError`
    before anything is downloaded or written. When more than 5% of the area is
    outside the municipal boundaries (the sea, Portugal), a `UserWarning` says
    so.
- **Breaking:** `find_area()` returns the lists `prov_codes`, `prov_names`,
  `mun_codes` and `mun_names` (INE codes and names of the provinces and
  municipalities of the area) instead of `province_code`. The search by name
  always uses the packaged boundaries, so `name_columns` and `code_columns` are
  removed, and `area_code` or `province` together with `boundaries_path` raise
  `ValueError`.
- **Breaking:** `download_gtfs()` takes `province_codes` (a list) instead of
  `province_code`, and reads them from the boundary's `prov_codes`.
- **Breaking:** the study-area columns `matched_column`, `matched_value`,
  `matched_rows` and `source_path` are now `match_col`, `match_val`,
  `match_rows` and `source`, in `find_area()`'s result and the
  `study_area_boundary` layer. The old names were longer than ten characters,
  so a Shapefile cut them short with a warning (#86).
- A GTFS feed whose routes are of a mode that was not asked for is skipped
  instead of written: the NAP declares some feeds as both bus and rail, so
  asking for `bus` also downloaded Cercanías into `train/`. Skipped feeds are
  listed in the manifest's `skipped_datasets` (#86).
- `download_network()`: `area_name` is optional. With `boundaries_path` it is
  only the label of the files and the manifest, and defaults to the file name
  (`pateval.geojson` → `pateval.gpkg`); with `area_code` alone, it is the
  municipality's name. The area is resolved before the output folder is
  created, so a wrong area leaves nothing behind.
- The `study_area_boundary` layer has the columns `prov_codes`, `prov_names`,
  `mun_codes` and `mun_names`, as text joined with `"; "`. The manifest adds
  `boundaries_path`, `provinces` and `municipalities`.
- Command line: `--area` is no longer required; give `--area`, `--area-code` or
  `--boundaries` (`multimodalpy --boundaries pateval.geojson --modes bus`).

  | 0.4.0 | Now |
  |---|---|
  | `download_network("Valencia", boundaries_path="my_municipalities.shp")` | `download_network("Valencia")` (packaged boundaries) |
  | (not possible) | `download_network(boundaries_path="my_area.geojson")` |
  | `find_area(...)["province_code"]` | `find_area(...)["prov_codes"]` (a list) |
  | `find_area(...)["matched_column"]`, `["matched_value"]`, `["matched_rows"]`, `["source_path"]` | `["match_col"]`, `["match_val"]`, `["match_rows"]`, `["source"]` |
  | `find_area(name, path, name_columns=..., code_columns=...)` | removed: give the area as a geometry |
  | `download_gtfs(..., province_code=46)` | `download_gtfs(..., province_codes=[46])` |

### Fixed
- A study area spanning several provinces only got the bus and train feeds of
  one, and a boundaries file without a province code had its province guessed
  from `area_name`, even with `province=` given (#83). The feeds of every
  province of the area are now downloaded, and a feed published for several of
  them (e.g. Renfe) only once. The NAP download report
  (`nap_download_report.csv`) has a `province_code` column.
- When the public Overpass server was at capacity, the OpenStreetMap download
  failed at once with a `JSONDecodeError` or a `ResponseStatusCodeError`
  carrying the server's whole HTML page, and every network already downloaded
  was lost (#85). Now:
  - temporary failures (server busy, timeout, lost connection) are retried four
    times, waiting 10, 20, 40 and 80 seconds;
  - osmnx no longer retries 429 and 504 answers forever, no longer waits a
    fixed 60 seconds when the server's status page can't be read or the server
    can't be reached at all (each attempt first checks that it answers), and no
    longer returns the partial data of a query the server cut short as if it
    were complete: those are retried too;
  - a network that still fails is reported in `failed_modes`, and the others
    are written; `OSMDownloadError` is raised only if nothing could be
    downloaded.
- OUIGO was saved under `bus/`: its routes use the extended route type 101
  (high-speed rail), and only the basic route types were recognised as rail.
  The extended railway (100–199), suburban, urban and metro rail (300–699),
  tram (900–999) and funicular (1400–1499) types now count as train (#86).
- With `gtfs_peak_periods` named other than `peak_am` and `peak_pm`, the
  per-period columns were computed but dropped: the columns kept, their short
  names and the matrix weights only knew the default names. They now follow the
  periods used. The default periods keep `pam`, `ppm` and `rod`; others get
  their first three letters (`morning` gives `tts_m_mor`, `tm_mor_wd`…) or `p1`,
  `p2`… when those clash, listed in the manifest's `gtfs_period_codes` and the
  matrix metadata (#86).

[0.5.0]: https://github.com/aivilor/multimodalpy/compare/v0.4.0...v0.5.0

## [0.4.0] - 2026-11-03

### Changed
- **Breaking:** the public API is reorganised into four modules, one per task,
  with clearer names (#77). There are no compatibility aliases; update your
  code with this table:

  | 0.3.0 | Now |
  |---|---|
  | `get_network.main()` | `multimodalpy.download_network()` (`network.download_network`) |
  | `get_area.find_area_boundary()` | `multimodalpy.find_area()` (`area.find_area`) |
  | `get_area.download_osm_layers()` | `network.download_osm()` |
  | `get_area.download_gtfs_layers()` | `network.download_gtfs()` |
  | `get_area.AmbiguousAreaError` | `area.AmbiguousAreaError` |
  | `get_area.NAPAuthenticationError` | `network.NAPAuthenticationError` |
  | `get_network.read_matrix()` | `multimodalpy.read_matrix()` (`io.read_matrix`) |
  | `get_network.read_graph_json()` | `multimodalpy.read_graph()` (`io.read_graph`) |
  | `from multimodalpy.isochrones import isochrones` | `multimodalpy.isochrones()` (`analysis.isochrones`) |
  | `python -m multimodalpy.get_network` | `multimodalpy` or `python -m multimodalpy` |

- **Breaking:** `download_network()` parameters renamed: `output_path` →
  `output_dir`, `output_file_type` → `formats`, `schedule` →
  `write_schedule`, `gtfs_zips` → `keep_gtfs_zips`. The manifest it returns
  uses `formats` and `output_dir` as keys. Command-line flags follow:
  `--output-dir`, `--formats`, `--write-schedule`, `--keep-gtfs-zips`.
- The remaining helpers of `get_area` (`slugify`, `normalize_name`,
  `levenshtein_*`, `resolve_nap_api_key`, `find_dotenv`, ...) are now private.
- The API reference documents only each module's public names.

### Added
- `shortest_path()` (`multimodalpy.analysis`, #78): the shortest path from
  every origin to every destination (node ids or coordinates) within a
  threshold in minutes (`weight="tts"`) or metres (`weight="length"`), on a
  walking, bike or driving network in any output format. Returns a
  GeoDataFrame with `origin`, `destination`, `time_min`, `length_m`,
  `n_edges` and the path as a line (`geometry=False` for large tables); pairs
  beyond the threshold are left out. Bus and train raise
  `NotImplementedError` until the multimodal network is available.
- `multimodalpy` command, installed with the package
  (`multimodalpy --area Valencia --modes walking`).
- `find_area()` has a full docstring (parameters, errors, examples).

### Removed
- The `multimodal` parameter of `download_network()`, which only raised
  `NotImplementedError`; it will come back with the multimodal network.
- The `--clean-gtfs` command-line flag, which had no effect.

[0.4.0]: https://github.com/aivilor/multimodalpy/compare/v0.3.0...v0.4.0

## [0.3.0] - 2026-10-30

### Added
- New output format `matrix` (#63): `output_file_type="matrix"` (or
  `--output-file-type matrix`) writes each network as one `matrix.npz`, a
  directed sparse adjacency matrix in CSR form for shortest-path calculations.
  Walking, bike and driving matrices carry `tts` and `length`; bus and train
  matrices carry the 18 travel-time columns of the GTFS edge layer. Parallel
  edges keep the minimum of each weight, and GTFS periods without service are
  `inf`. The file opens with `scipy.sparse.load_npz`. The layout is described
  in the "Matrix" section of `DATA_SCHEMA.md`.
- `multimodalpy.read_matrix()` reads a `matrix.npz` (or its folder) and returns
  the matrix of one weight as a `scipy.sparse.csr_array`, the node table and
  the metadata.
- `scipy` is now a required dependency.
- New public module `multimodalpy.isochrones` (#64). `isochrones()` computes
  the area reachable from one or more origins (node ids or coordinates) within
  each cutoff, in minutes (`weight="tts"`) or metres (`weight="length"`), on a
  walking, bike or driving network. It reads the network in any output format
  (output folder, network folder, `matrix.npz`, `graph.json`, GeoPackage,
  GeoJSON, Shapefile, or the result of `read_matrix` / `read_graph_json`) and
  returns a GeoDataFrame with one polygon per origin and cutoff, nested and in
  the network's CRS. `method="edges"` (default) buffers the streets reached,
  including the reached part of edges crossing the cutoff; `method="hull"`
  draws a concave hull. Bus and train isochrones raise `NotImplementedError`
  until the multimodal network is available.
- `shapely>=2.0` is now required explicitly (for `shapely.concave_hull`).

### Changed
- **Breaking:** `process_osm` and `process_gtfs` are now private modules
  (`_process_osm`, `_process_gtfs`) and are no longer part of the public API:
  `multimodalpy.process_osm` and `multimodalpy.process_gtfs` no longer exist,
  and both modules are gone from the API reference. The public API is
  `get_network`, `get_area` and the names in `multimodalpy.__all__`. Code that
  imported them directly should use `get_network.main()` or the `get_area`
  download functions, which return the standardized layers.

## [0.2.0] - 2026-10-29

### Added
- Municipalities made of several separate parts (e.g. Donostia/San Sebastián
  and its exclaves) get the shortest roads joining their parts added to the
  walking, bike and driving networks, so the network is no longer split into
  islands. On by default; `connect_parts=False` / `--no-connect-parts` turns it
  off. Parts that cannot be reached are listed in the manifest's
  `unconnected_parts` and reported with a `UserWarning` (#61).
- OSM edges have a new standard column, `connector`: `true` on the roads added
  outside the municipality to join its parts, `false` everywhere else.
- `--api-key` command-line option for the NAP API key (#62).
- `get_area.resolve_nap_api_key()`, `get_area.find_dotenv()` and
  `get_area.NAPAuthenticationError` (#62).

### Changed
- The NAP API key is checked at the start of `main()`, before anything is
  downloaded: without one, a `UserWarning` says so straight away and bus/train
  are skipped, instead of finding out after the OSM download (#62).
- A `.env` file is also found in the parent folders of the working folder, and
  the missing-key message names the folder searched (#62).

### Fixed
- A key rejected by the NAP while downloading the feed files raised no clear
  error: each file became a "failed" row in `nap_download_report.csv`. It now
  stops with `NAPAuthenticationError`, as the other NAP calls do (#62).
- `.env` and `.venv/` are in `.gitignore`, so a NAP key in `.env` is not
  committed by accident (#62).

[0.3.0]: https://github.com/aivilor/multimodalpy/compare/v0.2.0...v0.3.0

## [0.1.5] - 2026-10-14

### Added
- `multimodalpy.read_graph_json()` reads a networkx `graph.json`, accepting
  either edge-list key and turning `geometry` WKT into shapely objects.
- A "NetworkX graphs" section in `DATA_SCHEMA.md` describing `graph.json`.

### Fixed
- `graph.json` stored the edge list under `"links"` or `"edges"` depending on
  the installed networkx version; it is now always `"edges"`. Requires
  networkx 3.4 or later.
- GTFS graphs now record their CRS in `graph.crs`, like OSM graphs.
- GTFS graphs now carry the same edge columns as the GTFS edge layers
  (`edge_id`, period and weekday statistics, geometry as WKT, …). The
  `hour_band` attribute, which was always empty, is gone; per-hour detail is in
  `hourly_travel_times` / `hourly_trip_counts`.
- GTFS stops outside the study area that are reached by an edge crossing the
  boundary now have coordinates and a name in the graph instead of being empty
  nodes.

### Removed
- The unused internal helper `get_network._osm_graph_to_json()`.

[0.1.5]: https://github.com/aivilor/multimodalpy/compare/v0.1.4...v0.1.5


## [0.1.4] - 2026-10-14

### Changed
- Rewrote the README and docs index to better match what the package
  actually does.
- OSM edges now use one standard schema in every output format:
  `edge_id`, `from_node`, `to_node`, `highway`, `hwy_raw`, `lanes`,
  `maxspeed`, `spd_raw`, `name`, `oneway`, `reversed`, `length`, `tts`,
  `geometry`.
- OSM and GTFS edges both produce `from_node` / `to_node` directly
  (formerly `from_node_id` / `to_node_id`), so both networks use the
  same names, including the GTFS NetworkX graph and schedule table.
- All standard edge columns are always written, in that order; columns the
  network has no tag for are left empty instead of being dropped.
- The NetworkX `graph.json` for OSM modes is now built from the final
  standardized layers (split edges, `tts`, standard attribute names)
  instead of the raw osmnx graph.
- Area names are matched in steps (whole name, bilingual part, whole
  words, substring, typo), and a name matching several different
  municipalities raises `AmbiguousAreaError` listing them instead of
  merging them. Before, `Elche` merged Elx/Elche with Elche de la Sierra
  and `San Sebastián` merged three towns in three provinces, and the GTFS
  province was taken from whichever came first.
- Fuzzy matching needs 80% similarity (was 70%) and counts adjacent
  transpositions as one edit, so `Las Palmas` no longer resolves to Las
  Valeras; a tie between two towns (`Gerona`: Girona / Gerena) is reported
  as ambiguous.
- OSM downloads keep streets that cross the study-area edge
  (`truncate_by_edge=True`).
- `geopandas>=1.0` is required (`union_all`).

### Added
- `province=` in `find_area_boundary`, `get_network.main` and `--province`
  in the CLI, to choose between municipalities that share a name.
- `area_code` accepts the 5-digit INE municipality code as well as the
  full NATCODE.
- `buffer_m=` / `--buffer-m` widens the download and GTFS clipping area;
  the exported boundary stays exact. Recorded in the manifest.
- `UserWarning` when the match is looser than an official name, or the area
  is a non-municipal territory; the not-found error mentions that the
  default file has no Canary Islands.

### Fixed
- The boundaries download now has its SHA-256 hash, so pooch verifies it.
- `__MACOSX/._*.shp` metadata files in the release zip are skipped when
  picking the shapefile.

[0.1.4]: https://github.com/aivilor/multimodalpy/compare/v0.1.3...v0.1.4


## [0.1.3] - 2026-09-30

### Fixed
- OSM topology node-matching was rounded in the requested `output_crs`
  instead of a fixed projected/metric CRS, so distinct intersections
  could be merged (or matching ones missed) depending on what CRS was
  requested — most noticeably in EPSG:4326, where 3 decimal degrees is
  roughly 100m. Topology is now always derived in a projected metric CRS
  chosen from the data itself, independent of `output_crs`.
- `load_dotenv()` was never actually called, so a NAP API key set in a
  local `.env` file was silently ignored; only an explicit `api_key`
  argument or an already-exported environment variable worked.

### Added
- A `UserWarning` is now raised when GTFS layers fail to download, since
  previously the network would silently come back missing bus/train
  layers with the reason only visible in the logger.


## [0.1.1] - 2026-09-30

### Added
- `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md`, aligning the project with
  pyOpenSci's peer-review documentation requirements.
- Sphinx + Read the Docs configuration (`docs/`, `.readthedocs.yaml`) for
  auto-generated API reference documentation.
- `CITATION.cff` for machine-readable citation metadata.

### Changed
- Republished to TestPyPI with corrected README (installation
  instructions, badges, repository links) and dev-install instructions
  matching the project's `[dependency-groups]` setup
  (`pip install -e . --group dev`).

### Fixed
- Corrected `.gitignore` so build artifacts in `dist/` are actually
  excluded from version control (previous pattern only matched a nested,
  nonexistent `data/outputs/dist/` path).
- Removed the tracked `data/` folder from the repository; the default
  municipal boundaries are already fetched at runtime via `pooch` from a
  GitHub release asset, so bundling them in the repo was redundant.
- Restored nested `{mode}/{dataset}/...` output paths for GTFS layers
  (nodes, edges, schedule tables) that were dropped during a merge
  conflict resolution.

## [0.1.0] - 2026-09-30

### Added
- Initial release: OSM + GTFS multimodal network download and export
  pipeline (`get_network.main`), supporting GeoJSON, Shapefile,
  GeoPackage, and NetworkX graph output formats.
- Published to TestPyPI for pre-release testing.

[Unreleased]: https://github.com/aivilor/multimodalpy/compare/v0.5.1...HEAD
[0.1.3]: https://github.com/aivilor/multimodalpy/compare/v0.1.2...v0.1.3
[0.1.1]: https://github.com/aivilor/multimodalpy/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/aivilor/multimodalpy/releases/tag/v0.1.0