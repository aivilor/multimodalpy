# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

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

### Changed
- Republished to TestPyPI with corrected README (installation
  instructions, badges, repository links) and dev-install instructions
  matching the project's `[dependency-groups]` setup
  (`pip install -e . --group dev`).

## [0.1.0] - 2026-09-30

### Added
- Initial release: OSM + GTFS multimodal network download and export
  pipeline (`get_network.main`), supporting GeoJSON, Shapefile,
  GeoPackage, and NetworkX graph output formats.
- Published to TestPyPI for pre-release testing.

## [Unreleased]

### Added
- `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md`, aligning the project with
  pyOpenSci's peer-review documentation requirements.
- Sphinx + Read the Docs configuration (`docs/`, `.readthedocs.yaml`) for
  auto-generated API reference documentation.
- `CITATION.cff` for machine-readable citation metadata.

### Fixed
- OSM topology no longer depends on the output CRS. Topology nodes were
  matched on coordinates rounded to 3 decimals in the requested `crs`,
  which in the default EPSG:4326 is ~100 m: nearby intersections merged
  into a single node, edges became self-loops and nodes were moved to the
  rounded position. The topology is now built in a projected CRS in
  metres chosen from the data (the local UTM zone), with a 1 mm
  tolerance, and the final layers are reprojected to `crs` at the end.
  Nodes keep their exact coordinates.
- Corrected `.gitignore` so build artifacts in `dist/` are actually
  excluded from version control (previous pattern only matched a nested,
  nonexistent `data/outputs/dist/` path).
- Removed the tracked `data/` folder from the repository; the default
  municipal boundaries are already fetched at runtime via `pooch` from a
  GitHub release asset, so bundling them in the repo was redundant.
- Restored nested `{mode}/{dataset}/...` output paths for GTFS layers
  (nodes, edges, schedule tables) that were dropped during a merge
  conflict resolution.


[Unreleased]: https://github.com/aivilor/multimodalpy/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/aivilor/multimodalpy/compare/v0.1.2...v0.1.3
[0.1.1]: https://github.com/aivilor/multimodalpy/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/aivilor/multimodalpy/releases/tag/v0.1.0