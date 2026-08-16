"""Assembles the current + previous sprint report for every team."""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .cycles import fetch_previous_cycle, fetch_teams
from .issues import fetch_added_during_cycle, fetch_cycle_issues, fetch_uncompleted_upon_close
from .linear_client import LinearClient

UNASSIGNED = "Unassigned"


def _assignee_name(issue: Dict[str, Any]) -> str:
    assignee = issue.get("assignee")
    return assignee["name"] if assignee else UNASSIGNED


def _issue_summary(issue: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": issue["id"],
        "identifier": issue["identifier"],
        "title": issue["title"],
        "url": issue.get("url"),
        "status": issue["state"]["name"],
        "statusType": issue["state"]["type"],
    }


def _cycle_summary(cycle: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cycle:
        return None
    return {
        "id": cycle["id"],
        "number": cycle.get("number"),
        "name": cycle.get("name"),
        "startsAt": cycle.get("startsAt"),
        "endsAt": cycle.get("endsAt"),
        "completedAt": cycle.get("completedAt"),
    }


def build_current_sprint(client: LinearClient, team: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    active_cycle = team.get("activeCycle")
    if not active_cycle:
        return None

    issues = fetch_cycle_issues(client, active_cycle["id"])

    by_assignee: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"assignee": None, "total": 0, "statusBreakdown": defaultdict(int), "issues": []}
    )

    for issue in issues:
        name = _assignee_name(issue)
        bucket = by_assignee[name]
        bucket["assignee"] = name
        bucket["total"] += 1
        bucket["statusBreakdown"][issue["state"]["name"]] += 1
        bucket["issues"].append(_issue_summary(issue))

    assignees = _finalize_assignee_buckets(by_assignee)

    return {
        "cycle": _cycle_summary(active_cycle),
        "totalIssues": len(issues),
        "byAssignee": assignees,
    }


def build_previous_sprint(client: LinearClient, team: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    previous_cycle = fetch_previous_cycle(client, team["id"])
    if not previous_cycle:
        return None

    active_cycle = team.get("activeCycle")
    active_cycle_id = active_cycle["id"] if active_cycle else None

    current_scope_issues = fetch_cycle_issues(client, previous_cycle["id"])
    uncompleted_on_close = fetch_uncompleted_upon_close(client, previous_cycle["id"])

    merged_by_id: Dict[str, Dict[str, Any]] = {issue["id"]: issue for issue in current_scope_issues}
    uncompleted_by_id: Dict[str, Dict[str, Any]] = {}
    for issue in uncompleted_on_close:
        uncompleted_by_id[issue["id"]] = issue
        merged_by_id.setdefault(issue["id"], issue)

    merged_issues = list(merged_by_id.values())

    added_during = fetch_added_during_cycle(
        client,
        cycle_number=previous_cycle["number"],
        cycle_starts_at=previous_cycle["startsAt"],
        issues=merged_issues,
    )

    by_assignee: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "assignee": None,
            "totalAssigned": 0,
            "completed": [],
            "movedToNextSprint": [],
            "removedFromCycle": [],
            "addedDuringCycle": [],
        }
    )

    for issue in merged_issues:
        name = _assignee_name(issue)
        bucket = by_assignee[name]
        bucket["assignee"] = name
        bucket["totalAssigned"] += 1

        state_type = issue["state"]["type"]
        summary = _issue_summary(issue)

        if state_type == "completed":
            bucket["completed"].append(summary)
        elif issue["id"] in uncompleted_by_id:
            landed_cycle = uncompleted_by_id[issue["id"]].get("cycle")
            landed_cycle_id = landed_cycle["id"] if landed_cycle else None
            if active_cycle_id and landed_cycle_id == active_cycle_id:
                bucket["movedToNextSprint"].append(summary)
            else:
                bucket["removedFromCycle"].append(summary)

        if added_during.get(issue["identifier"]):
            bucket["addedDuringCycle"].append(summary)

    assignees = []
    for name, bucket in by_assignee.items():
        assignees.append(
            {
                "assignee": bucket["assignee"],
                "totalAssigned": bucket["totalAssigned"],
                "completed": {"count": len(bucket["completed"]), "issues": bucket["completed"]},
                "movedToNextSprint": {
                    "count": len(bucket["movedToNextSprint"]),
                    "issues": bucket["movedToNextSprint"],
                },
                "removedFromCycle": {
                    "count": len(bucket["removedFromCycle"]),
                    "issues": bucket["removedFromCycle"],
                },
                "addedDuringCycle": {
                    "count": len(bucket["addedDuringCycle"]),
                    "issues": bucket["addedDuringCycle"],
                },
            }
        )

    assignees.sort(key=lambda a: a["completed"]["count"], reverse=True)

    return {
        "cycle": _cycle_summary(previous_cycle),
        "totalIssues": len(merged_issues),
        "byAssignee": assignees,
    }


def _finalize_assignee_buckets(by_assignee: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for name, bucket in by_assignee.items():
        result.append(
            {
                "assignee": bucket["assignee"],
                "total": bucket["total"],
                "statusBreakdown": dict(bucket["statusBreakdown"]),
                "issues": bucket["issues"],
            }
        )
    result.sort(key=lambda a: a["total"], reverse=True)
    return result


def build_team_report(client: LinearClient, team: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "team": {"id": team["id"], "key": team["key"], "name": team["name"]},
        "currentSprint": build_current_sprint(client, team),
        "previousSprint": build_previous_sprint(client, team),
    }


def build_full_report(
    client: Optional[LinearClient] = None,
    team_filter: Optional[List[str]] = None,
) -> Dict[str, Any]:
    client = client or LinearClient()
    teams = fetch_teams(client, team_filter=team_filter)

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "teams": [build_team_report(client, team) for team in teams],
    }
