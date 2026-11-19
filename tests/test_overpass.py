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

# The real probe, before conftest's no_overpass_probe replaces it.
REAL_PROBE = _overpass._probe_server

# What overpass-api.de answers when it is overloaded (October-November 2026).
AT_CAPACITY = (
    "'overpass-api.de' responded: 502 Bad Gateway # 502 Gateway Timeout\n\n"
    "This Overpass API server is currently at capacity. All query workers are "
    "busy\nand the request queue is full.\n\n## What to do\n\nRetry your "
    "request using exponential backoff...\n\n## For heavy or automated use\n\n"
    "This public endpoint is intended for browser-based and human-operated tools."
)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record the waits between retries instead of sleeping."""
    waited: list[float] = []
    monkeypatch.setattr(_overpass, "_sleep", waited.append)
    return waited


def failing(exc):
    def download():
        raise exc

    return download


def failing_then(results):
    """A download that raises or returns each of ``results`` in turn."""
    calls = iter(results)

    def download():
        result = next(calls)
        if isinstance(result, BaseException):
            raise result
        return result

    return download


def response(status=200, body=b'{"elements": []}'):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp.encoding = "utf-8"
    return resp


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


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------
def test_busy_server_is_retried_four_times_doubling_the_wait(sleeps, caplog):
    with pytest.raises(OSMDownloadError, match="after 5 attempts") as info:
        _overpass.call_overpass(
            failing(ResponseStatusCodeError(AT_CAPACITY)), mode="walking"
        )
    assert sleeps == [10, 20, 40, 80]
    assert info.value.status_code == 502
    retries = [r for r in caplog.records if "trying again" in r.getMessage()]
    assert len(retries) == 4
    assert "retry 4 of 4" in retries[-1].getMessage()


def test_success_after_a_retry_returns_the_result(sleeps):
    download = failing_then([requests.exceptions.ConnectionError("reset"), "graph"])
    assert _overpass.call_overpass(download, mode="bike") == "graph"
    assert sleeps == [10]


def test_permanent_failure_is_not_retried(sleeps):
    with pytest.raises(OSMDownloadError, match="Found no graph nodes"):
        _overpass.call_overpass(
            failing(InsufficientResponseError("Found no graph nodes")), mode="bike"
        )
    assert sleeps == []


def test_waits_can_be_given(sleeps):
    with pytest.raises(OSMDownloadError, match="after 2 attempts"):
        _overpass.call_overpass(
            failing(requests.exceptions.ReadTimeout()), mode="bike", waits=[1]
        )
    assert sleeps == [1]


# ---------------------------------------------------------------------------
# Response hook
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_hook_stops_at_a_busy_answer(status):
    with pytest.raises(_overpass._ServerBusy) as info:
        _overpass._check_response(response(status, AT_CAPACITY.encode()))
    assert info.value.status_code == status


def test_hook_stops_at_a_query_cut_short():
    body = (
        b'{"version": 0.6, "elements": [{"type": "node", "id": 1}], '
        b'"remark": "runtime error: Query timed out in query at line 1"}'
    )
    with pytest.raises(_overpass._QueryCut, match="Query timed out"):
        _overpass._check_response(response(200, body))


@pytest.mark.parametrize(
    "body",
    [
        b'{"elements": []}',
        b'{"elements": [], "remark": "runtime remark: nothing to report"}',
    ],
)
def test_hook_lets_good_answers_through(body):
    resp = response(200, body)
    assert _overpass._check_response(resp) is resp


def test_query_cut_short_is_retried_and_explained(sleeps):
    with pytest.raises(OSMDownloadError, match="stopped the query") as info:
        _overpass.call_overpass(
            failing(_overpass._QueryCut("runtime error: Query run out of memory")),
            mode="driving",
        )
    assert sleeps == [10, 20, 40, 80]
    assert "too large for one query" in str(info.value)


def test_hook_is_added_only_while_downloading(monkeypatch):
    user_hook = lambda response, *args, **kwargs: response  # noqa: E731
    monkeypatch.setattr(
        ox.settings,
        "requests_kwargs",
        {"verify": True, "hooks": {"response": user_hook}},
    )
    seen = {}

    def download():
        seen.update(ox.settings.requests_kwargs)
        return None

    _overpass.call_overpass(download, mode="bike")
    assert seen["verify"] is True
    assert seen["hooks"]["response"] == [user_hook, _overpass._check_response]
    assert ox.settings.requests_kwargs == {
        "verify": True,
        "hooks": {"response": user_hook},
    }


def test_osmnx_requests_are_stopped_by_the_hook(monkeypatch, sleeps):
    """Through osmnx and requests: a 502 never reaches osmnx's own handling."""
    posts = []

    def send(self, request, **kwargs):
        posts.append(request.url)
        resp = response(502, AT_CAPACITY.encode())
        resp.url = request.url
        resp.request = request
        return resp

    # Only osmnx's own requests here (the probe has its own tests).

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(ox.settings, "use_cache", False)
    monkeypatch.setattr(ox.settings, "overpass_rate_limit", False)
    monkeypatch.setattr(_overpass_dns(), "_config_dns", lambda url: None)
    polygon = box(-0.38, 39.46, -0.37, 39.47)

    with pytest.raises(OSMDownloadError, match="HTTP 502"):
        _overpass.call_overpass(
            lambda: ox.graph_from_polygon(polygon, network_type="walk"),
            mode="walking",
        )
    assert len(posts) == 5  # one per attempt: osmnx did not loop on its own
    assert sleeps == [10, 20, 40, 80]


