"""Cross-project milestone timeline + "overload" detection for the "Project
Milestones" dashboard tab.

Every project *starting* in the current calendar quarter (same window as
the dashboard's "Other projects" group - see `projects.quarter_bounds`) is
shown on one shared timeline, one row per project - sorted by start date,
earliest on top - so it's easy to see at a glance which milestones land
close together. Every qualifying project gets a row even if it has no
dated milestones this quarter (an empty track), rather than disappearing
from the view entirely. Only the five canonical lifecycle milestones
(`milestones.KEY_MILESTONE_NAMES`) are plotted - same set the EPD Report
tab's project cards track - so the timeline stays scannable rather than
cluttered with every ad hoc, project-specific milestone. A canonical
milestone with no target date set can't be placed on the timeline at all
(there's no date to plot it at), so it's instead called out inline in that
project's own row (`Project.undatedMilestones`) - a nudge to go set a date
rather than the milestone just silently not appearing anywhere. On top of
that, this flags anyone who owns multiple milestones (across *different*
projects) landing within `OVERLOAD_WINDOW_DAYS` of each other - the "two
designers double-booked in the same week" scenario this was built for.

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
for "product lead" Linear actually exposes).

Candidate owners (from issue assignees, or the `lead` fallback) are then
cross-checked against `PERSON_ROLES` - a hand-maintained map of who
actually does which job function - and dropped if they don't hold the
role that milestone type calls for (e.g. an engineer who happened to get
assigned a ticket under "Design: Shape" isn't "the designer"). A milestone
with no issues, or none of the milestone's issue-assignees actually
matching the expected role, is reported with no owners ("Unassigned" in
the UI) rather than guessing. See `PERSON_ROLES` to correct/extend who's
who.
"""

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from .dashboard import DASHBOARD_TEAMS
from .linear_client import LinearClient
from .milestones import match_key_milestones, normalize_milestone_name
from .projects import COMPLETED_MILESTONE_STATUSES, quarter_bounds, quarter_label

MILESTONES_REPORT_CACHE_KEY = "dashboard-milestones-report"

# Bump whenever this module's output shape changes - see
# `dashboard.py:SQUAD_CACHE_VERSION` for why (same on-disk/Blob cache has no
# schema of its own).
MILESTONES_REPORT_CACHE_VERSION = 5

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

# Hand-maintained job-function map (first name -> role), since Linear has
# no such field. Used to cross-check issue-assignee-derived owners against
# the role their milestone type calls for (see module docstring) - told to
# the agent directly rather than guessed from ticket assignments. Matched
# against the *first word* of a Linear user's display name, case-
# insensitive; extend/correct this as the roster changes.
PERSON_ROLES = {
    "namhee": "Designer",
    "naqi": "Designer",
    "adeline": "Designer",
    "caleb": "Product Lead",
    "arjun": "Product Lead",
    "gordon": "Product Lead",
    "jon": "Product Lead",
    "rukhsar": "Product Lead",
}

# Everyone not named above - "Others are Engineers" - matches the role
# label used for the milestones an engineer owns (Early Access/Public
# Launch), so a plain `==` against a milestone's `role` works uniformly.
DEFAULT_PERSON_ROLE = "Eng Lead"


def person_role(name: Optional[str]) -> str:
    """This person's job function, per `PERSON_ROLES` (defaulting to
    `DEFAULT_PERSON_ROLE`) - used to sanity-check who a milestone gets
    attributed to, not just who happened to be assigned a linked ticket."""
    first_name = (name or "").strip().split(" ", 1)[0].lower()
    return PERSON_ROLES.get(first_name, DEFAULT_PERSON_ROLE)


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
    """Every non-archived project *starting* in the given quarter, including
    each milestone's linked issues (for owner derivation) and the project's
    `lead` (fallback owner - see module docstring). Filtered on `startDate`
    specifically (not target date too) so this stays "projects kicking off
    this quarter" rather than pulling in anything merely wrapping up this
    quarter after starting long before it. Deliberately a separate, heavier
    query from `projects.fetch_projects` (which every dashboard squad
    calls) rather than adding these fields there, so the common path stays
    lean."""
    project_filter = {"startDate": {"gte": quarter_start, "lt": quarter_end}}
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
        # Only count an assignee as "the owner" if their actual job
        # function (see PERSON_ROLES) matches what this milestone type
        # calls for - an engineer picking up a ticket filed under "Design:
        # Shape" doesn't make them the designer.
        if assignee and (role is None or person_role(assignee["name"]) == role):
            owners_by_id[assignee["id"]] = assignee

    if (
        not owners_by_id
        and role == "Product Lead"
        and project_lead
        and person_role(project_lead["name"]) == role
    ):
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

    # Only the five canonical lifecycle milestones - see module docstring -
    # regardless of date, so an undated one still gets caught below rather
    # than silently vanishing.
    canonical_raw = match_key_milestones(project_raw.get("projectMilestones", {}).get("nodes", []))

    # The project itself qualifies by *starting* in the quarter (see
    # `fetch_quarter_projects_with_owners`), but a long-running project's
    # individual milestones can be dated anywhere - without this, milestones
    # from months down the line would show up right away, which isn't a
    # meaningful signal for the overload check below (a milestone 5 months
    # out can't double-book anyone this week).
    dated_raw = [m for m in canonical_raw if m.get("targetDate") and quarter_start <= m["targetDate"] < quarter_end]
    dated_raw.sort(key=lambda m: (m["targetDate"], m.get("sortOrder") or 0))
    milestones = [_milestone_summary(m, lead) for m in dated_raw]

    # Can't place these on the timeline at all with no date - called out
    # inline in this project's own row instead (see `undatedMilestones` in
    # app.js's row-label rendering) rather than just dropping them.
    undated_milestones = [_milestone_summary(m, lead) for m in canonical_raw if not m.get("targetDate")]

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
        "undatedMilestones": undated_milestones,
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
    projects = [p for p in projects if _is_dashboard_team_project(p)]
    # Every project that qualified above gets a row - even one with zero
    # dated milestones this quarter renders as an empty track - sorted by
    # start date so the timeline reads chronologically top-to-bottom. The
    # `startDate` filter in `fetch_quarter_projects_with_owners` guarantees
    # this is always set for anything reaching here.
    projects.sort(key=lambda p: (p["startDate"], p["name"]))

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "quarterLabel": quarter_label(),
        "quarterStart": quarter_start,
        "quarterEnd": quarter_end,
        "overloadWindowDays": OVERLOAD_WINDOW_DAYS,
        "projects": projects,
        "overloads": _detect_overloads(projects),
    }
