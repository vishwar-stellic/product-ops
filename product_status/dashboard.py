"""Per-squad (Linear team) data for the web dashboard: projects (split into
"Star Project"-labeled and other current-quarter projects), current sprint,
and previous sprint - restricted to a fixed set of squads.

Each squad's data is built independently so it can be cached and refreshed
one squad at a time (see `server.py`)."""

from typing import Any, Dict, List, Optional

from .cycles import fetch_teams
from .linear_client import LinearClient
from .projects import DEFAULT_SUMMIT_LABEL, build_dashboard_projects_report
from .quality import build_quality_summary
from .report import build_current_sprint, build_previous_sprint

# The dashboard only ever shows these squads, in this order - regardless of
# how many other teams exist in the Linear workspace.
DASHBOARD_TEAMS = ["PROG", "PLAN", "CARE", "EXP", "PLAT", "INT", "DEVX"]

TEAMS_CACHE_KEY = "dashboard-teams"

# Bump whenever `build_squad_data`'s return shape *or* its underlying
# filtering logic changes (e.g. a new field is added, or which projects/
# updates are included changes) so same-day cache hits don't silently keep
# serving stale results computed under the old logic - see
# `cache.get_or_refresh`'s `version` parameter.
SQUAD_CACHE_VERSION = 10


def squad_cache_key(team_key: str) -> str:
    return f"dashboard-squad-{team_key.lower()}"


def fetch_dashboard_teams(
    client: Optional[LinearClient] = None,
    team_filter: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """The squads shown on the dashboard, in `DASHBOARD_TEAMS` order.

    Returns full team nodes (including `activeCycle`), as required by
    `build_squad_data` - not just the id/key/name summary."""
    client = client or LinearClient()
    team_filter = DASHBOARD_TEAMS if team_filter is None else team_filter

    teams = fetch_teams(client, team_filter=team_filter)
    # fetch_teams doesn't guarantee any particular order; pin it to the
    # order the squads were requested in.
    order = {key.lower(): i for i, key in enumerate(team_filter)}
    teams.sort(key=lambda t: order.get(t["key"].lower(), len(order)))

    return teams


def build_squad_data(
    client: Optional[LinearClient] = None,
    team: Optional[Dict[str, Any]] = None,
    summit_label: str = DEFAULT_SUMMIT_LABEL,
) -> Dict[str, Any]:
    """Projects + current/previous sprint for a single squad.

    `team` must be a full team node (e.g. from `fetch_dashboard_teams`),
    since `build_current_sprint` needs its `activeCycle` field.
    """
    if team is None:
        raise ValueError("team is required")
    client = client or LinearClient()

    projects_report = build_dashboard_projects_report(client=client, summit_label=summit_label)

    # A project can be shared across multiple teams (e.g. also shared with
    # "Docs"), so show it under every dashboard squad it's linked to rather
    # than guessing a single "owner" - matches what each team sees in Linear.
    def _for_team(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [p for p in projects if team["id"] in {t["id"] for t in p["teams"]}]

    return {
        "team": {"id": team["id"], "key": team["key"], "name": team["name"]},
        "summitLabel": projects_report["summitLabel"],
        "summitProjects": _for_team(projects_report["summitProjects"]),
        "otherProjects": _for_team(projects_report["otherProjects"]),
        "currentSprint": build_current_sprint(client, team),
        "previousSprint": build_previous_sprint(client, team),
        "quality": build_quality_summary(client, team),
    }
