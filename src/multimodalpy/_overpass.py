"""Overpass API access through osmnx: clear errors and retries.

osmnx downloads the OSM networks from an Overpass API server (by default
the public ``overpass-api.de``). When that server is at capacity it answers
with an HTML page instead of JSON, and osmnx fails with a ``JSONDecodeError``
or a ``ResponseStatusCodeError`` carrying the whole page. Here:

- a temporary failure (server busy, timeout, lost connection, a query the
  server cut short) is retried a few times, waiting longer each time
  (``RETRY_WAITS``), as the server itself asks;
- a refusal (HTTP 403 or 406: since April 2026 overpass-api.de blocks
  heavy or automated use for a while) is not retried, since every retry
  counts against the client;
- what still fails becomes an :class:`OSMDownloadError` that says which
  network, which server and what to do;
- :func:`overpass_settings` chooses the server and timeout, identifies the
  requests as multimodalpy's and keeps osmnx's cache in a fixed folder.

osmnx handles some busy answers itself, but not well for this server: it
retries 429 and 504 forever (55 s each time) and returns the partial data
of a query the server cut short, only logging the server's remark. A
requests *response hook* (added to ``osmnx.settings.requests_kwargs`` while
downloading, see :func:`_download_patches`) stops osmnx at those answers,
so the retries here handle them all the same way.

Before each query osmnx also asks the server's ``/status`` how long to
wait, and reads it badly: it waits a fixed 60 s, without saying so,
whenever it gets a page it can't read (a refusal, a server without
``/status``) or no answer at all, and on a server with no rate limit it
can keep asking every 5 s without end. While downloading,
:func:`_overpass_pause` takes the place of that check: it reads the slots
the page announces, waits only what the server asks, and lets a request
failure reach the retries here. It runs only for queries osmnx actually
sends, so an answer already in the cache makes no request at all.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

T = TypeVar("T")

logger = logging.getLogger(__name__)

#: Seconds to wait before each new attempt after a temporary failure: four
#: retries, doubling the wait (2.5 minutes in all, at worst).
RETRY_WAITS: tuple[float, ...] = (10, 20, 40, 80)

# The waits go through this name so the tests can replace them without
# also replacing osmnx's own (short) pauses.
_sleep = time.sleep

# Answers that mean "busy, come back later" (429 too many requests, 502/503
# at capacity or down, 504 gateway timeout).
_BUSY_STATUSES = frozenset({429, 502, 503, 504})

# Answers that mean "not from you": overpass-api.de answers 406 Not
# Acceptable (or 403 Forbidden) to a client it has blocked, usually
# automatically after heavy use; the block lifts by itself some hours later.
_REFUSED_STATUSES = frozenset({403, 406})

#: Where to find another Overpass server.
PUBLIC_INSTANCES_URL = (
    "https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances"
)


class OSMDownloadError(RuntimeError):
    """An OpenStreetMap network could not be downloaded from Overpass.

    Attributes
    ----------
    mode : str
        The network that failed (``"walking"``, ``"bike"`` or ``"driving"``).
    server : str
        Host name of the Overpass server.
    status_code : int or None
        HTTP status the server answered with, if any.
    transient : bool
        Whether the failure is usually temporary (server busy, connection
        lost), so trying again later may work.
    """

    def __init__(
        self,
        message: str,
        *,
        mode: str,
        server: str,
        status_code: int | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.mode = mode
        self.server = server
        self.status_code = status_code
        self.transient = transient


@dataclass(frozen=True)
class _Failure:
    """What went wrong in one request, in a few words."""

    reason: str
    transient: bool
    status_code: int | None = None
    hint: str = ""


class _ServerBusy(Exception):
    """The server answered with a busy status (raised by the response hook)."""

    def __init__(self, status_code: int, text: str) -> None:
        super().__init__(f"Overpass answered HTTP {status_code}")
        self.status_code = status_code
        self.text = text


class _ServerRefused(Exception):
    """The server refused the request, 403 or 406 (raised by the response hook)."""

    def __init__(self, status_code: int, text: str) -> None:
        super().__init__(f"Overpass answered HTTP {status_code}")
        self.status_code = status_code
        self.text = text


class _QueryCut(Exception):
    """The server stopped the query and sent partial data (response hook)."""

    def __init__(self, remark: str) -> None:
        super().__init__(remark)
        self.remark = remark


def _check_response(response: Any, *args: Any, **kwargs: Any) -> Any:
    """Response hook: stop osmnx at a busy or refused answer, or a query cut short.

    Overpass writes its ``remark`` at the end of the JSON, so only the last
    bytes are searched; the JSON is parsed only when there is one.
    """
    if response.status_code in _BUSY_STATUSES:
        raise _ServerBusy(response.status_code, response.text[:2000])
    if response.status_code in _REFUSED_STATUSES:
        raise _ServerRefused(response.status_code, response.text[:2000])
    tail = response.content[-2048:]
    if b'"remark"' in tail and b"runtime error" in tail:
        try:
            remark = str(response.json().get("remark", "")).strip()
        except ValueError:
            remark = "runtime error"
        raise _QueryCut(remark)
    return response


#: Seconds to wait for the ``/status`` answer before each query.
_STATUS_TIMEOUT = 30

#: A wait for a slot at least this long is logged as a warning (shorter
#: ones only as information).
_LONG_PAUSE = 30

# Lines of the ``/status`` page: "2 slots available now." and
# "Slot available after: 2026-11-25T12:00:05Z, in 12 seconds."
_SLOTS_FREE = re.compile(r"^\s*(\d+) slots? available now", re.MULTILINE)
_SLOT_AFTER = re.compile(
    r"^\s*Slot available after:.*?in (-?\d+) seconds?", re.MULTILINE
)


def _status_pause(text: str) -> float:
    """Seconds to wait for a free slot, as a ``/status`` page announces.

    0 when a slot is free now, or when the page says nothing about slots
    (a server with no rate limit, or a page that isn't Overpass's).
    Otherwise the wait until the first slot frees up, at least 1 s.
    """
    free = _SLOTS_FREE.search(text)
    if free and int(free.group(1)) > 0:
        return 0.0
    waits = [int(seconds) for seconds in _SLOT_AFTER.findall(text)]
    if waits:
        return float(max(min(waits), 1))
    return 0.0


def _overpass_pause(base_endpoint: str, *args: Any, **kwargs: Any) -> float:
    """How long osmnx must wait before its next query, from the server's ``/status``.

    Takes the place of osmnx's ``_overpass._get_overpass_pause`` while
    downloading (see :func:`_download_patches`), with its arguments. A
    request that fails (connection refused, DNS failure, timeout) raises,
    and a busy or refused answer is stopped by :func:`_check_response`, so
    the retries of :func:`call_overpass` handle them. Any other answer
    without slots (a server without ``/status``, or with no rate limit)
    means no wait. With ``osmnx.settings.overpass_rate_limit = False``
    the page is not asked for at all, as in osmnx.
    """
    import requests
    from osmnx import settings

    if not settings.overpass_rate_limit:
        return 0.0
    response = requests.get(
        base_endpoint.rstrip("/") + "/status",
        headers={"User-Agent": settings.http_user_agent},
        timeout=min(_STATUS_TIMEOUT, settings.requests_timeout),
        **settings.requests_kwargs,
    )
    if not response.ok:
        return 0.0
    pause = _status_pause(response.text)
    if pause:
        logger.log(
            logging.WARNING if pause >= _LONG_PAUSE else logging.INFO,
            "Overpass server %s: waiting %g s for a free slot, as it asks.",
            overpass_server(),
            pause,
        )
    return pause


def _pause_hook(*args: Any, **kwargs: Any) -> float:
    """Call :func:`_overpass_pause` (looked up each time, so tests can replace it)."""
    return _overpass_pause(*args, **kwargs)


@contextlib.contextmanager
def _download_patches() -> Iterator[None]:
    """Change osmnx's requests while downloading, and restore them afterwards.

    Adds :func:`_check_response` to its requests' response hooks, and puts
    :func:`_overpass_pause` in place of its ``/status`` check (if this
    osmnx has one by that name; 2.0 to 2.2 do).
    """
    from osmnx import _overpass as ox_overpass
    from osmnx import settings

    original = settings.requests_kwargs
    kwargs = dict(original)
    hooks = dict(kwargs.get("hooks") or {})
    existing = hooks.get("response") or []
    if callable(existing):
        existing = [existing]
    hooks["response"] = [*existing, _check_response]
    kwargs["hooks"] = hooks
    settings.requests_kwargs = kwargs
    original_pause = getattr(ox_overpass, "_get_overpass_pause", None)
    if original_pause is not None:
        ox_overpass._get_overpass_pause = _pause_hook
    try:
        yield
    finally:
        settings.requests_kwargs = original
        if original_pause is not None:
            ox_overpass._get_overpass_pause = original_pause


# osmnx keeps its exceptions in a private module (osmnx._errors), so they
# are recognised by name.
_STATUS_IN_MESSAGE = re.compile(r"responded:?\s+(\d{3})")


def _http_failure(status: int, text: str) -> _Failure:
    if status in _REFUSED_STATUSES:
        return _Failure(
            f"the server refused the request (HTTP {status})",
            transient=False,
            status_code=status,
            hint=(
                " The server is refusing requests from this connection: public "
                "Overpass servers block heavy or automated use for a while, and "
                "the block usually lifts by itself after some hours without "
                "requests. Retrying now would not help (each attempt counts "
                "against you): try again later, or use another Overpass server "
                f"with overpass_url=... (public instances: {PUBLIC_INSTANCES_URL})."
            ),
        )
    reason = f"the server answered HTTP {status}"
    if "at capacity" in text:
        reason += " (it is at capacity)"
    elif status == 429:
        reason += " (too many requests)"
    return _Failure(
        reason, transient=status == 429 or status >= 500, status_code=status
    )


def _classify(exc: BaseException) -> _Failure | None:
    """Describe a failed Overpass request, or ``None`` if it isn't one."""
    import requests

    if isinstance(exc, (_ServerBusy, _ServerRefused)):
        return _http_failure(exc.status_code, exc.text)
    if isinstance(exc, _QueryCut):
        return _Failure(
            f"the server stopped the query before the end ({exc.remark})",
            transient=True,
            hint=(
                " If it keeps happening, the area may be too large for one "
                "query: try a smaller area or a longer timeout."
            ),
        )
    name = type(exc).__name__
    text = str(exc)
    if name == "ResponseStatusCodeError":
        match = _STATUS_IN_MESSAGE.search(text)
        if match:
            return _http_failure(int(match.group(1)), text)
        return _Failure("the server answered with an error", transient=True)
    if name == "InsufficientResponseError":
        # e.g. "Found no graph nodes within the requested polygon".
        return _Failure(text.splitlines()[0][:200], transient=False)
    if isinstance(exc, requests.exceptions.JSONDecodeError):
        return _Failure("the server answered with a page that is not data", True)
    if isinstance(exc, requests.exceptions.Timeout):
        return _Failure("the server did not answer in time", transient=True)
    if isinstance(exc, requests.exceptions.RequestException):
        return _Failure("the connection to the server failed", transient=True)
    return None


