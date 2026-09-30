# multimodalpy

Easy creation of multimodal transport networks from OpenStreetMap and GTFS data.

`multimodalpy` builds routable, multimodal transport network graphs (walking,
cycling, driving, and public transit) by combining OpenStreetMap street data
with GTFS public transit feeds, and exports them as GeoDataFrames,
GeoPackages, shapefiles, or graph files ready for network analysis.

## Installation

```bash
pip install multimodalpy
```

See the [README](https://github.com/aivilor/multimodalpy#installation) for
detailed installation options, including installing from source.

## Quick start

```python
from multimodalpy import get_network

manifest = get_network.main(
    "Valencia",
    modes=["walking", "bus", "train"],
    output_path="output",
    output_file_type="geojson",
)
```

```{toctree}
:maxdepth: 2
:caption: Contents

api
```
