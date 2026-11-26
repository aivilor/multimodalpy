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

# The real /status check, before conftest's no_overpass_status replaces it.
REAL_PAUSE = _overpass._overpass_pause

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


@pytest.mark.parametrize("status", [403, 406])
def test_refused_request_is_explained_and_not_retried(sleeps, status):
    exc = ResponseStatusCodeError(f"'overpass-api.de' responded: {status} Refused")
    with pytest.raises(OSMDownloadError) as info:
        _overpass.call_overpass(failing(exc), mode="walking")
    error = info.value
    message = str(error)
    assert f"refused the request (HTTP {status})" in message
    assert "block" in message
    assert "overpass_url=" in message
    assert _overpass.PUBLIC_INSTANCES_URL in message
    assert "attempts" not in message
    assert "try again in a few minutes" not in message
    assert error.status_code == status
    assert not error.transient
    assert sleeps == []  # a retry would count against the client


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


@pytest.mark.parametrize("status", [403, 406])
def test_hook_stops_at_a_refused_answer(status):
    with pytest.raises(_overpass._ServerRefused) as info:
        _overpass._check_response(response(status, b"<html>Not Acceptable</html>"))
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

    # Only osmnx's queries here (the /status check has its own tests).

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


def test_osmnx_query_refused_is_not_retried(monkeypatch, sleeps):
    """Through osmnx and requests: a 406 to the query stops after one request."""
    posts = []

    def send(self, request, **kwargs):
        posts.append(request.url)
        resp = response(406, b"<html><body>406 Not Acceptable</body></html>")
        resp.url = request.url
        resp.request = request
        resp.reason = "Not Acceptable"
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(ox.settings, "use_cache", False)
    monkeypatch.setattr(ox.settings, "overpass_rate_limit", False)
    monkeypatch.setattr(_overpass_dns(), "_config_dns", lambda url: None)
    polygon = box(-0.38, 39.46, -0.37, 39.47)

    with pytest.raises(OSMDownloadError, match=r"refused the request \(HTTP 406\)"):
        _overpass.call_overpass(
            lambda: ox.graph_from_polygon(polygon, network_type="walk"),
            mode="walking",
        )
    assert len(posts) == 1
    assert sleeps == []


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
# The /status check that replaces osmnx's
# ---------------------------------------------------------------------------
STATUS_HEAD = (
    "Connected as: 1\nCurrent time: 2026-11-25T12:00:00Z\nAnnounced endpoint: none\n"
)
RUNNING = "Currently running queries (pid, space limit, time limit, start time):\n"
FREE = STATUS_HEAD + "Rate limit: 2\n2 slots available now.\n" + RUNNING
WAIT = (
    STATUS_HEAD
    + "Rate limit: 2\n"
    + "Slot available after: 2026-11-25T12:00:30Z, in 30 seconds.\n"
    + "Slot available after: 2026-11-25T12:00:07Z, in 7 seconds.\n"
    + RUNNING
)
# A server without a rate limit lists no slots: osmnx read its fifth line,
# "Currently running queries", as "busy" and asked again every 5 s.
NO_LIMIT = STATUS_HEAD + "Rate limit: 0\n" + RUNNING


@pytest.mark.parametrize(
    ("text", "pause"),
    [
        (FREE, 0),
        (WAIT, 7),
        (NO_LIMIT, 0),
        (
            "Slot available after: 2026-11-25T11:59:59Z, in -1 seconds.\n",
            1,
        ),
        (
            "0 slots available now.\nSlot available after: x, in 4 seconds.\n",
            4,
        ),
        ("<html><body>Not an Overpass status page</body></html>", 0),
        ("", 0),
    ],
)
def test_status_page_is_read(text, pause):
    assert _overpass._status_pause(text) == pause


@pytest.fixture
def server(monkeypatch):
    """Fake an Overpass server behind osmnx; records the requests made.

    ``status`` and ``query`` are what ``/status`` and ``/interpreter``
    answer: ``(http_status, body)`` or an exception to raise.
    """
    import osmnx._overpass as ox_overpass

    monkeypatch.setattr(_overpass, "_overpass_pause", REAL_PAUSE)
    state = {
        "status": (200, FREE),
        "query": (200, '{"elements": []}'),
        "requests": [],
        "osmnx_sleeps": [],
    }

    def send(self, request, **kwargs):
        state["requests"].append((request.method, request.url.split("?")[0]))
        answer = state["status" if request.url.endswith("/status") else "query"]
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        resp = response(status, body.encode())
        resp.url = request.url
        resp.request = request
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(ox.settings, "use_cache", False)
    monkeypatch.setattr(ox.settings, "overpass_rate_limit", True)
    monkeypatch.setattr(_overpass_dns(), "_config_dns", lambda url: None)
    monkeypatch.setattr(ox_overpass.time, "sleep", state["osmnx_sleeps"].append)
    return state