def overpass_server() -> str:
    """Host name of the Overpass server osmnx is set to use."""
    from osmnx import settings

    return urlparse(settings.overpass_url).hostname or settings.overpass_url


def _error(failure: _Failure, mode: str, attempts: int) -> OSMDownloadError:
    server = overpass_server()
    tries = f", after {attempts} attempts" if attempts > 1 else ""
    # ``mode`` names several networks when they were downloaded together.
    networks = "networks" if " and " in mode else "network"
    message = (
        f"Could not download the {mode} {networks} from OpenStreetMap: "
        f"{failure.reason} (Overpass server {server}{tries})."
    )
    if failure.transient:
        message += (
            " The public Overpass server is often busy: try again in a few "
            "minutes (the parts already downloaded are cached), or use another "
            "Overpass server with overpass_url=..."
        )
    message += failure.hint
    return OSMDownloadError(
        message,
        mode=mode,
        server=server,
        status_code=failure.status_code,
        transient=failure.transient,
    )


def overpass_error(exc: BaseException, mode: str) -> OSMDownloadError | None:
    """Return the :class:`OSMDownloadError` for a failed request, or ``None``.

    For failures raised outside :func:`call_overpass`, e.g. while building a
    network from an answer already downloaded (no data for that network).
    """
    failure = _classify(exc)
    return None if failure is None else _error(failure, mode, attempts=1)


