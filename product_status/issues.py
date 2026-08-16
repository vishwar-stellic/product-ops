"""Issue-level fetches for a given cycle: scope, rollover, and mid-cycle adds."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from .linear_client import LinearClient, chunked

_CYCLE_ISSUES_QUERY = """
query CycleIssues($cycleId: String!, $first: Int!, $after: String) {
  cycle(id: $cycleId) {
    issues(first: $first, after: $after) {
      nodes {
        id
        identifier
        title
        url
        estimate
        createdAt
        assignee { id name }
        state { id name type }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

_CYCLE_UNCOMPLETED_QUERY = """
query CycleUncompleted($cycleId: String!, $first: Int!, $after: String) {
    cycle(id: $cycleId) {
    uncompletedIssuesUponClose(first: $first, after: $after) {
      nodes {
        id
        identifier
        title
        url
        createdAt
        assignee { id name }
        state { id name type }
        cycle { id number }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

HISTORY_CHUNK_SIZE = 20


def fetch_cycle_issues(client: LinearClient, cycle_id: str) -> List[Dict[str, Any]]:
    """Issues currently assigned to this cycle (Cycle.issues)."""
    return client.paginate(
        _CYCLE_ISSUES_QUERY,
        variables={"cycleId": cycle_id},
        path=["cycle", "issues"],
        page_size=100,
    )


def fetch_uncompleted_upon_close(client: LinearClient, cycle_id: str) -> List[Dict[str, Any]]:
    """Issues that were still open when this (closed) cycle ended.

    These are the candidates for "moved to next sprint" - each node's current
    `cycle` field tells us where the issue actually landed afterwards.
    """
    return client.paginate(
        _CYCLE_UNCOMPLETED_QUERY,
        variables={"cycleId": cycle_id},
        path=["cycle", "uncompletedIssuesUponClose"],
        page_size=100,
    )


def fetch_added_during_cycle(
    client: LinearClient,
    cycle_number: int,
    cycle_starts_at: str,
    issues: List[Dict[str, Any]],
) -> Dict[str, bool]:
    """Determine which issues were added to the cycle after it started.

    For each issue, walk its history for a node where `toCycle.number`
    matches this cycle's number; that node's `updatedAt` is when it entered
    the cycle. If no such node exists, the issue was created directly into
    the cycle, so `issue.createdAt` is used instead. Either way, if that
    timestamp is after the cycle's `startsAt`, the issue counts as added
    mid-cycle rather than being present at kickoff.

    Returns a map of issue identifier -> bool (True if added mid-cycle).
    """
    starts_at = _parse_iso(cycle_starts_at)
    identifiers = [issue["identifier"] for issue in issues]
    created_at_by_id = {
        issue["identifier"]: issue.get("createdAt", cycle_starts_at) for issue in issues
    }

    result: Dict[str, bool] = {}

    for chunk in chunked(identifiers, HISTORY_CHUNK_SIZE):
        aliases = "\n".join(
            f'i{idx}: issue(id: "{_escape(identifier)}") {{ '
            f"identifier createdAt "
            f"history(first: 50) {{ "
            f"nodes {{ updatedAt toCycle {{ number }} }} "
            f"pageInfo {{ hasNextPage }} "
            f"}} }}"
            for idx, identifier in enumerate(chunk)
        )
        data = client.query("{ " + aliases + " }")

        for key, issue_data in data.items():
            if not issue_data:
                continue
            identifier = issue_data["identifier"]
            moved_in_at: Optional[datetime] = None

            for node in issue_data["history"]["nodes"]:
                to_cycle = node.get("toCycle")
                if to_cycle and to_cycle.get("number") == cycle_number:
                    moved_in_at = _parse_iso(node["updatedAt"])
                    break

            if moved_in_at is not None:
                result[identifier] = moved_in_at > starts_at
            else:
                created_at = _parse_iso(created_at_by_id.get(identifier, cycle_starts_at))
                result[identifier] = created_at > starts_at

    return result


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _escape(value: str) -> str:
    return value.replace('"', '\\"')