def query():
    """One Overpass query through osmnx, as the network downloads make it."""
    from collections import OrderedDict

    import osmnx._overpass as ox_overpass

    return ox_overpass._overpass_request(OrderedDict(data="[out:json];way(1);out;"))


def run(mode="walking"):
    return _overpass.call_overpass(query, mode=mode)


def test_free_slot_means_no_wait(server, sleeps):
    assert run() == {"elements": []}
    assert server["requests"] == [
        ("GET", "https://overpass-api.de/api/status"),
        ("POST", "https://overpass-api.de/api/interpreter"),
    ]
    assert server["osmnx_sleeps"] == [0]
    assert sleeps == []


def test_waits_only_what_the_server_asks(server, caplog):
    server["status"] = (200, WAIT)
    with caplog.at_level("INFO", logger="multimodalpy"):
        run()
    assert server["osmnx_sleeps"] == [7]
    assert "waiting 7 s for a free slot" in caplog.text


def test_server_without_rate_limit_is_not_polled(server):
    server["status"] = (200, NO_LIMIT)
    run()
    assert [method for method, _ in server["requests"]] == ["GET", "POST"]
    assert server["osmnx_sleeps"] == [0]


@pytest.mark.parametrize("status", [404, 500])
def test_unreadable_status_page_means_no_wait(server, status):
    server["status"] = (status, "<html>no status here</html>")
    run()
    assert server["osmnx_sleeps"] == [0]  # osmnx would wait 60 s here


def test_refused_status_page_stops_at_once(server, sleeps):
    server["status"] = (406, "<html>Not Acceptable</html>")
    with pytest.raises(OSMDownloadError, match="refused the request") as info:
        run()
    assert server["requests"] == [("GET", "https://overpass-api.de/api/status")]
    assert server["osmnx_sleeps"] == []
    assert sleeps == []
    assert info.value.status_code == 406


def test_busy_status_page_is_retried(server, sleeps):
    server["status"] = (502, AT_CAPACITY)
    with pytest.raises(OSMDownloadError, match="HTTP 502"):
        run()
    assert sleeps == [10, 20, 40, 80]
    assert all(method == "GET" for method, _ in server["requests"])


def test_unreachable_server_is_retried_without_osmnx_pausing(server, sleeps):
    server["status"] = requests.exceptions.ConnectionError("Connection refused")
    with pytest.raises(OSMDownloadError, match="connection to the server failed"):
        run()
    assert server["requests"] == [("GET", "https://overpass-api.de/api/status")] * 5
    assert server["osmnx_sleeps"] == []  # osmnx never waited its 60 s
    assert sleeps == [10, 20, 40, 80]


def test_cached_answer_makes_no_request(server, monkeypatch):
    monkeypatch.setattr(
        _overpass_dns(), "_retrieve_from_cache", lambda url: {"elements": ["cached"]}
    )
    assert run() == {"elements": ["cached"]}
    assert server["requests"] == []


def test_rate_limit_off_skips_the_status_page(server, monkeypatch):
    monkeypatch.setattr(ox.settings, "overpass_rate_limit", False)
    run()
    assert [method for method, _ in server["requests"]] == ["POST"]


def test_status_check_uses_the_settings(server, monkeypatch):
    monkeypatch.setattr(ox.settings, "overpass_url", "https://overpass.example/api")
    run()
    assert server["requests"][0] == ("GET", "https://overpass.example/api/status")


def test_osmnx_status_check_is_replaced_only_while_downloading():
    import osmnx._overpass as ox_overpass

    original = ox_overpass._get_overpass_pause
    seen = []
    _overpass.call_overpass(
        lambda: seen.append(ox_overpass._get_overpass_pause), mode="bike"
    )
    assert seen == [_overpass._pause_hook]
    assert ox_overpass._get_overpass_pause is original
