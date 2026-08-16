"""Notion OAuth ("public connection") flow.

Creating an *internal* integration (Settings -> Connections) requires
"workspace owner" permission in Notion, which not everyone has. A *public
connection* sidesteps that: any Notion member can authorize one for just the
pages they personally have access to, via a standard OAuth consent screen -
no admin/owner role needed on the target workspace.

Setup (one-time, done once by whoever sets this up - not per-user):
1. Go to https://www.notion.so/my-integrations and create a new connection,
   then switch its type to "Public" (or use the Developer Portal's "Public
   connections" section directly, if your account has that UI).
2. Set its redirect URI to match `NOTION_OAUTH_REDIRECT_URI` below
   (default: http://localhost:8008/notion/oauth/callback - Notion rejects
   literal IP addresses like 127.0.0.1 here, but allows "localhost").
3. Copy the connection's OAuth "Client ID" and "Client Secret" into `.env`
   as `NOTION_OAUTH_CLIENT_ID` / `NOTION_OAUTH_CLIENT_SECRET`.
4. Click "Connect to Notion" in the dashboard, and on Notion's consent
   screen pick the "Product Ops Reports" page (or its parent) to share.

Notion rejects literal IP addresses (e.g. `127.0.0.1`) in redirect URIs, but
explicitly allows the `localhost` hostname for local development - use
`http://localhost:<port>/notion/oauth/callback`, not `127.0.0.1`.

The resulting access token is stored on disk (`.cache/notion_oauth_token.json`,
already gitignored) and reused for every publish - each user who wants to
publish from their own machine authorizes once.
"""

import base64
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

from .config import PROJECT_ROOT

NOTION_AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"
# Notion rejects literal IP addresses (e.g. 127.0.0.1) in redirect URIs but
# allows the "localhost" hostname for local development - see module docstring.
DEFAULT_REDIRECT_URI = "http://localhost:8008/notion/oauth/callback"

TOKEN_PATH = PROJECT_ROOT / ".cache" / "notion_oauth_token.json"

# CSRF protection for the OAuth `state` param. In-memory is fine - this is a
# single-process local server and the flow completes within seconds.
_PENDING_STATES: Dict[str, float] = {}
_STATE_TTL_SECONDS = 600


def is_configured() -> bool:
    return bool(os.environ.get("NOTION_OAUTH_CLIENT_ID") and os.environ.get("NOTION_OAUTH_CLIENT_SECRET"))


def _client_id() -> str:
    value = os.environ.get("NOTION_OAUTH_CLIENT_ID")
    if not value:
        raise RuntimeError(
            "NOTION_OAUTH_CLIENT_ID is not set. Create a public connection at "
            "https://www.notion.so/my-integrations and add its Client ID/Secret "
            "to your .env file (see README's \"Publish to Notion\" section)."
        )
    return value


def _client_secret() -> str:
    value = os.environ.get("NOTION_OAUTH_CLIENT_SECRET")
    if not value:
        raise RuntimeError("NOTION_OAUTH_CLIENT_SECRET is not set (see NOTION_OAUTH_CLIENT_ID error for setup).")
    return value


def redirect_uri() -> str:
    return os.environ.get("NOTION_OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI)


def create_state() -> str:
    state = secrets.token_urlsafe(24)
    cutoff = time.time() - _STATE_TTL_SECONDS
    for key in [k for k, created_at in _PENDING_STATES.items() if created_at < cutoff]:
        _PENDING_STATES.pop(key, None)
    _PENDING_STATES[state] = time.time()
    return state


def consume_state(state: str) -> bool:
    """True if `state` was one we issued (and not already used/expired)."""
    return _PENDING_STATES.pop(state, None) is not None


def authorization_url(state: str) -> str:
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "owner": "user",
        "state": state,
    }
    return f"{NOTION_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str) -> Dict[str, Any]:
    """Exchange an OAuth `code` for an access token, and persist it."""
    basic = base64.b64encode(f"{_client_id()}:{_client_secret()}".encode()).decode()
    response = requests.post(
        NOTION_TOKEN_URL,
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json"},
        json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Notion OAuth token exchange failed ({response.status_code}): {response.text}")
    data = response.json()
    _save_token(data)
    return data


def _save_token(data: Dict[str, Any]) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = TOKEN_PATH.with_suffix(".tmp")
    with tmp_path.open("w") as f:
        json.dump(data, f)
    tmp_path.replace(TOKEN_PATH)


def load_token() -> Optional[Dict[str, Any]]:
    if not TOKEN_PATH.exists():
        return None
    try:
        with TOKEN_PATH.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def disconnect() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def status() -> Dict[str, Any]:
    token = load_token()
    if not token:
        return {"connected": False, "oauthConfigured": is_configured()}
    return {
        "connected": True,
        "workspaceName": token.get("workspace_name"),
        "workspaceIcon": token.get("workspace_icon"),
    }


def resolve_access_token() -> str:
    """The token to authenticate Notion API calls with: a connected OAuth
    token if one exists, otherwise a static `NOTION_API_KEY` (for teammates
    who *can* create an internal integration and prefer that instead)."""
    token = load_token()
    if token and token.get("access_token"):
        return token["access_token"]

    env_key = os.environ.get("NOTION_API_KEY")
    if env_key:
        return env_key

    raise RuntimeError(
        'Notion isn\'t connected yet. Click "Connect to Notion" in the dashboard to '
        "authorize via OAuth (no workspace-owner permission needed), or set "
        "NOTION_API_KEY in .env if you have an internal integration token instead."
    )
