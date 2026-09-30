"""Sphinx configuration for multimodalpy's documentation."""

from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable for autodoc without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import multimodalpy  # noqa: E402

# -- Project information -----------------------------------------------

project = "multimodalpy"
copyright = "2026, Aida Villalba Ortiz"
author = "Aida Villalba Ortiz"
release = getattr(multimodalpy, "__version__", "0.1.0")
version = release

# -- General configuration -----------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",  # parses numpy-style docstrings
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "myst_parser",
]

# Both .rst and .md source files are supported (MyST for Markdown).
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# Docstrings are numpy-style throughout the codebase.
napoleon_google_docstring = False
napoleon_numpy_docstring = True

autosummary_generate = True
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}
autodoc_typehints = "description"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/docs", None),
    "geopandas": ("https://geopandas.org/en/stable", None),
    "networkx": ("https://networkx.org/documentation/stable", None),
}

# -- Options for HTML output ----------------------------------------------

html_theme = "pydata_sphinx_theme"
html_theme_options = {
    "github_url": "https://github.com/aivilor/multimodalpy",
    "use_edit_page_button": True,
    "show_toc_level": 2,
}
html_context = {
    "github_user": "aivilor",
    "github_repo": "multimodalpy",
    "github_version": "develop",
    "doc_path": "docs",
}
html_static_path = ["_static"]
