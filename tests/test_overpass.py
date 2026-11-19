"""Tests for the Overpass access (``_overpass``): errors, retries and settings.

No request leaves the machine: osmnx's download function, or ``requests``
itself, is replaced by fakes that fail the way the public Overpass server
does.
"""

from __future__ import annotations

import osmnx as ox
import pytest
import requests
from osmnx._errors import InsufficientResponseError, ResponseStatusCodeError
from shapely.geometry import box

from multimodalpy import _osm, _overpass
from multimodalpy.network import OSMDownloadError

# What overpass-api.de answers when it is overloaded (October-November 2026).
AT_CAPACITY = (
    "'overpass-api.de' responded: 502 Bad Gateway # 502 Gateway Timeout\n\n"
    "This Overpass API server is currently at capacity. All query workers are "
    "busy\nand the request queue is full.\n\n## What to do\n\nRetry your "
    "request using exponential backoff...\n\n## For heavy or automated use\n\n"
    "This public endpoint is intended for browser-based and human-operated tools."
)


def failing(exc):
    def download():
        raise exc

    return download


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
def test_server_at_capacity_gives_a_short_clear_error():
    with pytest.raises(OSMDownloadError) as info:
        _overpass.call_overpass(
            failing(ResponseStatusCodeError(AT_CAPACITY)), mode="walking"
        )
    error = info.value
    message = str(error)
    assert "walking network" in message
    assert "HTTP 502 (it is at capacity)" in message
    assert "overpass-api.de" in message
    assert "try again in a few minutes" in message
    assert "What to do" not in message  # not the server's whole page
    assert error.mode == "walking"
    assert error.status_code == 502
    assert error.transient
    assert isinstance(error.__cause__, ResponseStatusCodeError)


@pytest.mark.parametrize(
    ("exc", "reason", "transient"),
    [
        (ResponseStatusCodeError("'h' responded: 429 Too Many"), "HTTP 429", True),
        (ResponseStatusCodeError("'h' responded: 400 Bad Request"), "HTTP 400", False),
        (requests.exceptions.ReadTimeout("read timed out"), "did not answer", True),
        (requests.exceptions.ConnectionError("refused"), "connection", True),
        (
            InsufficientResponseError("Found no graph nodes within the polygon."),
            "Found no graph nodes",
            False,
        ),
    ],
)
def test_request_failures_are_explained(exc, reason, transient):
    with pytest.raises(OSMDownloadError, match=reason) as info:
        _overpass.call_overpass(failing(exc), mode="bike")
    assert info.value.transient is transient
    assert ("try again" in str(info.value)) is transient


def test_other_errors_propagate_unchanged():
    with pytest.raises(ZeroDivisionError):
        _overpass.call_overpass(failing(ZeroDivisionError()), mode="bike")


def test_success_returns_the_result():
    assert _overpass.call_overpass(lambda: 42, mode="bike") == 42


def test_download_osm_raises_the_clear_error(monkeypatch):
    def graph_from_polygon(*args, **kwargs):
        raise ResponseStatusCodeError(AT_CAPACITY)

    monkeypatch.setattr(ox, "graph_from_polygon", graph_from_polygon)
    import geopandas as gpd

    boundary = gpd.GeoDataFrame(geometry=[box(-0.4, 39.4, -0.3, 39.5)], crs=4326)
    with pytest.raises(OSMDownloadError, match="bike network"):
        _osm.download_osm(boundary, modes=["bicicleta"])
