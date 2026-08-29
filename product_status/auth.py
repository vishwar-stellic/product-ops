"""Google Sign-In, restricted to a single email domain (e.g. Stellic accounts).

Session strategy: a signed, stateless cookie rather than a server-side
session store. This runs on Vercel, where nothing survives between
invocations (see `config.CACHE_DIR`'s serverless caveat) unless it's
explicitly persisted (Blob) - a session store would mean either a Blob
round-trip on every request or pinning a hard dependency on Blob just to
log in. A cookie the browser holds and resends avoids both: it carries the
user's email/name/picture plus an expiry, base64url-encoded and signed with
an HMAC-SHA256 keyed by `SESSION_SECRET`. Forging one without that secret
means guessing the HMAC; tampering with the payload invalidates the
signature. No server-side session storage, no extra dependency (deliberately
implemented with stdlib `hmac`/`hashlib` rather than pulling in e.g.
`itsdangerous`, matching this project's preference for raw `requests` over
SDKs elsewhere - see `notion_oauth.py`'s docstring).

Setup (one-time):
1. Go to https://console.cloud.google.com/apis/credentials (create/pick a
   project - if it belongs to the Stellic Google Workspace, set the OAuth
   consent screen's "User type" to Internal so only @<domain> accounts can
   even reach the consent screen, on top of the domain check this module
   does server-side either way).
2. Create an OAuth 2.0 Client ID of type "Web application".
3. Add an authorized redirect URI for every host this runs on, e.g.
   http://localhost:8008/auth/google/callback for local dev and
   https://<your-vercel-domain>/auth/google/callback for production - must
   match `GOOGLE_OAUTH_REDIRECT_URI` (or the default) exactly.
4. Copy the Client ID/Secret into `.env` as `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET`. Set `SESSION_SECRET` to any long random
   string, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   `ALLOWED_EMAIL_DOMAIN` defaults to "stellic.com" - override if needed.

If `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` aren't set, login is
treated as unconfigured and every route stays open - this keeps local dev
working out of the box for anyone who hasn't set up Google credentials
(see `server.py`'s auth middleware).
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

DEFAULT_REDIRECT_URI = "http://localhost:8008/auth/google/callback"
DEFAULT_ALLOWED_DOMAIN = "stellic.com"

SESSION_COOKIE_NAME = "product_ops_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days - after this, sign in again.

# CSRF protection for the OAuth `state` param - in-memory is fine, same
# reasoning as `notion_oauth.py`'s `_PENDING_STATES`: a single flow
# completes within seconds and doesn't need to survive a cold start.
_PENDING_STATES: Dict[str, float] = {}
_STATE_TTL_SECONDS = 600


class AuthError(RuntimeError):
    """Safe to show directly to the user (e.g. via the login page)."""


def is_configured() -> bool:
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def _client_id() -> str:
    value = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not value:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID is not set - create an OAuth Client ID at "
            "https://console.cloud.google.com/apis/credentials and add it to .env "
            "(see auth.py's module docstring for the full setup)."
        )
    return value


def _client_secret() -> str:
    value = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not value:
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_SECRET is not set (see GOOGLE_OAUTH_CLIENT_ID error for setup).")
    return value


def _session_secret() -> bytes:
    value = os.environ.get("SESSION_SECRET")
    if not value:
        raise RuntimeError(
            "SESSION_SECRET is not set - pick any long random string (e.g. "
            '`python -c "import secrets; print(secrets.token_urlsafe(32))"`) and add it to .env.'
        )
    return value.encode()


def allowed_domain() -> str:
    return os.environ.get("ALLOWED_EMAIL_DOMAIN", DEFAULT_ALLOWED_DOMAIN).lower().lstrip("@")


def partner_insights_allowed_emails() -> set:
    """Comma-separated allowlist for the "Partner Insights" tab
    (`PARTNER_INSIGHTS_ALLOWED_EMAILS`) - a second, narrower gate on top of
    the domain-wide Google sign-in above, since that tab surfaces
    per-partner scoring most of the team doesn't need to see."""
    raw = os.environ.get("PARTNER_INSIGHTS_ALLOWED_EMAILS", "")
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


def is_partner_insights_allowed(email: Optional[str]) -> bool:
    if not email:
        return False
    return email.strip().lower() in partner_insights_allowed_emails()


def redirect_uri() -> str:
    return os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI)


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
        "scope": "openid email profile",
        "state": state,
        # Hints Google's account chooser toward the right domain - a UX
        # nicety, not enforcement (a signed-in user could still pick a
        # different account). The real check is in `exchange_code` below.
        "hd": allowed_domain(),
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str) -> Dict[str, Any]:
    """Exchange an OAuth `code` for the signed-in user's profile
    (`email`/`name`/`picture`), enforcing the allowed email domain. Raises
    `AuthError` (safe to show the user) on failure or a disallowed domain."""
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not response.ok:
        raise AuthError("Google sign-in failed - please try again.")
    id_token = response.json().get("id_token")
    if not id_token:
        raise AuthError("Google didn't return an id_token - please try again.")

    # Verified by Google itself (signature, audience, expiry) rather than
    # decoding the JWT locally - avoids pulling in a JWT/crypto library just
    # to check a handful of claims.
    info_response = requests.get(GOOGLE_TOKENINFO_URL, params={"id_token": id_token}, timeout=30)
    if not info_response.ok:
        raise AuthError("Google rejected the sign-in token - please try again.")
    claims = info_response.json()

    if claims.get("aud") != _client_id():
        raise AuthError("Sign-in token audience mismatch - please try again.")

    email = claims.get("email")
    if not email or str(claims.get("email_verified")).lower() != "true":
        raise AuthError("That Google account has no verified email.")

    domain = email.rsplit("@", 1)[-1].lower()
    if domain != allowed_domain():
        raise AuthError(f'Only "@{allowed_domain()}" accounts can sign in - signed in as {email}.')

    return {"email": email, "name": claims.get("name") or email, "picture": claims.get("picture")}


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(body: str) -> str:
    return _b64encode(hmac.new(_session_secret(), body.encode(), hashlib.sha256).digest())


def create_session_cookie(profile: Dict[str, Any]) -> str:
    payload = {**profile, "exp": time.time() + SESSION_TTL_SECONDS}
    body = _b64encode(json.dumps(payload).encode())
    return f"{body}.{_sign(body)}"


def verify_session_cookie(cookie_value: Optional[str]) -> Optional[Dict[str, Any]]:
    """The signed-in user's profile if `cookie_value` is a valid, unexpired
    session cookie - `None` otherwise (missing, tampered, expired, or
    signed with a since-rotated `SESSION_SECRET`)."""
    if not cookie_value or "." not in cookie_value:
        return None
    body, signature = cookie_value.rsplit(".", 1)
    try:
        expected = _sign(body)
    except RuntimeError:
        return None
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload
