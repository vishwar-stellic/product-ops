"""Per-squad (Linear team) data for the web dashboard: projects (split into
"Star Project"-labeled and other current-quarter projects), current sprint,
and previous sprint - restricted to a fixed set of squads.

Each squad's data is built independently so it can be cached and refreshed
one squad at a time (see `server.py`)."""

from concurrent.futures import ThreadPoolExecutor
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

# The workspace-wide projects report (every "Star Project"-labeled project
# plus every project in the current quarter) is identical for every squad -
# `build_squad_data` just filters it down to the projects linked to one
# team. It's cached separately under this key so it's fetched from Linear
# once per staleness window rather than once per squad; see
# `server.py:_get_projects_report`.
PROJECTS_REPORT_CACHE_KEY = "dashboard-projects-report"

# Bump whenever `build_squad_data`'s return shape *or* its underlying
# filtering logic changes (e.g. a new field is added, or which projects/
# updates are included changes) so same-day cache hits don't silently keep
# serving stale results computed under the old logic - see
# `cache.get_or_refresh`'s `version` parameter.
SQUAD_CACHE_VERSION = 12


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
    projects_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Projects + current/previous sprint for a single squad.

    `team` must be a full team node (e.g. from `fetch_dashboard_teams`),
    since `build_current_sprint` needs its `activeCycle` field.

    `projects_report` is the workspace-wide report from
    `build_dashboard_projects_report` - pass in an already-fetched one (as
    `server.py` does, sharing one fetch across every squad) to skip fetching
    it again here. The remaining three Linear calls below are independent
    of each other, so they run concurrently rather than one after another -
    each squad's rebuild used to take as long as all four calls summed
    (~15-35s in practice), now it's roughly the slowest of them.
    """
    if team is None:
        raise ValueError("team is required")
    client = client or LinearClient()

    with ThreadPoolExecutor(max_workers=4) as pool:
        projects_future = (
            pool.submit(build_dashboard_projects_report, client=client, summit_label=summit_label)
            if projects_report is None
            else None
        )
        current_future = pool.submit(build_current_sprint, client, team)
        previous_future = pool.submit(build_previous_sprint, client, team)
        quality_future = pool.submit(build_quality_summary, client, team)

        if projects_future is not None:
            projects_report = projects_future.result()
        current_sprint = current_future.result()
        previous_sprint = previous_future.result()
        quality = quality_future.result()

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
        "currentSprint": current_sprint,
        "previousSprint": previous_sprint,
        "quality": quality,
    }
