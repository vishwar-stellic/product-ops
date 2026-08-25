"""Project summaries for the dashboard's Projects section: projects tagged
with a given label (e.g. "Star Project") plus, separately, any project whose
start or target date falls in the current calendar quarter.

Fetches each matching project's milestones (name, target date, status) and
its most recent project update (author, health, body) via Linear's GraphQL
API, independent of the cycle/sprint reporting in `report.py`.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .linear_client import LinearClient

DEFAULT_SUMMIT_LABEL = "Star Project"

# ProjectMilestoneStatus enum values that count as "done".
COMPLETED_MILESTONE_STATUSES = {"done"}

# Project updates older than this aren't shown as the project's "last
# update" - a 2-week-old (or older) update is more likely to be stale than
# useful, so it's treated the same as having no update at all.
LATEST_UPDATE_MAX_AGE = timedelta(days=14)

_PROJECTS_QUERY = """
query Projects($first: Int!, $after: String, $filter: ProjectFilter!) {
  projects(first: $first, after: $after, filter: $filter) {
    nodes {
      id
      name
      url
      status { name type }
      startDate
      targetDate
      progress
      leadTeam { id key name }
      teams(first: 5) {
        nodes { id key name }
      }
      labels(first: 10) {
        nodes { id name }
      }
      projectMilestones(first: 50) {
        nodes {
          id
          name
          targetDate
          status
          progress
          sortOrder
        }
        pageInfo { hasNextPage endCursor }
      }
      projectUpdates(first: 10) {
        nodes {
          id
          body
          health
          createdAt
          user { id name }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# ProjectUpdate.health enum values -> display label.
_HEALTH_LABELS = {"onTrack": "On track", "atRisk": "At risk", "offTrack": "Off track"}

# ProjectUpdate.body is markdown; the dashboard and Notion export both show
# it as plain text (no renderer on either side), so strip the bits that
# would otherwise show up as raw syntax - embedded images, link brackets,
# emphasis markers.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|`)")


def _clean_update_body(text: str) -> str:
    text = _MD_IMAGE_RE.sub("[image]", text)
    text = _MD_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _MD_EMPHASIS_RE.sub("", text)
    return text.strip()


def fetch_projects(
    client: LinearClient, project_filter: Dict[str, Any], page_size: int = 15
) -> List[Dict[str, Any]]:
    """All non-archived projects matching an arbitrary `ProjectFilter` (paginated)."""
    return client.paginate(
        _PROJECTS_QUERY,
        variables={"filter": project_filter},
        path=["projects"],
        page_size=page_size,
    )


def fetch_projects_by_label(client: LinearClient, label_name: str) -> List[Dict[str, Any]]:
    """All non-archived projects carrying the given project label."""
    return fetch_projects(client, {"labels": {"some": {"name": {"eq": label_name}}}})


def quarter_bounds(now: Optional[datetime] = None) -> Tuple[str, str]:
    """(start, end) `TimelessDate` strings for the current UTC calendar
    quarter, e.g. `("2026-07-01", "2026-10-01")` for Q3. `end` is exclusive.

    Also used by `milestone_setup.fetch_team_projects` to scope
    `--add-tracked-milestones` to the current quarter's projects."""
    now = now or datetime.now(timezone.utc)
    quarter_start_month = ((now.month - 1) // 3) * 3 + 1
    start = date(now.year, quarter_start_month, 1)
    end = (
        date(now.year + 1, 1, 1)
        if quarter_start_month == 10
        else date(now.year, quarter_start_month + 3, 1)
    )
    return start.isoformat(), end.isoformat()


def quarter_label(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"Q{(now.month - 1) // 3 + 1} {now.year}"


def fetch_projects_in_quarter(
    client: LinearClient, quarter_start: str, quarter_end: str
) -> List[Dict[str, Any]]:
    """All non-archived projects with a start date or target date falling in
    the given quarter (`quarter_end` exclusive) - either one counts. Used to
    build the dashboard's "Other Projects" bucket (see
    `build_dashboard_projects_report`)."""
    return fetch_projects(
        client,
        {
            "or": [
                {"startDate": {"gte": quarter_start, "lt": quarter_end}},
                {"targetDate": {"gte": quarter_start, "lt": quarter_end}},
            ]
        },
    )


def health_label(health: Optional[str]) -> str:
    return _HEALTH_LABELS.get(health, health or "—")


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_update_summary(
    project: Dict[str, Any], now: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    # `projectUpdates`'s default connection order isn't documented as
    # newest-first, so rather than rely on that, fetch a batch (10 - plenty
    # for how often these are posted) and pick the max `createdAt` client-side.
    updates = project.get("projectUpdates", {}).get("nodes", [])
    if not updates:
        return None
    latest = max(updates, key=lambda u: u.get("createdAt") or "")

    created_dt = _parse_datetime(latest.get("createdAt"))
    if created_dt is not None:
        now = now or datetime.now(timezone.utc)
        if now - created_dt >= LATEST_UPDATE_MAX_AGE:
            return None

    user = latest.get("user") or {}
    return {
        "id": latest["id"],
        "author": user.get("name"),
        "createdAt": latest.get("createdAt"),
        "health": latest.get("health"),
        "healthLabel": health_label(latest.get("health")),
        "body": _clean_update_body(latest.get("body") or ""),
    }


def _milestone_summary(milestone: Dict[str, Any]) -> Dict[str, Any]:
    status = milestone.get("status")
    return {
        "id": milestone["id"],
        "name": milestone["name"],
        "targetDate": milestone.get("targetDate"),
        "status": status,
        "completed": status in COMPLETED_MILESTONE_STATUSES,
        "progress": milestone.get("progress"),
    }


def _project_summary(project: Dict[str, Any]) -> Dict[str, Any]:
    milestones_raw = project.get("projectMilestones", {}).get("nodes", [])
    milestones_raw = sorted(milestones_raw, key=lambda m: m.get("sortOrder") or 0)
    milestones = [_milestone_summary(m) for m in milestones_raw]

    status = project.get("status") or {}
    labels = [label["name"] for label in project.get("labels", {}).get("nodes", [])]
    teams = [{"id": t["id"], "key": t["key"], "name": t["name"]} for t in project.get("teams", {}).get("nodes", [])]
    lead_team = project.get("leadTeam")

    return {
        "id": project["id"],
        "name": project["name"],
        "url": project.get("url"),
        "status": status.get("name"),
        "statusType": status.get("type"),
        "startDate": project.get("startDate"),
        "targetDate": project.get("targetDate"),
        "progress": project.get("progress"),
        "labels": labels,
        "leadTeam": {"id": lead_team["id"], "key": lead_team["key"], "name": lead_team["name"]}
        if lead_team
        else None,
        # All teams this project is shared with. Many projects here are
        # cross-team (e.g. shared with "Docs" as well as an owning squad),
        # so callers that group projects by squad should check membership
        # in this list rather than assuming a single owner - see
        # `dashboard.py`, which shows a project under every squad it's
        # linked to.
        "teams": teams,
        "totalMilestones": len(milestones),
        "completedMilestones": sum(1 for m in milestones if m["completed"]),
        "milestones": milestones,
        "lastUpdate": _latest_update_summary(project),
    }


def build_summit_projects_report(
    client: Optional[LinearClient] = None,
    label_name: str = DEFAULT_SUMMIT_LABEL,
) -> Dict[str, Any]:
    client = client or LinearClient()
    projects_raw = fetch_projects_by_label(client, label_name)
    projects = [_project_summary(p) for p in projects_raw]
    projects.sort(key=lambda p: (p["targetDate"] is None, p["targetDate"] or ""))

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "label": label_name,
        "totalProjects": len(projects),
        "projects": projects,
    }


def build_dashboard_projects_report(
    client: Optional[LinearClient] = None,
    summit_label: str = DEFAULT_SUMMIT_LABEL,
) -> Dict[str, Any]:
    """Projects for the dashboard's Projects section, split into two groups:

    - `summitProjects` - projects carrying the `summit_label` label (e.g.
      "Star Project").
    - `otherProjects` - projects *not* carrying that label, but with a
      start or target date in the current calendar quarter. The Notion
      export shows these collapsed under a single "Other Projects" toggle
      so the Star-labeled set stays the primary focus.

    A project that's both labeled and in the current quarter is only
    counted once, under `summitProjects` (the explicit label wins).
    """
    client = client or LinearClient()
    quarter_start, quarter_end = quarter_bounds()

    summit_raw = fetch_projects_by_label(client, summit_label)
    summit_ids = {p["id"] for p in summit_raw}

    quarter_raw = fetch_projects_in_quarter(client, quarter_start, quarter_end)
    other_raw = [p for p in quarter_raw if p["id"] not in summit_ids]

    def _sorted_summaries(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        summaries = [_project_summary(p) for p in raw]
        summaries.sort(key=lambda p: (p["targetDate"] is None, p["targetDate"] or ""))
        return summaries

    summit_projects = _sorted_summaries(summit_raw)
    other_projects = _sorted_summaries(other_raw)

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "summitLabel": summit_label,
        "summitProjects": summit_projects,
        "otherProjects": other_projects,
    }
