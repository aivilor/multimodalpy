"""The public API exposes get_network and get_area, not the processing modules."""

from __future__ import annotations
import importlib
import subprocess
import sys
import pytest
import multimodalpy

PUBLIC_MODULES = {"get_network", "get_area"}
PRIVATE_MODULES = {"process_osm", "process_gtfs"}


def test_all_lists_only_the_public_api():
    assert set(multimodalpy.__all__) == {
        "main",
        "read_graph_json",
        "read_matrix",
        "find_area_boundary",
        "get_network",
        "get_area",
        "__version__",
    }


@pytest.mark.parametrize("name", sorted(PUBLIC_MODULES))
def test_public_modules_are_exposed(name):
    assert hasattr(multimodalpy, name)


@pytest.mark.parametrize("name", sorted(PRIVATE_MODULES))
def test_processing_modules_are_not_exposed(name):
    assert not hasattr(multimodalpy, name)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"multimodalpy.{name}")


def test_star_import_brings_no_processing_module():
    namespace: dict[str, object] = {}
    exec("from multimodalpy import *", namespace)
    assert PRIVATE_MODULES.isdisjoint(namespace)
    assert not any(name.startswith("_process") for name in namespace)


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