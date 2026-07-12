"""Shared HTTP session with retry/backoff for ArcGIS REST fetches."""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_session(retries: int = 5, backoff: float = 1.0) -> requests.Session:
    """Session that retries transient failures (429/5xx, connection resets)."""
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = "cabin-land-geodb/0.1 (personal research)"
    return session
