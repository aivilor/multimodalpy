# multimodalpy

[![Tests](https://github.com/aivilor/multimodalpy/actions/workflows/tests.yml/badge.svg)](https://github.com/aivilor/multimodalpy/actions/workflows/tests.yml)
[![PyPI version](https://img.shields.io/pypi/v/multimodalpy.svg)](https://pypi.org/project/multimodalpy/)
[![Python versions](https://img.shields.io/pypi/pyversions/multimodalpy.svg)](https://pypi.org/project/multimodalpy/)
[![Documentation](https://readthedocs.org/projects/multimodalpy/badge/?version=latest)](https://multimodalpy.readthedocs.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Transport networks for Spanish municipalities, from OpenStreetMap and official GTFS data.

Give `multimodalpy` the name of a municipality in Spain and it downloads and
standardizes its transport networks, one layer per mode:

- **Walking, cycling and driving** from OpenStreetMap (via `osmnx`), with every
  intersection as a node and a travel time on every edge.
- **Bus and train** from the official GTFS feeds published on Spain's
  [National Access Point (NAP)](https://nap.transportes.gob.es), as stop-to-stop
  edges with travel times and trip counts by period of the day.

All modes share the same column names, and the layers are written as GeoJSON,
Shapefile, GeoPackage, NetworkX graphs or sparse matrices, ready for GIS or
network analysis. On the walking, cycling and driving networks, `multimodalpy`
also computes isochrones: the area reachable from a point within a travel time.

> **Scope.** Only municipalities in Spain are supported: peninsular Spain, the
> Balearic Islands, Ceuta and Melilla. The Canary Islands are not in the default
> boundaries file (you can pass your own with `boundaries_path`).
>
> **Status.** Each mode is exported as its own network. Linking them into a
> single multimodal network (e.g. walking to and from stops) is planned but not
> implemented yet.

## Installation

```bash
pip install multimodalpy
```

Python 3.11 or newer is required. No separate GDAL install is needed:
`geopandas`/`pyogrio` ship prebuilt GDAL bindings via wheels.

To install from source:

```bash
git clone https://github.com/aivilor/multimodalpy.git
cd multimodalpy
python -m venv .venv
source .venv/bin/activate  # on Windows: .venv\Scripts\activate
pip install -e .
```

For development (tests, linting, type-checking, docs):

```bash
pip install -e . --group dev
```

(requires pip ≥ 25.1 for `--group`; alternatively, with [uv](https://docs.astral.sh/uv/): `uv sync --group dev`)

## Quick start

Street networks need no setup:

```python
from multimodalpy import get_network

manifest = get_network.main(
    "Valencia",
    modes=["walking", "bike", "driving"],
    output_path="output",
    output_file_type="geojson",
)
print(manifest["files"])
```

The first run downloads the municipal boundaries file (about 50 MB) and caches
it, so later runs start straight away.

### Bus and train: NAP API key

Public transport data comes from the NAP API, which needs a free API key:
register on the [NAP portal](https://nap.transportes.gob.es). The key is looked
for in this order:

1. `main(..., api_key="...")`, or `--api-key` in the command line;
2. the `NAP_API_KEY` environment variable;
3. a `.env` file in the folder you run Python from **or any parent folder**
   (so a `.env` at the root of your project also works from `notebooks/`):

```bash
NAP_API_KEY=your-key-here
```

`.env` is in the repository's `.gitignore`, so the key is not committed by
accident. Prefer the environment variable or `.env` over `--api-key`, which
stays in your shell history.

```python
manifest = get_network.main("Valencia", modes=["walking", "bus", "train"])
```

The key is checked before anything is downloaded. Without one, a warning says
so straight away and bus/train are skipped; the street layers are still written.
If the NAP rejects the key, or has no feeds for the area, the street layers are
also written and a warning explains why there is no bus or train data
(`manifest["gtfs_status"]`).

### Command line

```bash
python -m multimodalpy.get_network --area Valencia --modes walking bus \
    --output output --output-file-type geojson geopackage --buffer-m 500
```

Run `python -m multimodalpy.get_network --help` for every option.

## What you get

```
output/
├── study_area_boundary.geojson
├── walking/                 nodes  edges
├── bike/                    nodes  edges
├── driving/                 nodes  edges
├── bus/<dataset>/           nodes  edges  schedule.csv
├── train/<dataset>/         nodes  edges  schedule.csv
└── <area>.gpkg              only with output_file_type="geopackage"
```

- **Street edges** (every mode): `edge_id`, `from_node`, `to_node`, `highway`,
  `lanes`, `maxspeed`, `name`, `oneway`, `reversed`, `length` (metres) and `tts`
  (travel time in seconds), plus the original tags in `hwy_raw` and `spd_raw`.
- **Transit edges**: one edge per pair of consecutive stops and route, with the
  number of trips and the mean, median, minimum and maximum travel time, overall
  and by period of the day and type of day.
- **Nodes**: for streets, every intersection, dead end and shape point, with
  its role in `node_role`; for transit, the stops (`node_id`, `stop_name`).

Every column, the travel-time model and how each value is computed are described
in [DATA_SCHEMA.md](DATA_SCHEMA.md).

### Shortest paths: the matrix format

`output_file_type="matrix"` writes each network as one `matrix.npz`: a directed
sparse adjacency matrix (CSR) with one vector per weight (`tts` and `length`
for streets, the travel-time statistics for bus and train). Read it with
`read_matrix` and pass it to scipy's shortest-path functions:

```python
from scipy.sparse.csgraph import dijkstra

from multimodalpy import read_matrix

data = read_matrix("output/walking")
origin = data.nodes.index[data.nodes["node_id"] == 1234][0]
seconds = dijkstra(data.matrix, indices=origin, limit=600)
```

For bus and train the matrix holds in-vehicle times only: waiting and transfers
are not modelled. See the
[Matrix section of DATA_SCHEMA.md](DATA_SCHEMA.md#matrix-matrixnpz).

### Isochrones

`isochrones` draws the area reachable from one or more origins within each
travel time, on a walking, bike or driving network:

```python
from multimodalpy.isochrones import isochrones

areas = isochrones(
    "output",                     # output folder of main()
    origins=(-0.3763, 39.4699),   # (x, y) in the network's CRS, or a node_id
    cutoffs=[5, 10, 15],          # minutes
    mode="walking",
)
areas.to_file("isochrones.geojson")
```

It reads the network in any output format (`matrix.npz` is the fastest) and
returns one polygon per origin and cutoff, in the network's CRS. By default
(`method="edges"`) the area is a band of `buffer_distance` metres (25) along the
streets reached, so it follows the street network; `method="hull"` draws a
concave hull around them instead, faster when most of the network is reached.
With `weight="length"` the cutoffs are metres. Coordinates are snapped to the
nearest node within `max_snap_distance` metres (250). Bus and train isochrones
will come with the multimodal network.

## Main options

| Option | Default | What it does |
|---|---|---|
| `modes` | `["walking"]` | Any of `walking`, `bike`, `driving`, `bus`, `train`. Spanish synonyms work too (`caminable`, `bicicleta`, `coche`). |
| `output_file_type` | `"geojson"` | `geojson`, `shapefile`, `geopackage`, `networkx`, `matrix`, or a list to write several at once. |
| `crs` | `"EPSG:4326"` | CRS of every output layer, e.g. `"EPSG:25830"` for metres in Spain. |
| `province` | `None` | Province name or INE code, to choose between towns that share a name (e.g. `"Castejón"` exists in Cuenca and Navarra). |
| `area_code` | `None` | The municipality's 5-digit INE code (`"46250"` for València) or its full INSPIRE `NATCODE`. |
| `buffer_m` | `0` | Widen the download area by this many metres, so streets and routes crossing the municipal boundary are not cut. The exported boundary stays exact. |
| `connect_parts` | `True` | For municipalities in several separate parts (e.g. Donostia/San Sebastián), add the shortest roads joining them, marked `connector=True`. `--no-connect-parts` in the CLI. |
| `boundaries_path` | packaged file | Your own boundaries file (Shapefile or GeoJSON). |
| `schedule` | `False` | Also write the trip-by-trip timetable of each GTFS feed. |
| `gtfs_hour_range` | `None` | Only keep trips in an hour window, e.g. `(7, 9)`. |

`main()` writes the files and returns a manifest (a dictionary listing them).
To work with the layers in memory instead, use the functions in
`multimodalpy.get_area`, which return GeoDataFrames. See the
[API reference](https://multimodalpy.readthedocs.io/en/latest/api.html).

## How it works

1. **Study area.** The municipality name is matched in the boundaries file
   (accents, bilingual names such as `Elx/Elche` and small typos are handled)
   to get its polygon and province code. If the name fits several
   municipalities, you get the list of candidates and pick one with
   `province` or `area_code`, instead of a merged area.
2. **Streets.** `osmnx` downloads the OSM network inside the polygon (widened
   by `buffer_m` if set). If the municipality is made of separate parts, the
   shortest roads joining them are added, so the network is not split into
   islands.
   `multimodalpy` adds a node at every real intersection, splits the edges
   there and computes a travel time per mode.
3. **Public transport.** The province's GTFS feeds are downloaded from the NAP,
   clipped to the area and turned into stop-to-stop edges with timetable
   statistics.
4. **Export.** Every layer is written in the requested formats with the same
   column names across modes.

## Documentation

API reference: <https://multimodalpy.readthedocs.io>. Column-level
details are in [DATA_SCHEMA.md](DATA_SCHEMA.md), and changes between versions in
[CHANGELOG.md](CHANGELOG.md).

## Getting help & contributing

- Found a bug or have a feature request? [Open an issue](https://github.com/aivilor/multimodalpy/issues).
- Want to contribute? See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and the contribution process.
- This project follows a [Code of Conduct](CODE_OF_CONDUCT.md).
- Licensed under the [MIT License](LICENSE).

## Citation

If you use `multimodalpy` in your research, please cite the software and state
the version you used. On GitHub, the "Cite this repository" button in the
sidebar gives a ready-made citation (APA, BibTeX) from
[`CITATION.cff`](CITATION.cff).

> Villalba Ortiz, A. (2026). *multimodalpy: Easy creation of multimodal
> transport networks from OpenStreetMap and GTFS data* (Version X.Y.Z)
> [Computer software]. https://doi.org/10.5281/zenodo.NNNNNNN

```bibtex
@software{villalba_multimodalpy,
  author    = {Villalba Ortiz, Aida},
  title     = {multimodalpy: Easy creation of multimodal transport networks
               from OpenStreetMap and GTFS data},
  year      = {2026},
  version   = {0.2.0},
  doi       = {10.5281/zenodo.NNNNNNN},
  url       = {https://github.com/aivilor/multimodalpy},
  license   = {MIT}
}
```

Each release is archived on Zenodo with its own DOI; the DOI above is the one
for all versions. Use the version DOI listed on the
[Zenodo record](https://doi.org/10.5281/zenodo.NNNNNNN) when you need to cite
the exact release you ran.