def call_overpass(
    download: Callable[[], T],
    *,
    mode: str,
    waits: Sequence[float] | None = None,
) -> T:
    """Run ``download`` (an osmnx call), retrying temporary failures.

    A temporary failure (server busy, timeout, lost connection, query cut
    short) is retried after each of ``waits`` seconds (``RETRY_WAITS`` by
    default); every retry is logged. What still fails, and any request
    failure that isn't temporary, raises :class:`OSMDownloadError`, with the
    original exception as its cause. Any other exception propagates
    unchanged. osmnx caches each answer it gets in full, so a retry only
    asks again for what was missing.
    """
    waits = RETRY_WAITS if waits is None else tuple(waits)
    attempt = 0
    with _download_patches():
        while True:
            attempt += 1
            try:
                return download()
            except Exception as exc:
                failure = _classify(exc)
                if failure is None:
                    raise
                if not failure.transient or attempt > len(waits):
                    raise _error(failure, mode, attempts=attempt) from exc
                wait = waits[attempt - 1]
                logger.warning(
                    "OpenStreetMap %s network: %s (Overpass server %s); "
                    "trying again in %g s (retry %d of %d).",
                    mode,
                    failure.reason,
                    overpass_server(),
                    wait,
                    attempt,
                    len(waits),
                )
                _sleep(wait)