def _overpass_dns():
    import osmnx._http

    return osmnx._http


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@pytest.fixture
def osmnx_defaults(monkeypatch):
    """osmnx's own defaults, whatever another test left behind."""
    monkeypatch.setattr(ox.settings, "overpass_url", "https://overpass-api.de/api")
    monkeypatch.setattr(ox.settings, "requests_timeout", 180)
    monkeypatch.setattr(
        ox.settings,
        "http_user_agent",
        "OSMnx Python package (https://github.com/gboeing/osmnx)",
    )
    monkeypatch.setattr(ox.settings, "cache_folder", "./cache")


def current():
    s = ox.settings
    return (s.overpass_url, s.requests_timeout, s.http_user_agent, s.cache_folder)


def test_settings_apply_while_downloading_and_are_restored(osmnx_defaults):
    before = current()
    with _overpass.overpass_settings("https://overpass.example/api/", 600):
        url, timeout, agent, cache = current()
    assert url == "https://overpass.example/api"
    assert timeout == 600
    assert agent.startswith("multimodalpy/")
    assert "github.com/aivilor/multimodalpy" in agent
    assert cache == str(_overpass.osm_cache_dir())
    assert current() == before


def test_settings_are_restored_after_an_error(osmnx_defaults):
    before = current()
    with pytest.raises(RuntimeError):
        with _overpass.overpass_settings("https://overpass.example/api", 60):
            raise RuntimeError
    assert current() == before


def test_users_own_osmnx_settings_are_kept(osmnx_defaults, monkeypatch):
    monkeypatch.setattr(ox.settings, "overpass_url", "https://mine.example/api")
    monkeypatch.setattr(ox.settings, "http_user_agent", "my-research-project")
    monkeypatch.setattr(ox.settings, "cache_folder", "/data/osm_cache")
    with _overpass.overpass_settings():
        assert current() == (
            "https://mine.example/api",
            180,
            "my-research-project",
            "/data/osm_cache",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://overpass.example/api",
        "https://overpass.example/api/",
        "https://overpass.example/api/interpreter",
        "https://overpass.example/api/status",
    ],
)
def test_overpass_url_may_be_pasted_with_its_endpoint(osmnx_defaults, url):
    with _overpass.overpass_settings(url):
        assert ox.settings.overpass_url == "https://overpass.example/api"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"overpass_url": "overpass.example/api"}, "http"),
        ({"overpass_url": "ftp://overpass.example/api"}, "http"),
        ({"timeout": 0}, "positive"),
        ({"timeout": -5}, "positive"),
    ],
)
def test_invalid_settings_are_rejected(osmnx_defaults, kwargs, match):
    with pytest.raises(ValueError, match=match):
        with _overpass.overpass_settings(**kwargs):
            pass


