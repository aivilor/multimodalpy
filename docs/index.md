# multimodalpy

Transport networks for Spanish municipalities, from OpenStreetMap and official
GTFS data.

Give `multimodalpy` the name of a municipality in Spain and it downloads and
standardizes its transport networks, one layer per mode: walking, cycling and
driving from OpenStreetMap, and bus and train from the official GTFS feeds of
Spain's National Access Point (NAP). The layers share the same column names and
are written as GeoJSON, Shapefile, GeoPackage, NetworkX graphs or sparse
matrices for shortest-path calculations.

Only municipalities in Spain are supported (not the Canary Islands with the
default boundaries file). Each mode is exported as its own network; linking them
into a single multimodal network is planned but not implemented yet.

## Installation

```bash
pip install multimodalpy
```

See the [README](https://github.com/aivilor/multimodalpy#installation) for
installing from source and for development.

## Quick start

```python
from multimodalpy import get_network

manifest = get_network.main(
    "Valencia",
    modes=["walking", "bike", "driving"],
    output_path="output",
    output_file_type="geojson",
)
```

Bus and train (`modes=["bus", "train"]`) also need a free NAP API key, passed
as `api_key=`, in the `NAP_API_KEY` environment variable or in a `.env` file in
the working folder or a parent folder; see the
[README](https://github.com/aivilor/multimodalpy#bus-and-train-nap-api-key).
Every output column is described in
[DATA_SCHEMA.md](https://github.com/aivilor/multimodalpy/blob/main/DATA_SCHEMA.md).

```{toctree}
:maxdepth: 2
:caption: Contents

api
```