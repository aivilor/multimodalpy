multimodalpy/
├── LICENSE
├── README.md
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── CITATION.cff
├── CHANGELOG.md
├── pyproject.toml
├── .gitignore
│
├── .github/
│   └── workflows/
│       ├── tests.yml
│       ├── docs.yml
│       └── publish.yml
│
├── src/
│   └── multimodalpy/
│       ├── __init__.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── network.py          # base graph structure
│       │   ├── nodes.py
│       │   └── edges.py
│       ├── modes/
│       │   ├── __init__.py
│       │   ├── walk.py
│       │   ├── bike.py
│       │   ├── transit.py
│       │   └── car.py
│       ├── multimodal/
│       │   ├── __init__.py
│       │   ├── graph_builder.py    # combines mode-specific graphs
│       │   ├── transfers.py        # transfer penalties/logic between modes
│       │   └── weighting.py        # cost functions per mode
│       ├── routing/
│       │   ├── __init__.py
│       │   ├── shortest_path.py
│       │   └── algorithms.py       # Dijkstra/A* adapted for multimodal
│       ├── io/
│       │   ├── __init__.py
│       │   ├── readers.py          # GTFS, OSM, shapefile ingestion
│       │   └── writers.py
│       ├── viz/
│       │   ├── __init__.py
│       │   └── plotting.py
│       └── utils/
│           ├── __init__.py
│           └── validation.py
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # shared fixtures (sample networks, etc.)
│   ├── test_core/
│   ├── test_modes/
│   ├── test_multimodal/
│   ├── test_routing/
│   └── data/                       # small sample datasets for tests
│
├── docs/
│   ├── conf.py                     # Sphinx config (furo theme)
│   ├── index.rst
│   ├── api/                        # autodoc-generated API reference
│   ├── examples/
│   │   └── case_study.ipynb        # your multimodal case study
│   └── _static/
│
└── examples/
    └── quickstart.py                # standalone runnable example
