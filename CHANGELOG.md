# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


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