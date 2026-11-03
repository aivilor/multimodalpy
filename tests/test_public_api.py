"""The public API: four modules, and the main functions from the package itself."""

from __future__ import annotations

import importlib
import inspect
import subprocess
import sys
from importlib.metadata import entry_points

import pytest

import multimodalpy

PUBLIC_MODULES = {"analysis", "area", "io", "network"}
SHORTCUTS = {
    "download_network": "network",
    "find_area": "area",
    "read_matrix": "io",
    "read_graph": "io",
    "isochrones": "analysis",
    "shortest_path": "analysis",
}
#: Modules of earlier versions that no longer exist.
REMOVED_MODULES = {"get_area", "get_network", "process_osm", "process_gtfs"}


def test_all_lists_only_the_public_api():
    assert set(multimodalpy.__all__) == (
        PUBLIC_MODULES | set(SHORTCUTS) | {"__version__"}
    )


@pytest.mark.parametrize("name", sorted(PUBLIC_MODULES))
def test_public_modules_are_modules(name):
    assert inspect.ismodule(getattr(multimodalpy, name))


@pytest.mark.parametrize(("name", "module"), sorted(SHORTCUTS.items()))
def test_shortcuts_are_the_functions_of_their_module(name, module):
    function = getattr(multimodalpy, name)
    assert callable(function)
    assert function is getattr(getattr(multimodalpy, module), name)


@pytest.mark.parametrize("name", sorted(PUBLIC_MODULES))
def test_public_modules_only_document_their_all(name):
    module = getattr(multimodalpy, name)
    assert module.__all__
    assert all(hasattr(module, item) for item in module.__all__)
    assert not any(item.startswith("_") for item in module.__all__)


@pytest.mark.parametrize("name", sorted(REMOVED_MODULES))
def test_modules_of_earlier_versions_are_gone(name):
    assert not hasattr(multimodalpy, name)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"multimodalpy.{name}")


def test_star_import_brings_only_the_public_api():
    namespace: dict[str, object] = {}
    exec("from multimodalpy import *", namespace)
    names = {name for name in namespace if name != "__builtins__"}
    assert names == set(multimodalpy.__all__)


def test_importing_the_package_does_not_load_the_heavy_dependencies():
    # Run in a fresh interpreter: other tests have already imported them here.
    code = (
        "import sys, multimodalpy; "
        "print(sorted({'numpy', 'pandas', 'scipy'} & set(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_command_line_entry_point_is_registered():
    scripts = entry_points(group="console_scripts")
    (script,) = [entry for entry in scripts if entry.name == "multimodalpy"]
    assert script.value == "multimodalpy._cli:main"


def test_python_dash_m_runs_the_command_line():
    result = subprocess.run(
        [sys.executable, "-m", "multimodalpy", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "usage: multimodalpy" in result.stdout
    assert "--formats" in result.stdout
