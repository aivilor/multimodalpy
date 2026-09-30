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
│       ├── tests.yml          # pytest + mypy + ruff, on push/PR
│       ├── docs.yml           # Sphinx build check, on push/PR
│       └── publish.yml        # build + publish to (Test)PyPI on release
│
├── src/
│   └── multimodalpy/
│       ├── __init__.py
│       ├── get_area.py        # study-area boundary resolution (pooch-fetched
│       │                      # default boundaries; see DATA_SCHEMA.md)
│       ├── get_network.py     # main() entry point: OSM + GTFS -> network
│       ├── process_osm.py     # OSM download/normalization per mode
│       └── process_gtfs.py    # GTFS download/normalization via NAP
│
├── tests/
│   ├── test_get_area_cities.py
│   ├── test_get_network.py
│   └── test_output_format.py
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
  release asset and cached locally (see `get_area.py`); this keeps the repo
  small and avoids committing large binary geospatial files to git history.
- `dist/` (build artifacts from `python -m build`) is git-ignored; each
  release version's wheel/sdist filenames are permanently reserved by
  PyPI/TestPyPI once uploaded, so they must never be committed.
- Documentation is built by Read the Docs from `.readthedocs.yaml` +
  `docs/`; `docs.yml` in CI is a build-check only (fails PRs that break the
  Sphinx build), it does not deploy.