"""On-disk JSON cache for the dashboard, keyed by age rather than a fixed TTL.

Unlike the in-process 120s cache in `server.py` (which resets whenever the
process restarts), this cache is persisted to disk so the dashboard only
re-queries Linear when the last successful pull is older than
`max_age_seconds` (default 24h) - across server restarts too. A manual
refresh always bypasses the age check.
"""

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .config import CACHE_DIR

DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _read(key: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write(key: str, payload: Dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(key)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w") as f:
        json.dump(payload, f, default=str)
    tmp_path.replace(path)


def get_or_refresh(
    key: str,
    fetch_fn: Callable[[], Any],
    force: bool = False,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    version: Optional[int] = None,
) -> Dict[str, Any]:
    """Return `{fetchedAt, stale, data}` for `key`, refetching if needed.

    A refetch happens when `force=True`, no cache exists yet, the cached
    entry is older than `max_age_seconds`, or (if `version` is given) the
    cached entry was written by an older `version`. That last case matters
    because this cache has no schema of its own - if `fetch_fn`'s return
    shape changes (e.g. a new field is added), a same-day cache hit would
    otherwise silently keep serving the old shape for up to `max_age_seconds`
    with no error. Callers should bump `version` whenever they change what
    `fetch_fn` returns.
    """
    cached = _read(key)
    now = time.time()
    version_ok = version is None or (cached is not None and cached.get("version") == version)

    if not force and cached and version_ok and (now - cached["fetchedAt"]) < max_age_seconds:
        return {**cached, "stale": False}

    data = fetch_fn()
    payload: Dict[str, Any] = {"fetchedAt": now, "data": data}
    if version is not None:
        payload["version"] = version
    _write(key, payload)
    return {**payload, "stale": False}


def peek(key: str) -> Optional[Dict[str, Any]]:
    """Return the cached entry (if any) without triggering a refetch."""
    return _read(key)
