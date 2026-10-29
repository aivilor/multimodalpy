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
