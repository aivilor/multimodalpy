# multimodalpy

[![Tests](https://github.com/aivilor/multimodalpy/actions/workflows/tests.yml/badge.svg)](https://github.com/aivilor/multimodalpy/actions/workflows/tests.yml)
[![TestPyPI version](https://img.shields.io/badge/TestPyPI-0.1.0-blue.svg)](https://test.pypi.org/project/multimodalpy/) <!-- swap for a real PyPI badge once published there -->
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Easy creation of multimodal transport networks from OpenStreetMap and GTFS data.

`multimodalpy` builds routable, multimodal transport network graphs (walking, cycling, driving, and public transit) by combining OpenStreetMap street data with GTFS public transit feeds, and exports them as GeoDataFrames, GeoPackages, shapefiles, or graph files ready for network analysis (e.g. with `networkx` or `osmnx`). It complements general-purpose OSM tools like `osmnx` by adding a dedicated GTFS-to-network pipeline and unified multimodal output formats.

## Installation

`multimodalpy` is currently available on [TestPyPI](https://test.pypi.org/project/multimodalpy/) while it goes through review. To install it, you also need the real PyPI for its dependencies:

```bash
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ multimodalpy
```

<!-- Once published on the real PyPI, replace the above with:
pip install multimodalpy
-->

Or install from source:

```bash
git clone https://github.com/aivilor/multimodalpy.git
cd multimodalpy
python -m venv .venv
source .venv/bin/activate  # on Windows: .venv\Scripts\activate
pip install -e .
```

For development (running tests, linting, type-checking, building docs):

```bash
pip install -e . --group dev
```

(requires pip ≥ 25.1 for `--group`; alternatively, with [uv](https://docs.astral.sh/uv/): `uv sync --group dev`)

### Requirements

- Python 3.11+ (tested on 3.11, 3.12, 3.13, 3.14)
- Core dependencies (installed automatically): `numpy`, `networkx`, `geopandas`, `osmnx`, `pandas`, `requests`, `shapely`, `pooch`
- No separate GDAL install is required — `geopandas`/`pyogrio` ship prebuilt GDAL bindings via wheels.

## Quick start

```python
from multimodalpy import get_network

# Build a multimodal network for a given area, combining OSM street data
# with one or more GTFS feeds
manifest = get_network.main(
    "Valencia",
    modes=["walking", "bus", "train"],
    output_path="output",
    output_file_type="geojson",
)
```

<!-- TODO: verify this matches the actual public API/signature exactly. -->

## Documentation

Full documentation, tutorials, and API reference: <https://multimodalpy.readthedocs.io> <!-- TODO: confirm this is live -->

## Getting help & contributing

- Found a bug or have a feature request? [Open an issue](https://github.com/aivilor/multimodalpy/issues).
- Want to contribute? See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and the contribution process. <!-- TODO: create this file -->
- This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). <!-- TODO: create this file -->
- Licensed under the [MIT License](LICENSE).

## Citation

If you use `multimodalpy` in your research, please cite it as: <!-- TODO: add citation / DOI once available, e.g. via Zenodo -->

```
<citation placeholder — author(s), year, title, version, DOI>
```