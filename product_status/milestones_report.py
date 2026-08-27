"""Cross-project milestone timeline + "overload" detection for the "Project
Milestones" dashboard tab.

Every project with a start or target date in the current calendar quarter
(same window as the dashboard's "Other projects" group - see
`projects.quarter_bounds`) is shown on one shared timeline, one row per
project, so it's easy to see at a glance which milestones land close
together. On top of that, this flags anyone who owns multiple milestones
(across *different* projects) landing within `OVERLOAD_WINDOW_DAYS` of each
other - the "two designers double-booked in the same week" scenario this
was built for.

Milestone ownership (who to flag) is derived from the Linear issues linked
to that milestone (`ProjectMilestone.issues`) rather than any single
"project owner" field, since a project's canonical milestones are each
owned by a different role (see `MILESTONE_ROLES`): a designer is on the
hook for "Design: Shape"/"Design: Refine", an eng lead for "Early
Access"/"Public Launch", and so on - whoever's assigned the issues under a
given milestone is whoever's actually doing that work, which is a better
signal than one fixed "project lead" for every milestone type. Falls back
to the project's `lead` for "Product: Define" specifically if that
milestone has no linked issues yet (a project's lead is the closest proxy
for "product lead" Linear actually exposes). A milestone with no issues and
no applicable fallback is simply reported with no owners ("Unassigned" in
the UI) rather than guessing.
"""

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from .dashboard import DASHBOARD_TEAMS
from .linear_client import LinearClient
from .milestones import normalize_milestone_name
from .projects import COMPLETED_MILESTONE_STATUSES, quarter_bounds, quarter_label

MILESTONES_REPORT_CACHE_KEY = "dashboard-milestones-report"

# Bump whenever this module's output shape changes - see
# `dashboard.py:SQUAD_CACHE_VERSION` for why (same on-disk/Blob cache has no
# schema of its own).
MILESTONES_REPORT_CACHE_VERSION = 1

# Which role is "on the hook" for each canonical milestone - see module
# docstring. Fuzzy-matched the same way `milestones.py` matches milestone
# names, so slight naming variations still resolve to a role.
MILESTONE_ROLES = {
    "Product: Define": "Product Lead",
    "Design: Shape": "Designer",
    "Design: Refine": "Designer",
    "Early Access": "Eng Lead",
    "Public Launch": "Eng Lead",
}
_ROLE_BY_NORM = {normalize_milestone_name(name): role for name, role in MILESTONE_ROLES.items()}

# Issues in these states don't count toward "who owns this milestone" -
# their assignee isn't doing (or didn't do) the work anymore.
_INACTIVE_ISSUE_STATE_TYPES = {"canceled", "duplicate"}

# How close together two of the same person's milestones (from different
# projects) need to land to count as "overloaded" - see the AskQuestion
# decision this was built from (1 week).
OVERLOAD_WINDOW_DAYS = 7

