"""Overpass API access through osmnx: clear errors when a download fails.

osmnx downloads the OSM networks from an Overpass API server (by default
the public ``overpass-api.de``). When that server is at capacity it answers
with an HTML page instead of JSON, and osmnx fails with a ``JSONDecodeError``
or a ``ResponseStatusCodeError`` carrying the whole page. Here those
failures become an :class:`OSMDownloadError` that says which network, which
server and what to do.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urlparse

T = TypeVar("T")


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


# osmnx keeps its exceptions in a private module (osmnx._errors), so they
# are recognised by name.
_STATUS_IN_MESSAGE = re.compile(r"responded:?\s+(\d{3})")


def _http_failure(status: int, text: str) -> _Failure:
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
    message = (
        f"Could not download the {mode} network from OpenStreetMap: "
        f"{failure.reason} (Overpass server {server}{tries})."
    )
    if failure.transient:
        message += (
            " The public Overpass server is often busy: try again in a few "
            "minutes (the parts already downloaded are cached)."
        )
    return OSMDownloadError(
        message,
        mode=mode,
        server=server,
        status_code=failure.status_code,
        transient=failure.transient,
    )


def call_overpass(download: Callable[[], T], *, mode: str) -> T:
    """Run ``download`` (an osmnx call), turning its failures into errors.

    Request failures (HTTP errors, a page that isn't JSON, timeouts, lost
    connections) raise :class:`OSMDownloadError`, with the original
    exception as its cause. Any other exception propagates unchanged.
    """
    try:
        return download()
    except Exception as exc:
        failure = _classify(exc)
        if failure is None:
            raise
        raise _error(failure, mode, attempts=1) from exc
