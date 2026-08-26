"""Vercel Blob-backed persistent cache, for hosts where the local
filesystem doesn't survive between requests.

`cache.py`'s on-disk cache works fine locally, but Vercel serverless
functions ship a read-only filesystem (writes are only allowed under
`/tmp`, which is wiped on cold start and not shared across scaled-out
instances - see `config.py`), so nothing written there actually persists.
Vercel Blob storage does persist across invocations/deployments, so
`cache.py` delegates to this module instead whenever `BLOB_READ_WRITE_TOKEN`
is set (added via the Vercel dashboard's Storage tab -> Blob, then
`vercel env pull` locally if you also want to exercise this path outside
Vercel).

This talks to Vercel Blob's HTTP API directly with `requests` rather than
the official `vercel` Python SDK (`pip install vercel`), because that SDK
requires Python >= 3.10 and this project targets 3.9. There's no published
plain-REST reference for Vercel Blob (the docs only cover the JS/Python
SDKs), so the endpoints/headers below are reverse-engineered from the
open-source JS SDK (https://github.com/vercel/storage/tree/main/packages/blob,
specifically `api.ts`/`put-helpers.ts`/`helpers.ts`) - if Vercel changes
this contract, that's the place to check against.

Every operation here is best-effort: on any failure (misconfigured token,
network error, Vercel Blob outage, etc.) reads return `None` and writes are
silently skipped, with a warning printed to stderr. A broken cache should
degrade to "always refetch from Linear" (slower, but correct), never to a
crashed dashboard.
"""

import json
import os
import sys
from typing import Any, Dict, Optional

import requests

_BLOB_API_BASE = "https://vercel.com/api/blob"
# Sent so the Blob API knows which response format/behavior to use - mirrors
# the JS SDK's hardcoded `BLOB_API_VERSION` (see module docstring).
_BLOB_API_VERSION = "12"
_REQUEST_TIMEOUT_SECONDS = 15


def _token() -> Optional[str]:
    return os.environ.get("BLOB_READ_WRITE_TOKEN") or None


def is_configured() -> bool:
    return _token() is not None


def _store_id(token: str) -> str:
    # Read-write tokens are formatted "vercel_blob_rw_<storeId>_<secret>".
    parts = token.split("_")
    return parts[3] if len(parts) > 3 else ""


def _warn(action: str, exc: BaseException) -> None:
    print(f"[blob_cache] {action} failed, falling back: {exc}", file=sys.stderr)


def read_json(pathname: str) -> Optional[Dict[str, Any]]:
    """Fetch and parse a JSON blob. Returns `None` if it doesn't exist, Blob
    isn't configured, or the request fails for any reason - callers should
    treat that the same as a cache miss."""
    token = _token()
    if not token:
        return None
    try:
        response = requests.get(
            f"https://{_store_id(token)}.private.blob.vercel-storage.com/{pathname}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        _warn(f"reading {pathname}", exc)
        return None


def write_json(pathname: str, payload: Dict[str, Any]) -> None:
    """Best-effort write - see module docstring for why failures here are
    swallowed rather than raised."""
    token = _token()
    if not token:
        return
    try:
        response = requests.put(
            f"{_BLOB_API_BASE}/",
            params={"pathname": pathname},
            data=json.dumps(payload, default=str).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "x-api-version": _BLOB_API_VERSION,
                "x-vercel-blob-store-id": _store_id(token),
                "x-vercel-blob-access": "private",
                "x-content-type": "application/json",
                # Deterministic pathname (no random suffix) so repeated
                # writes to the same cache key overwrite in place.
                "x-add-random-suffix": "0",
                "x-allow-overwrite": "1",
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _warn(f"writing {pathname}", exc)