# ---------------------------------------------------------------------------
# Settings: server, timeout, User-Agent and cache
# ---------------------------------------------------------------------------
_OSMNX_USER_AGENT = "OSMnx Python package"
_OSMNX_CACHE_FOLDERS = {"./cache", "cache"}
_PROJECT_URL = "https://github.com/aivilor/multimodalpy"


def user_agent() -> str:
    """User-Agent of multimodalpy's requests, as Overpass asks clients to set."""
    from . import __version__

    return f"multimodalpy/{__version__} (+{_PROJECT_URL})"


def osm_cache_dir() -> Path:
    """Folder where osmnx keeps the Overpass answers (next to the boundaries)."""
    import pooch

    return Path(pooch.os_cache("multimodalpy")) / "osmnx"


def _base_url(url: str) -> str:
    """Return the ``.../api`` base osmnx expects, from what the user may paste."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"overpass_url must be an http(s) address, e.g. "
            f"'https://overpass-api.de/api'. Received: {url!r}"
        )
    base = url.rstrip("/")
    for suffix in ("/interpreter", "/status"):
        base = base.removesuffix(suffix)
    return base


@contextlib.contextmanager
def overpass_settings(
    overpass_url: str | None = None, timeout: float | None = None
) -> Iterator[None]:
    """Set osmnx up for multimodalpy's downloads, and restore it afterwards.

    - ``overpass_url``: the Overpass server (its ``.../api`` address; a
      ``.../api/interpreter`` address is accepted too). If not given, the
      one in ``osmnx.settings.overpass_url`` is used (``overpass-api.de``
      unless you changed it).
    - ``timeout``: seconds to wait for each answer, also the time limit the
      server is asked to respect. If not given, osmnx's (180 s).
    - The requests carry multimodalpy's User-Agent, unless you set your own
      in ``osmnx.settings.http_user_agent``.
    - osmnx's cache goes to :func:`osm_cache_dir` instead of a ``cache``
      folder in the current directory, unless you chose another folder; a
      rerun from any folder reuses what was downloaded.
    """
    from osmnx import settings

    if overpass_url is not None:
        overpass_url = _base_url(overpass_url)
    if timeout is not None and not timeout > 0:
        raise ValueError(f"osm_timeout must be a positive number of seconds: {timeout}")

    names = ("overpass_url", "requests_timeout", "http_user_agent", "cache_folder")
    saved = {name: getattr(settings, name) for name in names}
    try:
        if overpass_url is not None:
            settings.overpass_url = overpass_url
        if timeout is not None:
            settings.requests_timeout = int(timeout)
        if str(settings.http_user_agent).startswith(_OSMNX_USER_AGENT):
            settings.http_user_agent = user_agent()
        if str(settings.cache_folder) in _OSMNX_CACHE_FOLDERS:
            settings.cache_folder = str(osm_cache_dir())
        yield
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)
