"""Builds the Notion block tree for a "Product Ops" dashboard export and
publishes it as a sub-page.

Structure (see README "Publish to Notion" section):

    Product Ops <date>                       (page)
      <Team name>                            (toggle)
        Projects                             (toggle)
          For Summit (Projects with label "<label>")   (bold heading)
            <Project name>                   (toggle, one per project)
              ...status / dates / milestones table...
              📁 Last update by <author> · <date> · <health> - <body>
              Callout: "Additional commentary from PL/TL:"
          Other projects (<quarter>)         (bold heading)
            ...same per-project structure, for projects not labeled "For
               Summit" but starting/due in the current quarter...
          Other Asks not covered by Projects (toggle, empty - for manual notes)
        Quality                              (toggle)
          ...quality table (with Goal column)...
          Callout: "Additional commentary from PL/TL:"
        Current Sprint                       (toggle)
          ...status summary + sprint table...
          Callout: "Additional commentary from PL/TL:"
        Previous Sprint                      (toggle)
          ...status summary + sprint table...
          Callout: "Additional commentary from PL/TL:"

Note: Notion's public API has no way to set a page's layout to "Full width" -
that's a per-page display setting only exposed in the Notion UI (the "..."
menu on the page), so it has to be toggled on by hand after the page is
created.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .notion_client import NotionClient, create_nested_blocks, extract_page_id

DEFAULT_PARENT_PAGE_URL = "https://app.notion.com/p/stellic/Product-Ops-Reports-3be9dd09f473806c875bc8356f5c71a4"
DEFAULT_PARENT_PAGE_ID = extract_page_id(DEFAULT_PARENT_PAGE_URL)

COMMENTARY_TEXT = "Additional commentary from PL/TL:\n"

_DATE_ONLY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _fmt_date(value: Optional[str]) -> str:
    if not value:
        return "—"
    match = _DATE_ONLY_RE.match(value)
    if not match:
        return value
    year, month, day = (int(part) for part in match.groups())
    return datetime(year, month, day).strftime("%b %-d, %Y")


def _cycle_display_name(cycle: Dict[str, Any]) -> str:
    return cycle.get("name") or f"Cycle {cycle.get('number')}"


# ---- Rich text / block builders -------------------------------------------------


def rich_text(text: str, bold: bool = False, color: Optional[str] = None, link: Optional[str] = None) -> Dict[str, Any]:
    text = str(text)[:2000] or " "
    node: Dict[str, Any] = {"type": "text", "text": {"content": text}}
    if link:
        node["text"]["link"] = {"url": link}
    annotations = {}
    if bold:
        annotations["bold"] = True
    if color:
        annotations["color"] = color
    if annotations:
        node["annotations"] = annotations
    return node


def _runs(value):
    return [rich_text(value)] if isinstance(value, str) else value


def paragraph(text_runs) -> Dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": _runs(text_runs)}}


def bulleted_item(text_runs) -> Dict[str, Any]:
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _runs(text_runs)}}


def toggle(title, children: List[Dict[str, Any]]) -> Dict[str, Any]:
    rich = [rich_text(title, bold=True)] if isinstance(title, str) else title
    return {
        "type": "toggle",
        "toggle": {"rich_text": rich},
        "_children": children,
    }


def callout(emoji: str, text_runs) -> Dict[str, Any]:
    return {
        "type": "callout",
        "callout": {"rich_text": _runs(text_runs), "icon": {"type": "emoji", "emoji": emoji}},
    }


def commentary_callout() -> Dict[str, Any]:
    return callout("💡", [rich_text(COMMENTARY_TEXT)])


def table(headers: List[str], rows: List[List[Any]]) -> Dict[str, Any]:
    width = len(headers)

    def cell_runs(cell: Any) -> List[Dict[str, Any]]:
        if isinstance(cell, dict):
            return [rich_text(cell["text"], bold=cell.get("bold", False), color=cell.get("color"))]
        return [rich_text(cell)]

    header_row = {"type": "table_row", "table_row": {"cells": [cell_runs(h) for h in headers]}}
    data_rows = [{"type": "table_row", "table_row": {"cells": [cell_runs(c) for c in row]}} for row in rows]
    return {
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": [header_row] + data_rows,
        },
    }


def _score_color(within_threshold: bool) -> str:
    return "green" if within_threshold else "red"


_HEALTH_COLORS = {"onTrack": "green", "atRisk": "yellow", "offTrack": "red"}


def _health_color(health: Optional[str]) -> Optional[str]:
    return _HEALTH_COLORS.get(health)


# ---- Per-block-type content -------------------------------------------------


def _milestones_table(milestones: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [
        [
            milestone["name"],
            _fmt_date(milestone.get("targetDate")),
            {
                "text": (milestone.get("status") or "—").replace("_", " ").capitalize(),
                "color": "green" if milestone["completed"] else None,
            },
        ]
        for milestone in milestones
    ]
    return table(["Milestone", "Due Date", "Status"], rows)


def _last_update_callout(last_update: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not last_update:
        return callout("📁", [rich_text("No project updates yet.", color="gray")])

    author = last_update.get("author") or "Unknown"
    when = _fmt_date(last_update.get("createdAt"))
    health_label = last_update.get("healthLabel") or "—"
    header_runs = [
        rich_text(f"Last update by {author}  ·  {when}  ·  ", bold=True),
        rich_text(health_label, bold=True, color=_health_color(last_update.get("health"))),
        rich_text("\n"),
    ]
    body_text = (last_update.get("body") or "").strip() or "No update content."
    return callout("📁", header_runs + [rich_text(body_text)])


def _project_toggles(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    project_toggles: List[Dict[str, Any]] = []
    for project in projects:
        meta = (
            f"{project.get('status') or '—'}  ·  "
            f"{_fmt_date(project.get('startDate'))} → {_fmt_date(project.get('targetDate'))}  ·  "
            f"{project['completedMilestones']}/{project['totalMilestones']} milestones done"
        )
        children = [paragraph([rich_text(meta, color="gray")])]
        if project["milestones"]:
            children.append(_milestones_table(project["milestones"]))
        else:
            children.append(paragraph([rich_text("No milestones defined.", color="gray")]))
        children.append(_last_update_callout(project.get("lastUpdate")))
        children.append(commentary_callout())

        title_runs = [rich_text(project["name"], bold=True, link=project.get("url"))]
        project_toggles.append(toggle(title_runs, children))
    return project_toggles


def _project_group_content(projects: List[Dict[str, Any]], empty_note: str) -> List[Dict[str, Any]]:
    if not projects:
        return [paragraph([rich_text(empty_note, color="gray")]), commentary_callout()]
    return _project_toggles(projects)


def _project_content(
    summit_projects: List[Dict[str, Any]],
    other_projects: List[Dict[str, Any]],
    summit_label: str,
    quarter_label: str,
) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = [
        paragraph([rich_text(f'For Summit (Projects with label "{summit_label}")', bold=True)])
    ]
    blocks.extend(
        _project_group_content(summit_projects, f'No projects tagged "{summit_label}" for this squad.')
    )
    blocks.append(paragraph([rich_text(f"Other projects ({quarter_label})", bold=True)]))
    blocks.extend(
        _project_group_content(
            other_projects, f"No other projects starting or due in {quarter_label} for this squad."
        )
    )
    blocks.append(toggle("Other Asks not covered by Projects", []))
    return blocks


def _quality_content(quality: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not quality:
        return [paragraph("No quality data available.")]

    threshold = quality["threshold"]
    goal = {"text": f"≤ {threshold}", "bold": True}
    rows = [
        [
            {"text": "SLA Quality Total", "bold": True},
            {
                "text": quality["slaQualityTotal"],
                "bold": True,
                "color": _score_color(quality["slaQualityWithinThreshold"]),
            },
            goal,
        ],
        ["Currently Out of SLA", quality["currentlyOutOfSla"], "—"],
        ["Failed SLA This Month", quality["failedSlaThisMonth"], "—"],
        [
            {"text": "Incoming Bugs with High or Urgent priority this month", "bold": True},
            {
                "text": quality["incomingHighUrgentThisMonth"],
                "bold": True,
                "color": _score_color(quality["incomingHighUrgentWithinThreshold"]),
            },
            goal,
        ],
    ]
    return [table(["Metric", "Value", "Goal"], rows)]


def _current_sprint_status_summary(sprint: Dict[str, Any]) -> Optional[str]:
    statuses = sprint.get("statuses", [])
    by_assignee = sprint.get("byAssignee", [])
    if not statuses or not by_assignee:
        return None
    totals = {status: 0 for status in statuses}
    for row in by_assignee:
        for status in statuses:
            totals[status] += row["statusBreakdown"].get(status, 0)
    return "  ·  ".join(f"{status}: {totals[status]}" for status in statuses)


def _current_sprint_content(sprint: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not sprint:
        return [paragraph("No active cycle for this team.")]

    cycle = sprint["cycle"]
    meta = (
        f"{_cycle_display_name(cycle)}  ({_fmt_date(cycle.get('startsAt'))} → {_fmt_date(cycle.get('endsAt'))})"
        f"  ·  {sprint['totalIssues']} issue{'s' if sprint['totalIssues'] != 1 else ''}"
    )
    blocks = [paragraph(meta)]

    statuses = sprint.get("statuses", [])
    by_assignee = sprint.get("byAssignee", [])
    if by_assignee:
        summary = _current_sprint_status_summary(sprint)
        if summary:
            blocks.append(paragraph([rich_text(summary, color="gray")]))
        headers = ["Assignee", "Total"] + statuses
        rows = [
            [row["assignee"], row["total"]] + [row["statusBreakdown"].get(status, 0) for status in statuses]
            for row in by_assignee
        ]
        blocks.append(table(headers, rows))
    else:
        blocks.append(paragraph("No issues in this cycle."))
    return blocks


def _previous_sprint_status_summary(sprint: Dict[str, Any]) -> Optional[str]:
    by_assignee = sprint.get("byAssignee", [])
    if not by_assignee:
        return None
    totals = {
        "Assigned": sprint.get("totalIssues", 0),
        "Completed": sum(row["completed"]["count"] for row in by_assignee),
        "Moved to next": sum(row["movedToNextSprint"]["count"] for row in by_assignee),
        "Removed": sum(row["removedFromCycle"]["count"] for row in by_assignee),
        "Added mid-cycle": sum(row["addedDuringCycle"]["count"] for row in by_assignee),
    }
    return "  ·  ".join(f"{label}: {value}" for label, value in totals.items())


def _previous_sprint_content(sprint: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not sprint:
        return [paragraph("No completed cycle found for this team.")]

    cycle = sprint["cycle"]
    meta = (
        f"{_cycle_display_name(cycle)}  ({_fmt_date(cycle.get('startsAt'))} → {_fmt_date(cycle.get('endsAt'))})"
        f"  ·  {sprint['totalIssues']} issue{'s' if sprint['totalIssues'] != 1 else ''} assigned"
    )
    blocks = [paragraph(meta)]

    by_assignee = sprint.get("byAssignee", [])
    if by_assignee:
        summary = _previous_sprint_status_summary(sprint)
        if summary:
            blocks.append(paragraph([rich_text(summary, color="gray")]))
        headers = ["Assignee", "Assigned", "Completed", "Moved to next", "Removed", "Added mid-cycle"]
        rows = [
            [
                row["assignee"],
                row["totalAssigned"],
                row["completed"]["count"],
                row["movedToNextSprint"]["count"],
                row["removedFromCycle"]["count"],
                row["addedDuringCycle"]["count"],
            ]
            for row in by_assignee
        ]
        blocks.append(table(headers, rows))
    else:
        blocks.append(paragraph("No issues were assigned."))
    return blocks


def _section_toggle(title: str, content_blocks: List[Dict[str, Any]], append_commentary: bool = True) -> Dict[str, Any]:
    children = content_blocks + [commentary_callout()] if append_commentary else content_blocks
    return toggle(title, children)


def build_team_toggle(squad: Dict[str, Any]) -> Dict[str, Any]:
    team = squad["team"]
    sections = [
        # Projects gets one commentary callout per project (see
        # `_project_content`) rather than a single one for the whole toggle.
        _section_toggle(
            "Projects",
            _project_content(
                squad.get("summitProjects", []),
                squad.get("otherProjects", []),
                squad.get("summitLabel", ""),
                squad.get("quarterLabel", "this quarter"),
            ),
            append_commentary=False,
        ),
        _section_toggle("Quality", _quality_content(squad.get("quality"))),
        _section_toggle("Current Sprint", _current_sprint_content(squad.get("currentSprint"))),
        _section_toggle("Previous Sprint", _previous_sprint_content(squad.get("previousSprint"))),
    ]
    return toggle(team["name"], sections)


def publish_dashboard_to_notion(
    dashboard_data: Dict[str, Any],
    parent_page_id: Optional[str] = None,
    client: Optional[NotionClient] = None,
) -> Dict[str, Any]:
    """Create a "Product Ops <date>" sub-page of `parent_page_id` (defaults
    to the workspace's Product Ops Reports page) with one toggle per team,
    each containing Projects / Quality / Current Sprint / Previous Sprint
    sub-toggles."""
    client = client or NotionClient()
    parent_page_id = parent_page_id or DEFAULT_PARENT_PAGE_ID

    title = f"Product Ops {datetime.now(timezone.utc).strftime('%b %-d, %Y')}"
    page = client.create_page(parent_page_id, title)

    team_toggles = [build_team_toggle(squad) for squad in dashboard_data["squads"]]
    create_nested_blocks(client, page["id"], team_toggles)

    return {"pageId": page["id"], "url": page.get("url"), "title": title}
