"""Client of Spain's National Access Point (NAP): API key and GTFS downloads.

Private module. Everything specific to the NAP lives here, so other
countries' data providers can be added as modules of their own.
"""

from __future__ import annotations

import difflib
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import requests

from ._boundaries import NAP_PROVINCE_IDS
from ._text import normalize_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------
def find_dotenv(start: str | Path | None = None) -> Path | None:
    """First ``.env`` file in ``start`` (default: the working folder) or a parent.

    Looking in the parent folders too means a ``.env`` at the root of a
    project is found when Python runs from a subfolder (e.g. a notebook
    in ``notebooks/``), not only from the folder that holds it.
    """
    folder = Path(start if start is not None else Path.cwd()).resolve()
    for candidate in (folder, *folder.parents):
        path = candidate / ".env"
        if path.is_file():
            return path
    return None


def load_dotenv(env_path: str | Path | None = None) -> Path | None:
    """Load variables from a ``.env`` file into ``os.environ``.

    ``env_path`` defaults to the first ``.env`` found by
    :func:`find_dotenv`. Variables already set in the environment are not
    overwritten. Returns the file that was read, or ``None`` if there was
    none.
    """
    path = Path(env_path) if env_path is not None else find_dotenv()
    if path is None or not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return path


def resolve_nap_api_key(api_key: str | None = None) -> str | None:
    """Return the NAP API key to use, or ``None`` if there is none.

    In order of priority: the ``api_key`` argument (``--api-key`` in the
    command line), the ``NAP_API_KEY`` environment variable, and a
    ``NAP_API_KEY=...`` line in a ``.env`` file (see :func:`find_dotenv`).
    """
    if api_key:
        return api_key
    if not os.environ.get("NAP_API_KEY"):
        load_dotenv()
    return os.environ.get("NAP_API_KEY") or None


def missing_nap_key_message() -> str:
    """Explain how to provide the NAP key, naming the folders searched."""
    return (
        "Missing NAP API key (only needed for bus/train). Pass api_key=... "
        "(--api-key in the command line), set the NAP_API_KEY environment "
        "variable, or put NAP_API_KEY=... in a .env file in "
        f"{Path.cwd()} or one of its parent folders."
    )


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
NAP_BASE_URL = "https://nap.transportes.gob.es/api/v2"


class NAPAuthenticationError(ValueError):
    """The NAP API rejected the key (HTTP 401): missing, invalid or expired."""


def _nap_get(url: str, headers: dict, timeout: int = 60) -> requests.Response:
    """GET request to the NAP API with clear handling of network/auth errors.

    Explicitly handles:
    - ``ConnectionError``: no internet connection or the NAP server is down.
    - ``Timeout``: the NAP API didn't respond in time.
    - HTTP 401: missing, invalid or expired API key (plain text).
    - HTTP 500: internal NAP server error.
    """
    import requests

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise ConnectionError(
            f"Could not connect to the NAP API ({url}). "
            "Check your internet connection or try again later."
        ) from None
    except requests.exceptions.Timeout:
        raise TimeoutError(
            f"The NAP API did not respond within {timeout}s ({url}). "
            "The server may be overloaded; try again later."
        ) from None

    if resp.status_code == 401:
        raise NAPAuthenticationError(
            f"Authentication error from the NAP API: {resp.text.strip()}. "
            "Check that NAP_API_KEY is valid and hasn't expired."
        )

    if resp.status_code == 500:
        try:
            server_msg = resp.json().get("message", resp.text)
        except Exception:
            server_msg = resp.text

        raise RuntimeError(
            f"Internal NAP server error (HTTP 500) at {url}: {server_msg}. "
            "Try again later."
        )

    return resp


