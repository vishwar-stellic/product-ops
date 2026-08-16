"""Team and cycle ("sprint") lookups."""

from typing import Any, Dict, List, Optional

from .linear_client import LinearClient

_TEAMS_QUERY = """
query Teams($first: Int!, $after: String) {
  teams(first: $first, after: $after) {
    nodes {
      id
      key
      name
      cyclesEnabled
      activeCycle {
        id
        number
        name
        startsAt
        endsAt
        completedAt
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_TEAM_CYCLES_QUERY = """
query TeamCycles($teamId: String!, $first: Int!, $after: String) {
  team(id: $teamId) {
    cycles(first: $first, after: $after) {
      nodes {
        id
        number
        name
        startsAt
        endsAt
        completedAt
        isPrevious
        isActive
        isNext
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def fetch_teams(
    client: LinearClient,
    team_filter: Optional[List[str]] = None,
    require_cycles_enabled: bool = True,
) -> List[Dict[str, Any]]:
    """Fetch all teams (paginated), optionally restricted by key/name."""
    nodes = client.paginate(
        _TEAMS_QUERY,
        variables={},
        path=["teams"],
        page_size=50,
    )

    if require_cycles_enabled:
        nodes = [t for t in nodes if t.get("cyclesEnabled")]

    if team_filter:
        lowered = {f.lower() for f in team_filter}
        nodes = [
            t
            for t in nodes
            if t["key"].lower() in lowered or t["name"].lower() in lowered
        ]

    return nodes


def fetch_previous_cycle(client: LinearClient, team_id: str) -> Optional[Dict[str, Any]]:
    """Find the most recently completed cycle for a team (Cycle.isPrevious)."""
    nodes = client.paginate(
        _TEAM_CYCLES_QUERY,
        variables={"teamId": team_id},
        path=["team", "cycles"],
        page_size=50,
    )
    for cycle in nodes:
        if cycle.get("isPrevious"):
            return cycle
    return None
