"""Thin GraphQL client for the Linear API.

Only `requests` + raw GraphQL are used here - never the Linear MCP tools.
"""

import time
from typing import Any, Callable, Dict, List, Optional

import requests

from .config import LINEAR_GRAPHQL_URL, get_api_key

MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.5


class LinearGraphQLError(RuntimeError):
    def __init__(self, errors: List[Dict[str, Any]]):
        self.errors = errors
        super().__init__(f"Linear GraphQL error(s): {errors}")


class LinearClient:
    """Session-based client that authenticates with a Linear personal API key."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_api_key()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": self.api_key,
                "Content-Type": "application/json",
            }
        )

    def query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(1, MAX_RETRIES + 1):
            response = self._session.post(LINEAR_GRAPHQL_URL, json=payload, timeout=30)

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_RETRIES:
                    response.raise_for_status()
                time.sleep(backoff)
                backoff *= 2
                continue

            response.raise_for_status()
            body = response.json()

            if "errors" in body and body["errors"]:
                if attempt < MAX_RETRIES and _is_retryable(body["errors"]):
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise LinearGraphQLError(body["errors"])

            return body["data"]

        raise RuntimeError("Unreachable: exceeded retry loop without returning")

    def paginate(
        self,
        query: str,
        variables: Dict[str, Any],
        path: List[str],
        page_size: int = 50,
        cursor_var: str = "after",
    ) -> List[Dict[str, Any]]:
        """Follow a GraphQL connection's pageInfo until exhausted.

        `path` is the list of keys to walk from the response `data` down to
        the connection object (e.g. ["team", "activeCycle", "issues"]).
        The query must accept `$first` and `$<cursor_var>` variables and
        select `nodes { ... } pageInfo { hasNextPage endCursor }` on that
        connection.
        """
        all_nodes: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        merged_variables = dict(variables)
        merged_variables.setdefault("first", page_size)

        while True:
            if cursor:
                merged_variables[cursor_var] = cursor
            data = self.query(query, merged_variables)

            connection = data
            for key in path:
                if connection is None:
                    break
                connection = connection[key]

            if connection is None:
                break

            all_nodes.extend(connection["nodes"])
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

        return all_nodes


def _is_retryable(errors: List[Dict[str, Any]]) -> bool:
    for error in errors:
        message = str(error.get("message", "")).lower()
        if "rate limit" in message or "timeout" in message:
            return True
    return False


def chunked(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