# ---------------------------------------------------------------------------
# GTFS feeds
# ---------------------------------------------------------------------------
def download_gtfs_nap_zips(
    area_name: str,
    output_dir: str | Path,
    *,
    modes: Iterable[int] = (1, 2),
    api_key: str | None = None,
    base_url: str = NAP_BASE_URL,
    fail_fast: bool = False,
    province_code: int | None = None,
) -> list[Path]:
    """Download the NAP GTFS zip files for the study area's province.

    NAP mode ids: 1 bus, 2 rail, 3 maritime, 4 air.
    The key is taken from ``api_key``, the ``NAP_API_KEY`` environment
    variable or a ``.env`` file (see :func:`resolve_nap_api_key`). A key
    the NAP rejects raises :class:`NAPAuthenticationError`, also when it
    happens while downloading the files.

    ``province_code`` is the INE province code (1-52), which is exactly
    the region id understood by the API. This is the reliable path, and
    the one used by ``network.download_network``, which gets it from the
    boundary. If not given, ``area_name`` is compared against the names of
    the 52 province entries in the ``/region`` listing; never against the
    municipality or region (CCAA) entries, which return 404 or another
    province's data (see ``NAP_PROVINCE_IDS``).

    If the province has no published dataset, the API responds with 404:
    that isn't an error, so it's logged and an empty list is returned
    instead of raising an exception.
    """
    import pandas as pd
    import requests

    api_key = resolve_nap_api_key(api_key)
    if not api_key:
        raise ValueError(missing_nap_key_message())

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    headers = {"ApiKey": api_key}

    if province_code is not None and int(province_code) not in NAP_PROVINCE_IDS:
        raise ValueError(
            f"province_code '{province_code}' is not valid: must be an "
            "INE province code between 1 and 52."
        )

    region_id = int(province_code) if province_code is not None else None
    if region_id is None:
        regions_response = _nap_get(f"{base_url}/region", headers=headers)
        regions_response.raise_for_status()
        regions = regions_response.json().get("data", [])
        if not regions:
            raise ValueError("The NAP API did not return any region.")

        # Only province entries (``tipo == "0"``) have an id that the
        # datasets endpoint knows how to interpret.
        provinces = [item for item in regions if str(item.get("tipo")) == "0"]
        if not provinces:
            raise ValueError("The NAP API did not return any province region (tipo=0).")

        target = normalize_name(area_name)
        best_region = max(
            provinces,
            key=lambda item: difflib.SequenceMatcher(
                None, target, normalize_name(item.get("nombre", ""))
            ).ratio(),
        )
        region_id = int(best_region["id"])
        logger.warning(
            "Could not infer the province for '%s' from the boundaries "
            "file; using province '%s' based on name similarity, which "
            "may not be correct.",
            area_name,
            best_region.get("nombre"),
        )

    datasets_response = _nap_get(
        f"{base_url}/conjunto-dato/region/{region_id}",
        headers=headers,
    )

    if datasets_response.status_code == 404:
        try:
            error_data = datasets_response.json()
            error_message = error_data.get("message", "")
        except Exception:
            error_message = ""

        # Do NOT translate this string: it's matched against the NAP API's
        # own (Spanish-language) error response text, not our own message.
        if "no se ha encontrado ningun conjunto de datos" in normalize_name(
            error_message
        ):
            logger.info(
                "NAP has no published datasets for region %s; no GTFS "
                "will be downloaded.",
                region_id,
            )
            return []

        # Unexpected 404 (broken endpoint, etc.)
        logger.error(
            "Unexpected 404 at %s: %s",
            datasets_response.url,
            error_message or datasets_response.text,
        )

    datasets_response.raise_for_status()
    datasets = datasets_response.json().get("data", [])
    mode_set = {int(mode) for mode in modes}

    downloaded: list[Path] = []
    report_rows: list[dict[str, object]] = []
    for dataset in datasets:
        dataset_modes = {int(item["id"]) for item in dataset.get("tiposTransporte", [])}
        if mode_set and dataset_modes.isdisjoint(mode_set):
            continue

        for file_info in dataset.get("ficheros", []):
            file_id = file_info.get("id")
            if file_id is None:
                continue
            row = {
                "dataset_id": dataset.get("id"),
                "dataset_name": dataset.get("nombre"),
                "file_id": file_id,
                "status": "pending",
                "file_name": None,
                "path": None,
                "error": None,
            }
            try:
                # Through _nap_get, like the other NAP calls, so a rejected
                # key raises a clear NAPAuthenticationError instead of
                # becoming one more "failed" row in the report.
                link_response = _nap_get(
                    f"{base_url}/fichero/{file_id}/descarga", headers=headers
                )
                link_response.raise_for_status()
                payload = link_response.json().get("data", {})
                download_url = payload.get("enlaceDescarga")
                file_name = payload.get("nombreFichero") or f"nap_gtfs_{file_id}.zip"
                row["file_name"] = file_name
                if not download_url:
                    raise ValueError("The NAP response did not include enlaceDescarga")

                zip_response = requests.get(download_url, timeout=120)
                zip_response.raise_for_status()
                path = output_dir / file_name
                path.write_bytes(zip_response.content)
                downloaded.append(path)
                row["status"] = "downloaded"
                row["path"] = str(path)
            except Exception as exc:  # noqa: BLE001 - logged and execution continues
                row["status"] = "failed"
                row["error"] = str(exc)
                # A rejected key fails every file the same way: stop and
                # report it, as with fail_fast.
                if fail_fast or isinstance(exc, NAPAuthenticationError):
                    report_rows.append(row)
                    pd.DataFrame(report_rows).to_csv(
                        output_dir / "nap_download_report.csv", index=False
                    )
                    raise
            report_rows.append(row)

    if report_rows:
        pd.DataFrame(report_rows).to_csv(
            output_dir / "nap_download_report.csv", index=False
        )

    return downloaded
