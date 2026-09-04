"""Thin REST client for the Vitally API (customer success platform - health
scores, NPS, CSM/AE ownership, etc.) - used by `partner_identity.py` to pull
in a `healthScore` per partner for the Partner Insights tab's "Vitally"
column.

Auth: HTTP Basic, with the REST API secret key as the username and an empty
password, base64-encoded into the `Authorization` header - see
https://docs.vitally.io/en/articles/9880649-rest-api-overview. Only
`requests` + the raw REST API are used here, no Vitally SDK/MCP.

Confirmed against live data: `https://rest.vitally.io/resources/accounts`
works with no workspace subdomain needed for this account (some Vitally
docs show a `https://<subdomain>.rest.vitally.io` form - both resolved to
the same data when spot-checked, so the bare host is used here for
simplicity). Pagination is cursor-based: each page's `next` value gets
passed back as the `from` query param on the following request, until
`atEnd` is true or `next` is absent.

Each Account's `externalId` (e.g. "fsu", "uc", "virginia") turned out to be
the exact same short partner code Intercom calls `company_id` and Linear
Customers carry in `externalIds` - see `partner_identity.py`'s module
docstring - so accounts cross-reference into the existing partner registry
with the same matching logic already used for Linear.
"""

import base64
import os
import time
from typing import Any, Dict, Iterator, Optional

import requests

VITALLY_BASE_URL = "https://rest.vitally.io/resources"

MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.5


def is_configured() -> bool:
    return bool(os.environ.get("VITALLY_ACCESS_TOKEN"))


class VitallyClient:
    def __init__(self, access_token: Optional[str] = None):
        self.access_token = access_token or self._env_token()
        auth_header = "Basic " + base64.b64encode(f"{self.access_token}:".encode()).decode()
        self._session = requests.Session()
        self._session.headers.update({"Authorization": auth_header})

    @staticmethod
    def _env_token() -> str:
        token = os.environ.get("VITALLY_ACCESS_TOKEN")
        if not token:
            raise RuntimeError(
                "VITALLY_ACCESS_TOKEN is not set - see .env.example. Needed for the "
                "Partner Insights tab's Vitally health score column."
            )
        return token

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        backoff = INITIAL_BACKOFF_SECONDS
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._session.get(f"{VITALLY_BASE_URL}{path}", params=params, timeout=30)
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code == 429 and attempt < MAX_RETRIES:
                    last_error = RuntimeError(f"Vitally API rate limited (attempt {attempt})")
                elif not response.ok:
                    raise RuntimeError(f"Vitally API error {response.status_code}: {response.text[:500]}")
                else:
                    return response.json()
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2
        raise RuntimeError(f"Vitally API: exhausted retries ({last_error})")

    def list_accounts(self, page_size: int = 100) -> Iterator[Dict[str, Any]]:
        """Every Account, paginated - see module docstring for the cursor
        scheme. Each account carries `id`, `externalId`, `name`,
        `healthScore`, `npsScore`, `mrr`, `csmId`, `segments`, a `traits`
        bag of imported CRM/Intercom/CSV fields, and more."""
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"limit": page_size}
            if cursor:
                params["from"] = cursor
            payload = self._get("/accounts", params=params)
            for account in payload.get("results") or []:
                yield account
            if payload.get("atEnd") or not payload.get("next"):
                break
            cursor = payload["next"]

    def list_account_conversations(self, account_id: str, page_size: int = 100) -> Iterator[Dict[str, Any]]:
        """Every Conversation for one Account (Vitally's internal `id`, not
        `externalId`), ordered by `updatedAt` desc per the API docs -
        https://docs.vitally.io/en/articles/9880665-rest-api-conversations.
        Each entry here is a *summary* (subject/source/status/etc, no
        `messages` array) - call `get_conversation` for the full thread."""
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"limit": page_size}
            if cursor:
                params["from"] = cursor
            payload = self._get(f"/accounts/{account_id}/conversations", params=params)
            for conversation in payload.get("results") or []:
                yield conversation
            if payload.get("atEnd") or not payload.get("next"):
                break
            cursor = payload["next"]

    def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Full Conversation including its `messages` array - each Message
        has `type` ("inbound" from a `user`, or "outbound" from an
        `admin`), `timestamp`, `message` (HTML), and `from`/`to`/`cc`/`bcc`
        Participant objects."""
        return self._get(f"/conversations/{conversation_id}")
