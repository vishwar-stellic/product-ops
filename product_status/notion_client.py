"""Thin REST client for the Notion API.

This runs from the FastAPI backend (triggered by the dashboard's "Publish to
Notion" button), not from an agent session, so it can't use Notion MCP
tools - it talks to https://api.notion.com directly, authenticated with
either an OAuth access token (see `notion_oauth.py`, the default - no
Notion "workspace owner" permission required) or a static internal
integration token (`NOTION_API_KEY`).
"""

import re
import time
from typing import Any, Dict, List, Optional

import requests

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notion only allows nesting a block's children up to two levels deep in a
# single API call, so deeper trees (e.g. team -> section -> content) are
# built by appending one level at a time and recursing - see
# `create_nested_blocks`.
MAX_CHILDREN_PER_REQUEST = 100


class NotionError(RuntimeError):
    pass


def extract_page_id(url_or_id: str) -> str:
    """Notion page/URL IDs are a 32-char hex string; format as a dashed
    UUID (the API also accepts the undashed form, but this is safer)."""
    match = re.search(r"([0-9a-fA-F]{32})(?:[/?#]|$)", url_or_id)
    raw = (match.group(1) if match else url_or_id).replace("-", "")
    if len(raw) != 32:
        raise ValueError(f"Couldn't find a Notion page ID in {url_or_id!r}")
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def _strip_internal(block: Dict[str, Any]) -> Dict[str, Any]:
    """Drop our own `_children` marker key before sending to Notion - real
    nested children (e.g. a table's rows) live under the block's own type
    key (e.g. `table.children`) and are left untouched."""
    return {k: v for k, v in block.items() if k != "_children"}


class NotionClient:
    def __init__(self, api_key: Optional[str] = None):
        if api_key is None:
            from .notion_oauth import resolve_access_token

            api_key = resolve_access_token()
        self.api_key = api_key
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{NOTION_API_BASE}{path}"
        for attempt in range(5):
            response = self._session.request(method, url, json=json_body, timeout=30)
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After", "1"))
                time.sleep(wait)
                continue
            if not response.ok:
                raise NotionError(f"Notion API error {response.status_code}: {response.text}")
            # Stay comfortably under Notion's ~3 req/s average rate limit.
            time.sleep(0.35)
            return response.json()
        raise NotionError("Notion API rate-limited the request too many times")

    def create_page(self, parent_page_id: str, title: str) -> Dict[str, Any]:
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        }
        return self._request("POST", "/pages", body)

    def append_children(self, block_id: str, children: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for i in range(0, len(children), MAX_CHILDREN_PER_REQUEST):
            chunk = [_strip_internal(c) for c in children[i : i + MAX_CHILDREN_PER_REQUEST]]
            resp = self._request("PATCH", f"/blocks/{block_id}/children", {"children": chunk})
            results.extend(resp["results"])
        return results

    def update_block(self, block_id: str, block: Dict[str, Any]) -> Dict[str, Any]:
        """Overwrite an existing block's content (e.g. to fill in a table-of-
        contents link once we know the target heading's block ID)."""
        body = {k: v for k, v in block.items() if k not in ("type", "_children")}
        return self._request("PATCH", f"/blocks/{block_id}", body)


def create_nested_blocks(
    client: NotionClient, parent_block_id: str, blocks: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Append `blocks` under `parent_block_id`, then recursively append any
    `_children` each block carries under that block's freshly created ID.
    Returns the top-level created blocks (in the same order as `blocks`),
    so callers can look up the real ID of a just-created block (e.g. to
    link to it from elsewhere on the page).

    Notion only allows two levels of nesting per request, so arbitrarily
    deep trees (team toggle -> section toggle -> content) are built with
    one call per level rather than a single deeply-nested payload. Notion
    returns created blocks in the same order they were submitted, so
    `blocks[i]`'s `_children` line up positionally with `created[i]`.
    """
    if not blocks:
        return []
    created = client.append_children(parent_block_id, blocks)
    for input_block, created_block in zip(blocks, created):
        nested = input_block.get("_children")
        if nested:
            create_nested_blocks(client, created_block["id"], nested)
    return created
