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
def no_overpass_probe(monkeypatch):
    """Never reach a real Overpass server from the tests.

    Before each download attempt ``_overpass`` asks the server's ``/status``
    (see ``_probe_server``); the downloads in the tests are fakes, so that
    request is skipped. The probe's own tests put it back.
    """
    from multimodalpy import _overpass

    monkeypatch.setattr(_overpass, "_probe_server", lambda: None)
