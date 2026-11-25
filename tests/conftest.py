"""Shared test setup."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def nap_api_key(monkeypatch):
    """Give every test a dummy NAP key.

    ``main()`` checks the key before downloading anything, so tests that
    run bus/train with fake downloads need one. It also means a real key
    in a developer's ``.env`` is never read by the test suite. Tests about
    a missing key remove it with ``monkeypatch.delenv("NAP_API_KEY")``.
    """
    monkeypatch.setenv("NAP_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def no_overpass_status(monkeypatch):
    """Never reach a real Overpass server from the tests.

    Before each query osmnx asks the server's ``/status`` how long to wait,
    which ``_overpass`` answers with ``_overpass_pause`` while downloading;
    the downloads in the tests are fakes, so that request is skipped (no
    wait). The tests of the ``/status`` check put it back.
    """
    from multimodalpy import _overpass

    monkeypatch.setattr(_overpass, "_overpass_pause", lambda *args, **kwargs: 0.0)
