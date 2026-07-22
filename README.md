# multimodalpy

Librería de Python para **descargar y estandarizar redes multimodales de transporte**
de un municipio español, a partir de datos abiertos de **OpenStreetMap (OSM)** y
**GTFS oficial del NAP** (Punto de Acceso Nacional).

Autora: Aida Villalba Ortiz — `aivilor@upv.es`

## Estructura

```
multimodalpy/
├── setup.py
├── README.md
├── requirements.txt
└── multimodalpy/
    ├── __init__.py
    ├── process_osm.py     # Estandarización de datos OSM (creación de intersecciones).
    ├── process_gtfs.py    # Estandarización de datos GTFS. Limpieza de datos de bus + tren.
    ├── get_area.py        # Descarga desde APIs OSM y GTFS + llamada a process_osm / process_gtfs.
    ├── get_network.py     # Descarga en local según indicaciones del usuario. Llamada del main().
    └── data/
        └── recintos_municipales_inspire_peninbal_etrs89/   # límites municipales incluidos
```

## Instalación

```bash
python -m pip install -r requirements.txt
python -m pip install -e .
```

## Uso básico

La función principal es `main()`. Solo el nombre del municipio es obligatorio;
el resto de parámetros tienen valores por defecto razonables.

```python
from multimodalpy import main

# Descarga la red caminable de Valencia en GeoJSON (WGS84) dentro de ./salida
main("Valencia", modes=["caminable", "bicicleta"], output_path="salida")
```

Ejemplo multimodal proyectando a UTM 30N y exportando GeoPackage:

```python
from multimodalpy import main

main(
    area_name="Sotes",
    modes=["caminable", "coche", "bus_interurbano", "cercanias"],
    output_path="salida_sotes",
    crs="EPSG:25830",
    output_file_type="geopackage",
)
```

Desde la línea de comandos:

```bash
multimodalpy --area "Valencia" --modes caminable coche --output salida --crs EPSG:25830
# o, sin instalar el entry point:
python -m multimodalpy.get_network --area "Valencia" --modes caminable coche
```

## Parámetros de `main()`

| Parámetro          | Tipo   | Por defecto     | Descripción |
|--------------------|--------|-----------------|-------------|
| `area_name`        | `str`  | *(obligatorio)* | Nombre del municipio sobre el que se realiza la descarga. |
| `modes`            | `list` | `["caminable"]` | Modos de transporte a descargar (ver abajo). |
| `output_path`      | `Path` | `"output"`      | Carpeta local de descarga. |
| `boundaries_path`  | `Path` | *(recintos incluidos)* | **Shapefile (`.shp`) o GeoJSON (`.geojson`)** con las geometrías a seleccionar. Si se omite, se usa el shapefile de recintos municipales del paquete. |
| `crs`              | `str`  | `"EPSG:4326"`   | CRS de salida. Por defecto WGS84 (universal). Indicar otro (p. ej. `"EPSG:25830"`) para proyectar la red. |
| `output_file_type` | `str`  | `"geojson"`     | Formato de descarga: `geopackage`, `geojson`, `shapefile` o `networkx`. |

Extras opcionales (no imprescindibles): `area_code`, `travel_speed_kmh`,
`clean_gtfs` (limpieza de bus/tren), `api_key`.

### Modos de transporte

| Modo             | Fuente | Backend |
|------------------|--------|---------|
| `caminable`      | OSM    | osmnx `walk` |
| `bicicleta`      | OSM    | osmnx `bike` |
| `coche`          | OSM    | osmnx `drive` |
| `bus_urbano`     | GTFS   | NAP modo 1 (bus) |
| `bus_interurbano`| GTFS   | NAP modo 1 (bus) |
| `metro`          | GTFS   | NAP modo 2 (ferroviario) |
| `cercanias`      | GTFS   | NAP modo 2 (ferroviario) |

## Clave del NAP (solo transporte público)

Los modos GTFS (`bus_*`, `metro`, `cercanias`) requieren una clave de la API del
NAP. Defínela en la variable de entorno `NAP_API_KEY` o en un fichero `.env`
(ver `.env.example`). Los modos OSM (`caminable`, `bicicleta`, `coche`) **no**
necesitan clave.

```bash
export NAP_API_KEY="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

## Salida

Según `output_file_type`:

- **geojson**: un fichero `.geojson` por capa (`osm_<modo>_nodes`, `osm_<modo>_edges`,
  `gtfs_<dataset>_nodes_stops`, etc.) más `study_area_boundary`.
- **shapefile**: un `.shp` por capa.
- **geopackage**: un único `.gpkg` con una capa por elemento.
- **networkx**: los grafos OSM y GTFS como JSON node-link de NetworkX, más el
  límite del área en GeoJSON de apoyo.
