multimodalpy/
├── LICENSE
├── README.md
├── ARCHITECTURE.md
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── CITATION.cff
├── CHANGELOG.md
├── pyproject.toml
├── .gitignore
├── .readthedocs.yaml
│
├── .github/
│   └── workflows/
│       ├── tests.yml          # pytest on Python 3.11-3.13, on push/PR
│       ├── docs.yml           # Sphinx build check, on push/PR
│       └── publish.yml        # build + publish to (Test)PyPI on release
│
├── src/
│   └── multimodalpy/
│       ├── __init__.py        # package shortcuts (download_network, find_area, ...)
│       ├── __main__.py        # python -m multimodalpy
│       ├── network.py         # public: download_network, download_osm, download_gtfs
│       ├── area.py            # public: find_area
│       ├── io.py              # public: read_matrix, read_graph
│       ├── analysis/          # public: isochrones, shortest_path (one private file each)
│       │   ├── __init__.py
│       │   ├── _common.py     # shared: network loading, snapping, cutoffs, Dijkstra
│       │   ├── _isochrones.py
│       │   └── _shortest_path.py
│       ├── _cli.py            # the `multimodalpy` command
│       ├── _boundaries.py     # boundaries file (pooch), study-area lookup, provinces
│       ├── _nap.py            # NAP client: API key, .env, GTFS feed downloads
│       ├── _osm.py            # OSM download and standardization per mode
│       ├── _gtfs.py           # GTFS download and standardization
│       ├── _matrix.py         # CSR matrix (matrix.npz) and network loading
│       ├── _writers.py        # writing every output format
│       └── _text.py           # name normalisation, edit distances, slugs
│
├── tests/                     # one file per module, plus end-to-end output checks
│   ├── test_public_api.py
│   ├── test_network.py
│   ├── test_area.py
│   ├── test_area_cities.py
│   ├── test_multipart_area.py
│   ├── test_gtfs.py
│   ├── test_matrix.py
│   ├── test_network_loading.py
│   ├── test_isochrones.py
│   ├── test_shortest_path.py
│   ├── test_output_format.py
│   └── test_topology_crs.py
│
└── docs/
    ├── conf.py                # Sphinx config (pydata-sphinx-theme)
    ├── index.md
    ├── api.md                 # autosummary-generated API reference
    └── requirements.txt       # pinned docs deps for Read the Docs' isolated build

Notes
-----
- No `data/` folder is tracked in the repository. The default study-area
  boundaries are downloaded on demand via `pooch.retrieve()` from a GitHub
  release asset and cached locally (see `_boundaries.py`); this keeps the repo
  small and avoids committing large binary geospatial files to git history.
- `dist/` (build artifacts from `python -m build`) is git-ignored; each
  release version's wheel/sdist filenames are permanently reserved by
  PyPI/TestPyPI once uploaded, so they must never be committed.
- Documentation is built by Read the Docs from `.readthedocs.yaml` +
  `docs/`; `docs.yml` in CI is a build-check only (fails PRs that break the
  Sphinx build), it does not deploy.