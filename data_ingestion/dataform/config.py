"""Shared config/HTTP helpers for the dataform loaders."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

COURTLISTENER_API_TOKEN = os.environ.get("COURTLISTENER_API_TOKEN")
GOVINFO_API_KEY = os.environ.get("GOVINFO_API_KEY")
CONGRESS_API_KEY = os.environ.get("CONGRESS_API_KEY")

USER_AGENT = "amicus-dataform/0.1 (research/hackathon data normalization)"


class MissingAPIKeyError(RuntimeError):
    pass


def require_key(value: Optional[str], env_var: str, source_name: str) -> str:
    if not value:
        raise MissingAPIKeyError(
            f"{source_name} requires {env_var} to be set (see dataform/.env.example). "
            f"Get a free key at https://api.data.gov/signup/ and add it to your .env file."
        )
    return value


# Cross-thread, per-host courtesy rate limit. A single lane self-pacing its own
# requests isn't enough once multiple lanes hit the same host concurrently
# (parallel_load.py runs up to 6 lanes against courtlistener.com at once) --
# confirmed live 2026-08-16: 6 concurrent lanes still tripped 429s at a 0.6s/host
# interval, so courtlistener.com's *anonymous* limit (no COURTLISTENER_API_TOKEN
# set) needs a materially slower pace than the other three hosts, which held up
# fine at 0.6s. The lock is only held to read/update the timestamp, never across
# the sleep, so unrelated hosts never block on each other.
_RATE_LIMIT_LOCK = threading.Lock()
_LAST_REQUEST_AT: Dict[str, float] = {}
DEFAULT_MIN_INTERVAL = 0.6
HOST_MIN_INTERVAL = {
    "www.courtlistener.com": 3.5,  # anonymous access; raise back down once COURTLISTENER_API_TOKEN is set
}


def _throttle(url: str) -> None:
    host = urlparse(url).netloc
    min_interval = HOST_MIN_INTERVAL.get(host, DEFAULT_MIN_INTERVAL)
    while True:
        with _RATE_LIMIT_LOCK:
            now = time.time()
            wait = min_interval - (now - _LAST_REQUEST_AT.get(host, 0.0))
            if wait <= 0:
                _LAST_REQUEST_AT[host] = now
                return
        time.sleep(wait)


def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    max_retries: int = 5,
    timeout: int = 20,
) -> Dict[str, Any]:
    """GET a JSON endpoint with cross-thread per-host throttling and retry on 429/5xx."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    for attempt in range(max_retries):
        _throttle(url)
        try:
            resp = requests.get(url, params=params, headers=req_headers, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_status = resp.status_code
                time.sleep(min(2 ** attempt, 8))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:  # pragma: no cover
            last_exc = exc
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(
        f"GET {url} failed after {max_retries} attempts: "
        f"{last_exc or (f'HTTP {last_status}' if last_status else 'unknown error')}"
    )