_QUARTER_PROJECTS_WITH_OWNERS_QUERY = """
query QuarterProjectMilestoneOwners($first: Int!, $after: String, $filter: ProjectFilter!) {
  projects(first: $first, after: $after, filter: $filter) {
    nodes {
      id
      name
      url
      status { name type }
      startDate
      targetDate
      progress
      lead { id name avatarUrl }
      teams(first: 5) {
        nodes { id key name }
      }
      projectMilestones(first: 20) {
        nodes {
          id
          name
          targetDate
          status
          progress
          sortOrder
          issues(first: 15) {
            nodes {
              id
              assignee { id name avatarUrl }
              state { type }
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def milestone_role(name: Optional[str]) -> Optional[str]:
    """The role "on the hook" for a milestone name, fuzzy-matched against
    `MILESTONE_ROLES` - `None` for anything that isn't one of the five
    canonical milestones (e.g. an ad hoc, project-specific milestone)."""
    norm = normalize_milestone_name(name)
    if norm in _ROLE_BY_NORM:
        return _ROLE_BY_NORM[norm]
    for target_norm, role in _ROLE_BY_NORM.items():
        if target_norm in norm or norm in target_norm:
            return role
    return None


def fetch_quarter_projects_with_owners(
    client: LinearClient, quarter_start: str, quarter_end: str, page_size: int = 8
) -> List[Dict[str, Any]]:
    """Every non-archived project with a start or target date in the given
    quarter, including each milestone's linked issues (for owner
    derivation) and the project's `lead` (fallback owner - see module
    docstring). Deliberately a separate, heavier query from
    `projects.fetch_projects` (which every dashboard squad calls) rather
    than adding these fields there, so the common path stays lean."""
    project_filter = {
        "or": [
            {"startDate": {"gte": quarter_start, "lt": quarter_end}},
            {"targetDate": {"gte": quarter_start, "lt": quarter_end}},
        ]
    }
    return client.paginate(
        _QUARTER_PROJECTS_WITH_OWNERS_QUERY,
        variables={"filter": project_filter},
        path=["projects"],
        page_size=page_size,
    )


def _user_summary(user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not user:
        return None
    return {"id": user["id"], "name": user["name"], "avatarUrl": user.get("avatarUrl")}


def _milestone_owners(
    milestone_raw: Dict[str, Any], role: Optional[str], project_lead: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    owners_by_id: Dict[str, Dict[str, Any]] = {}
    for issue in milestone_raw.get("issues", {}).get("nodes", []):
        state = issue.get("state") or {}
        if state.get("type") in _INACTIVE_ISSUE_STATE_TYPES:
            continue
        assignee = _user_summary(issue.get("assignee"))
        if assignee:
            owners_by_id[assignee["id"]] = assignee

    if not owners_by_id and role == "Product Lead" and project_lead:
        owners_by_id[project_lead["id"]] = project_lead

    return list(owners_by_id.values())


def _milestone_summary(milestone_raw: Dict[str, Any], project_lead: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    status = milestone_raw.get("status")
    role = milestone_role(milestone_raw.get("name"))
    return {
        "id": milestone_raw["id"],
        "name": milestone_raw["name"],
        "targetDate": milestone_raw.get("targetDate"),
        "status": status,
        "completed": status in COMPLETED_MILESTONE_STATUSES,
        "progress": milestone_raw.get("progress"),
        "role": role,
        "owners": _milestone_owners(milestone_raw, role, project_lead),
    }


def _project_summary(
    project_raw: Dict[str, Any], quarter_start: str, quarter_end: str
) -> Dict[str, Any]:
    status = project_raw.get("status") or {}
    teams = [{"id": t["id"], "key": t["key"], "name": t["name"]} for t in project_raw.get("teams", {}).get("nodes", [])]
    lead = _user_summary(project_raw.get("lead"))

    # The project itself qualifies if *either* its start or target date
    # falls in the quarter (see `fetch_quarter_projects_with_owners`), but
    # a long-running project's individual milestones can be dated anywhere
    # - without this, a project that merely *ends* in Q3 would drag in
    # milestones from months earlier, which is neither the focused "this
    # quarter" view asked for nor a meaningful signal for the overload
    # check below (a March milestone can't double-book anyone in August).
    milestones_raw = [
        m
        for m in project_raw.get("projectMilestones", {}).get("nodes", [])
        if m.get("targetDate") and quarter_start <= m["targetDate"] < quarter_end
    ]
    milestones_raw.sort(key=lambda m: (m["targetDate"], m.get("sortOrder") or 0))
    milestones = [_milestone_summary(m, lead) for m in milestones_raw]

    return {
        "id": project_raw["id"],
        "name": project_raw["name"],
        "url": project_raw.get("url"),
        "status": status.get("name"),
        "statusType": status.get("type"),
        "startDate": project_raw.get("startDate"),
        "targetDate": project_raw.get("targetDate"),
        "progress": project_raw.get("progress"),
        "lead": lead,
        "teams": teams,
        "milestones": milestones,
    }


def _is_dashboard_team_project(project: Dict[str, Any]) -> bool:
    dashboard_keys = {key.lower() for key in DASHBOARD_TEAMS}
    return any(team["key"].lower() in dashboard_keys for team in project["teams"])


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _flush_cluster(cluster: List[Dict[str, Any]], person: Dict[str, Any], overloads: List[Dict[str, Any]]) -> None:
    """A cluster only counts as an overload if it spans 2+ *different*
    projects - back-to-back milestones on the same project (e.g. "Design:
    Shape" then "Design: Refine" a week apart) is normal cadence, not the
    cross-project double-booking this is meant to catch."""
    if len(cluster) < 2 or len({entry["projectId"] for entry in cluster}) < 2:
        return
    milestones = [{k: v for k, v in entry.items() if not k.startswith("_")} for entry in cluster]
    overloads.append(
        {
            "person": person,
            "windowStart": cluster[0]["targetDate"],
            "windowEnd": cluster[-1]["targetDate"],
            "milestones": milestones,
        }
    )


def _detect_overloads(projects: List[Dict[str, Any]], window_days: int = OVERLOAD_WINDOW_DAYS) -> List[Dict[str, Any]]:
    entries_by_person: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    people: Dict[str, Dict[str, Any]] = {}

    for project in projects:
        for milestone in project["milestones"]:
            # Already done - no longer a live workload concern, so it
            # shouldn't count toward flagging someone as overloaded (it's
            # still shown on the timeline itself, just excluded here).
            if milestone["completed"]:
                continue
            target = _parse_date(milestone["targetDate"])
            if target is None:
                continue
            for owner in milestone["owners"]:
                people[owner["id"]] = owner
                entries_by_person[owner["id"]].append(
                    {
                        "projectId": project["id"],
                        "projectName": project["name"],
                        "projectUrl": project["url"],
                        "milestoneId": milestone["id"],
                        "milestoneName": milestone["name"],
                        "role": milestone["role"],
                        "targetDate": milestone["targetDate"],
                        "_date": target,
                    }
                )

    overloads: List[Dict[str, Any]] = []
    for person_id, entries in entries_by_person.items():
        entries.sort(key=lambda entry: entry["_date"])
        cluster: List[Dict[str, Any]] = []
        for entry in entries:
            if cluster and (entry["_date"] - cluster[-1]["_date"]).days > window_days:
                _flush_cluster(cluster, people[person_id], overloads)
                cluster = []
            cluster.append(entry)
        _flush_cluster(cluster, people[person_id], overloads)

    overloads.sort(key=lambda o: o["windowStart"])
    return overloads


def build_milestones_report(client: Optional[LinearClient] = None) -> Dict[str, Any]:
    client = client or LinearClient()
    quarter_start, quarter_end = quarter_bounds()

    raw_projects = fetch_quarter_projects_with_owners(client, quarter_start, quarter_end)
    projects = [_project_summary(p, quarter_start, quarter_end) for p in raw_projects]
    projects = [p for p in projects if p["milestones"] and _is_dashboard_team_project(p)]
    projects.sort(key=lambda p: (p["milestones"][0]["targetDate"], p["name"]))

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "quarterLabel": quarter_label(),
        "quarterStart": quarter_start,
        "quarterEnd": quarter_end,
        "overloadWindowDays": OVERLOAD_WINDOW_DAYS,
        "projects": projects,
        "overloads": _detect_overloads(projects),
    }