def test_error_names_the_server_in_use(osmnx_defaults):
    with _overpass.overpass_settings("https://overpass.example/api"):
        with pytest.raises(OSMDownloadError) as info:
            _overpass.call_overpass(
                failing(requests.exceptions.ConnectionError()), mode="bike", waits=[]
            )
    assert info.value.server == "overpass.example"
    assert "overpass_url=" in str(info.value)


def test_download_osm_uses_the_settings(osmnx_defaults, monkeypatch):
    seen = {}

    def graph_from_polygon(*args, **kwargs):
        seen["url"] = ox.settings.overpass_url
        seen["timeout"] = ox.settings.requests_timeout
        raise InsufficientResponseError("Found no graph nodes")

    monkeypatch.setattr(ox, "graph_from_polygon", graph_from_polygon)
    import geopandas as gpd

    boundary = gpd.GeoDataFrame(geometry=[box(-0.4, 39.4, -0.3, 39.5)], crs=4326)
    with pytest.raises(OSMDownloadError):
        _osm.download_osm(
            boundary,
            modes=["walking"],
            overpass_url="https://overpass.example/api",
            osm_timeout=300,
        )
    assert seen == {"url": "https://overpass.example/api", "timeout": 300}
    assert ox.settings.overpass_url == "https://overpass-api.de/api"


# ---------------------------------------------------------------------------
# Probe: is the server there at all?
# ---------------------------------------------------------------------------
@pytest.fixture
def server(monkeypatch):
    """Fake the Overpass server's answers; records the requests made."""
    monkeypatch.setattr(_overpass, "_probe_server", REAL_PROBE)
    state = {"answer": 200, "requests": []}

    def send(self, request, **kwargs):
        state["requests"].append((request.method, request.url))
        answer = state["answer"]
        if isinstance(answer, BaseException):
            raise answer
        resp = response(answer, b"Connected as: 1\n")
        resp.url = request.url
        resp.request = request
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


def counting_download(calls):
    def download():
        calls.append(1)
        return "graph"

    return download


def test_unreachable_server_is_retried_without_osmnx_pausing(server, sleeps):
    server["answer"] = requests.exceptions.ConnectionError("Connection refused")
    calls: list[int] = []
    with pytest.raises(OSMDownloadError, match="connection to the server failed"):
        _overpass.call_overpass(counting_download(calls), mode="walking")
    assert calls == []  # osmnx was never let in to wait its 60 s
    assert sleeps == [10, 20, 40, 80]
    assert [url for _, url in server["requests"]] == [
        "https://overpass-api.de/api/status"
    ] * 5


def test_busy_status_page_is_retried(server, sleeps):
    server["answer"] = 502
    calls: list[int] = []
    with pytest.raises(OSMDownloadError, match="HTTP 502"):
        _overpass.call_overpass(counting_download(calls), mode="walking")
    assert calls == []


@pytest.mark.parametrize("status", [200, 404])
def test_answering_server_lets_osmnx_go_ahead(server, sleeps, status):
    server["answer"] = status  # 404: a server without /status
    calls: list[int] = []
    assert _overpass.call_overpass(counting_download(calls), mode="bike") == "graph"
    assert calls == [1]
    assert sleeps == []


def test_probe_uses_the_settings(server, monkeypatch):
    monkeypatch.setattr(ox.settings, "overpass_url", "https://overpass.example/api")
    _overpass.call_overpass(lambda: None, mode="bike")
    assert server["requests"] == [("GET", "https://overpass.example/api/status")]
