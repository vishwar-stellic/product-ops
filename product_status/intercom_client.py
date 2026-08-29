"""Thin REST client for the Intercom API.

Matches this project's other API clients (`linear_client.py`,
`notion_client.py`): a plain wrapper around `requests` rather than the
`python-intercom` SDK, so there's one dependency-free pattern for every
external API this project talks to.

Setup: create an Access Token in Intercom (Settings -> Integrations ->
Developer Hub -> your app -> Authentication, or a Personal Access Token if
your workspace has one) with at least read access to Conversations, and set
`INTERCOM_ACCESS_TOKEN` in `.env`. Used by `support_report.py` for the
"Support Report" dashboard tab.
"""

import os
import time
from typing import Any, Dict, Iterator, Optional

import requests

INTERCOM_API_BASE = "https://api.intercom.io"
# Pinned so a workspace-side API upgrade can't silently change field shapes
# (e.g. conversation_parts nesting) out from under `support_report.py`.
INTERCOM_API_VERSION = "2.11"


class IntercomError(RuntimeError):
    pass


class IntercomClient:
    def __init__(self, access_token: Optional[str] = None):
        self.access_token = access_token or os.environ.get("INTERCOM_ACCESS_TOKEN")
        if not self.access_token:
            raise RuntimeError(
                "INTERCOM_ACCESS_TOKEN is not set - create an Access Token in Intercom "
                "(Settings -> Integrations -> Developer Hub) with read access to "
                "Conversations, and add it to .env."
            )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Intercom-Version": INTERCOM_API_VERSION,
            }
        )

    def _request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{INTERCOM_API_BASE}{path}"
        for attempt in range(5):
            response = self._session.request(method, url, json=json_body, timeout=30)
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After", "2"))
                time.sleep(wait)
                continue
            if response.status_code >= 500 and attempt < 4:
                time.sleep(1.5 * (attempt + 1))
                continue
            if not response.ok:
                raise IntercomError(f"Intercom API error {response.status_code}: {response.text[:500]}")
            return response.json() if response.content else {}
        raise IntercomError("Intercom API rate-limited/errored too many times")

    def search_conversations(self, query: Dict[str, Any], per_page: int = 150) -> Iterator[Dict[str, Any]]:
        """Yields every conversation matching `query` (Intercom's Search DSL
        - https://developers.intercom.com/docs/references/rest-api/api.intercom.io/conversations/searchconversations),
        transparently paginating via the cursor-based `pages.next.starting_after`."""
        body: Dict[str, Any] = {"query": query, "pagination": {"per_page": per_page}}
        while True:
            data = self._request("POST", "/conversations/search", json_body=body)
            for conversation in data.get("conversations", []):
                yield conversation
            next_page = (data.get("pages") or {}).get("next")
            starting_after = next_page.get("starting_after") if isinstance(next_page, dict) else next_page
            if not starting_after:
                return
            body["pagination"]["starting_after"] = starting_after

    def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Full conversation, including `conversation_parts` - only source of
        the reply history (`search_conversations` results don't include
        parts, just summary `statistics`)."""
        return self._request("GET", f"/conversations/{conversation_id}")

    def search_contacts(self, query: Dict[str, Any], per_page: int = 150) -> Iterator[Dict[str, Any]]:
        """Same shape as `search_conversations` but against `/contacts/search`
        - used to batch-resolve real contact names for admin/bot-authored
        conversations (see `support_report.py:_build_contact_name_map`)."""
        body: Dict[str, Any] = {"query": query, "pagination": {"per_page": per_page}}
        while True:
            data = self._request("POST", "/contacts/search", json_body=body)
            for contact in data.get("data", []):
                yield contact
            next_page = (data.get("pages") or {}).get("next")
            starting_after = next_page.get("starting_after") if isinstance(next_page, dict) else next_page
            if not starting_after:
                return
            body["pagination"]["starting_after"] = starting_after

    def list_companies(self, per_page: int = 60) -> Iterator[Dict[str, Any]]:
        """Yields every company in the workspace - unlike conversations,
        `/companies` pages by plain page number (`pages.next` is a full URL,
        `pages.total_pages` bounds the loop) rather than a cursor."""
        page = 1
        while True:
            data = self._request("GET", f"/companies?per_page={per_page}&page={page}")
            for company in data.get("data", []):
                yield company
            pages = data.get("pages") or {}
            if not pages.get("next") or page >= pages.get("total_pages", page):
                return
            page += 1
