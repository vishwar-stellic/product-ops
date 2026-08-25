"""Builds the Notion block tree for a "Product Ops" dashboard export and
publishes it as a sub-page.

Structure (see README "Publish to Notion" section):

    EPD Report <date>                        (page)
      🤖 Report automatically generated from Linear...   (callout - see
                                               REPORT_INTRO_TEXT)
      <Team name>                            (bulleted link, one per squad - table of contents)
      ...
      <Team name>                            (heading_2, one per squad)
        Projects with label "<label>"  Learn more   (heading_3 - title
                                               reflects `only_star_projects`;
                                               when set, just the labeled
                                               "Star Project" set is
                                               published, otherwise (the
                                               default) the heading reverts
                                               to "Projects", the labeled set
                                               is still shown directly, and
                                               every other current-quarter
                                               project is collapsed under an
                                               "Other Projects" toggle
                                               heading; "Learn more" links to
                                               LEARN_MORE_URL_PROJECTS)
            <Project name> (<status>,        (toggle, one per project - status
             Last Update: <health>)           and latest update's health both
                                               shown in the title so they're
                                               visible without expanding)
              ...status / dates / milestones table...
              📁 Last update by <author> · <date> (<n> days ago) · <health> - <body>
              Callout: "Additional commentary from PL/TL/Designer:"  (bold)
        Other Projects                       (toggle heading_4 - only when
                                               `only_star_projects` is unset;
                                               same per-project toggle
                                               structure as above)
        Other Asks                           (toggle heading_4 - always
                                               shown, right after "Other
                                               Projects")
          ...4-column table: Asks / Goal / Status/Outcome / Commentary/analysis...
        Quality  Learn more                  (heading_3 - "Learn more" links
                                               to LEARN_MORE_URL_QUALITY)
          - Currently out of SLA: ...         (bulleted definitions, above
          - Failed SLA this month: ...         the table)
          ...quality table (Metric / Goal / Value / Notes columns)...
          (no commentary callout - Notes is the column for manual notes)
        Previous Sprint                      (heading_3 - dropped entirely
                                               when `skip_sprint_data` is set;
                                               "Current Sprint" no longer
                                               exists as its own section)
          ...status summary + sprint table...
          Callout: "Additional commentary from PL/TL/Designer:"  (bold)
        Kudos                                (heading_3)
          ...3-column table: Name / Contribution / Why it matters...
        Dependency/Availability  Learn more   (heading_3 - "Learn more" links
                                               to LEARN_MORE_URL_DEPENDENCY)
          (blank - for manual notes)

Note: Notion's public API has no way to set a page's layout to "Full width" -
that's a per-page display setting only exposed in the Notion UI (the "..."
menu on the page), so it has to be toggled on by hand after the page is
created. Likewise, per-column widths on a basic `table` block (e.g. making
Kudos's "Contribution"/"Why it matters" columns wider than "Name") aren't
exposed by the API either - that's display-only state Notion doesn't expose
for API-created tables (only for column *layouts* via `width_ratio`, and
full Notion databases via a view's `width` field - neither applies here),
so column widths have to be dragged to size by hand in the Notion UI.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .milestones import match_key_milestones
from .notion_client import NotionClient, create_nested_blocks, extract_page_id

# Report titles use Pacific time (handles PST/PDT automatically) rather than
# UTC, matching how the team refers to dates/deadlines day-to-day.
_PACIFIC = ZoneInfo("America/Los_Angeles")

DEFAULT_PARENT_PAGE_URL = "https://app.notion.com/p/stellic/Product-Ops-Reports-3be9dd09f473806c875bc8356f5c71a4"
DEFAULT_PARENT_PAGE_ID = extract_page_id(DEFAULT_PARENT_PAGE_URL)

COMMENTARY_TEXT = "Additional commentary from PL/TL/Designer:\n"

# Shown once, in a callout at the very top of the page, above the table of
# contents - a standing disclaimer that the report is generated, not
# hand-written, so readers know to treat it as a starting point.
REPORT_INTRO_TEXT = (
    "Report automatically generated from Linear based on project dates, "
    "project status, project updates and milestone dates. Please review and "
    "add updates as needed."
)

# "Learn more" links appended to a few section headings, pointing at the
# relevant guidelines in the team's "Engineering Reviews" doc.
_GUIDELINES_BASE_URL = "https://app.notion.com/p/Engineering-Reviews-Some-Guidelines-3429dd09f47380058d65c3c8690b251c?source=copy_link"
LEARN_MORE_URL_PROJECTS = f"{_GUIDELINES_BASE_URL}#34b9dd09f473804fb02df0b41af9d1cf"
LEARN_MORE_URL_QUALITY = f"{_GUIDELINES_BASE_URL}#34b9dd09f473807a92dee1d4ee381b77"
LEARN_MORE_URL_DEPENDENCY = f"{_GUIDELINES_BASE_URL}#34b9dd09f47380cd9e01e6ff484492dd"

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


def _relative_days_ago(value: Optional[str]) -> Optional[str]:
    """Renders '(N days ago)'-style text for a project update's `createdAt`
    value, or `None` if it can't be parsed."""
    if not value:
        return None
    try:
        created_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    days = (datetime.now(timezone.utc) - created_at).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


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


def toggle_heading(level: int, title, children: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A toggleable heading block (`heading_2`/`heading_3`/`heading_4` with
    `is_toggleable: true`) - unlike `toggle()` (a plain bulleted-style
    toggle), this renders at the given heading level and shows up in
    Notion's heading/outline hierarchy while still collapsing its
    children."""
    rich = [rich_text(title)] if isinstance(title, str) else title
    key = f"heading_{level}"
    return {
        "type": key,
        key: {"rich_text": rich, "is_toggleable": True},
        "_children": children,
    }


def heading_2(title: str) -> Dict[str, Any]:
    return {"type": "heading_2", "heading_2": {"rich_text": [rich_text(title)]}}


def heading_3(title: str, learn_more_url: Optional[str] = None) -> Dict[str, Any]:
    runs = [rich_text(title)]
    if learn_more_url:
        runs += [rich_text("  "), rich_text("Learn more", link=learn_more_url)]
    return {"type": "heading_3", "heading_3": {"rich_text": runs}}


def _toc_placeholder(team_name: str) -> Dict[str, Any]:
    """A bulleted-list item for one squad's table-of-contents entry. Created
    with plain text first (we don't know the squad's `heading_2` block ID
    until *after* it's created), then patched in place via `update_block`
    once the real ID is known - see `publish_dashboard_to_notion`."""
    return bulleted_item([rich_text(team_name)])


def callout(emoji: str, text_runs) -> Dict[str, Any]:
    return {
        "type": "callout",
        "callout": {"rich_text": _runs(text_runs), "icon": {"type": "emoji", "emoji": emoji}},
    }


def commentary_callout() -> Dict[str, Any]:
    return callout("💡", [rich_text(COMMENTARY_TEXT, bold=True)])


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
    relative = _relative_days_ago(last_update.get("createdAt"))
    when_display = f"{when} ({relative})" if relative else when
    health_label = last_update.get("healthLabel") or "—"
    header_runs = [
        rich_text(f"Last update by {author}  ·  {when_display}  ·  ", bold=True),
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
            f"{_fmt_date(project.get('startDate'))} → {_fmt_date(project.get('targetDate'))}"
        )
        children = [paragraph([rich_text(meta, color="gray")])]
        key_milestones = match_key_milestones(project["milestones"])
        if key_milestones:
            children.append(_milestones_table(key_milestones))
        elif project["milestones"]:
            children.append(
                paragraph([rich_text("None of the tracked milestones are defined for this project.", color="gray")])
            )
        else:
            children.append(paragraph([rich_text("No milestones defined.", color="gray")]))
        children.append(_last_update_callout(project.get("lastUpdate")))
        children.append(commentary_callout())

        last_update = project.get("lastUpdate")
        last_update_status = (last_update.get("healthLabel") if last_update else None) or "—"
        title_runs = [
            rich_text(project["name"], bold=True, link=project.get("url")),
            rich_text(
                f" ({project.get('status') or '—'}, Last Update: {last_update_status})", bold=True
            ),
        ]
        project_toggles.append(toggle(title_runs, children))
    return project_toggles


def _project_group_content(projects: List[Dict[str, Any]], empty_note: str) -> List[Dict[str, Any]]:
    if not projects:
        return [paragraph([rich_text(empty_note, color="gray")]), commentary_callout()]
    return _project_toggles(projects)


def _project_content(
    summit_projects: List[Dict[str, Any]],
    summit_label: str,
    other_projects: Optional[List[Dict[str, Any]]] = None,
    only_star_projects: bool = False,
) -> List[Dict[str, Any]]:
    star_blocks = _project_group_content(summit_projects, f'No projects tagged "{summit_label}" for this squad.')
    if only_star_projects:
        # The heading above this already states the label, so no separate
        # subtitle is needed for the (only) group being shown.
        blocks: List[Dict[str, Any]] = list(star_blocks)
    else:
        blocks = [paragraph([rich_text(f'Star Project (Projects with label "{summit_label}")', bold=True)])]
        blocks.extend(star_blocks)
        # Every non-star project is collapsed into a single toggle heading
        # rather than shown inline, so the Star-labeled set stays the
        # primary focus of the section.
        other_blocks = _project_group_content(other_projects or [], "No other projects for this squad.")
        blocks.append(toggle_heading(4, "Other Projects", other_blocks))

    # "Other Asks" sits right after "Other Projects" (or at the end of the
    # Projects section if that toggle isn't shown) rather than as its own
    # per-squad section.
    blocks.append(_other_asks_toggle())
    return blocks


# Shown as bulleted definitions above the Quality table, so readers don't
# have to guess what "Currently Out of SLA" / "Failed SLA This Month" mean.
_QUALITY_DEFINITIONS = [
    ("Currently out of SLA", "Open bugs that have breached SLA."),
    ("Failed SLA this month", "Bugs that were fixed this month after they had breached their SLA"),
]


def _quality_definitions_blocks() -> List[Dict[str, Any]]:
    return [
        bulleted_item([rich_text(f"{label}: ", bold=True, color="gray"), rich_text(text, color="gray")])
        for label, text in _QUALITY_DEFINITIONS
    ]


def _quality_content(quality: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not quality:
        return [paragraph("No quality data available.")]

    threshold = quality["threshold"]
    goal = {"text": f"≤ {threshold}", "bold": True}
    # Blank by design - a place for whoever's presenting to jot a note
    # directly in the table, rather than a single commentary callout for
    # the whole section.
    notes = ""
    rows = [
        [
            {"text": "SLA Quality Total", "bold": True},
            goal,
            {
                "text": quality["slaQualityTotal"],
                "bold": True,
                "color": _score_color(quality["slaQualityWithinThreshold"]),
            },
            notes,
        ],
        ["Currently Out of SLA", "—", quality["currentlyOutOfSla"], notes],
        ["Failed SLA This Month", "—", quality["failedSlaThisMonth"], notes],
        ["Currently Active High Bugs", "—", quality["currentlyActiveHighBugs"], notes],
        # No data pull for this one - a manual placeholder row, filled in
        # by hand each time (see the "Notes" column). Value is left blank
        # (not "—") since there's nothing computed to show yet.
        ["Number of bugs because of missing tests", "—", "", notes],
        [
            {"text": "Incoming Bugs with High or Urgent priority this month", "bold": True},
            goal,
            {
                "text": quality["incomingHighUrgentThisMonth"],
                "bold": True,
                "color": _score_color(quality["incomingHighUrgentWithinThreshold"]),
            },
            notes,
        ],
    ]
    return _quality_definitions_blocks() + [table(["Metric", "Goal", "Value", "Notes"], rows)]


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


def _kudos_content() -> List[Dict[str, Any]]:
    # Always blank - filled in by hand in Notion for each report. One blank
    # row (rather than just a header) so there's an obvious place to start
    # typing. Note: the API has no way to make "Contribution"/"Why it
    # matters" wider than "Name" (see module docstring) - resize by hand in
    # the Notion UI if desired.
    return [table(["Name", "Contribution", "Why it matters"], [["", "", ""]])]


def _dependency_availability_content() -> List[Dict[str, Any]]:
    return [paragraph([rich_text("Add dependency/availability notes here.", color="gray")])]


def _other_asks_toggle() -> Dict[str, Any]:
    # A single blank row (rather than just headers) gives an obvious place
    # to start typing - filled in by hand for each report. Sits right after
    # "Other Projects" in the Projects section - see `_project_content`.
    other_asks_table = table(["Asks", "Goal", "Status/Outcome", "Commentary/analysis"], [["", "", "", ""]])
    return toggle_heading(4, "Other Asks", [other_asks_table])


def _section_blocks(content_blocks: List[Dict[str, Any]], append_commentary: bool = True) -> List[Dict[str, Any]]:
    return content_blocks + [commentary_callout()] if append_commentary else content_blocks


def build_team_blocks(
    squad: Dict[str, Any],
    skip_sprint_data: bool = True,
    only_star_projects: bool = False,
) -> List[Dict[str, Any]]:
    """Flat block list for one squad: an `heading_2` with the team name,
    followed by an `heading_3` + content for each of Projects / Quality /
    Previous Sprint / Kudos / Dependency-Availability. Headings can't have
    children of their own via the API (only *toggleable* headings can), so
    section content sits as siblings after its heading rather than nested
    under it - the table of contents at the top of the page is what makes
    this navigable.

    If `skip_sprint_data` is set, the Previous Sprint section is dropped
    entirely - useful for weeks where sprint data isn't relevant to call
    out. (There's no separate "Current Sprint" section to reduce - that
    section was removed in favor of Kudos/Dependency-Availability below.)

    If `only_star_projects` is set, the Projects section only includes
    projects labeled with the configured "Star Project" label - matching
    the web dashboard's "Only Star Projects" checkbox. When unset (the
    default), every other current-quarter project is included too,
    collapsed under an "Other Projects" toggle heading, immediately
    followed by an "Other Asks" toggle heading - see `_project_content`."""
    team = squad["team"]
    blocks: List[Dict[str, Any]] = [heading_2(team["name"])]

    summit_label = squad.get("summitLabel", "")
    projects_heading = (
        f'Projects with label "{summit_label}"' if only_star_projects and summit_label else "Projects"
    )
    blocks.append(heading_3(projects_heading, learn_more_url=LEARN_MORE_URL_PROJECTS))
    # Projects gets one commentary callout per project (see
    # `_project_content`) rather than a single one for the whole section.
    blocks.extend(
        _section_blocks(
            _project_content(
                squad.get("summitProjects", []),
                summit_label,
                other_projects=squad.get("otherProjects", []),
                only_star_projects=only_star_projects,
            ),
            append_commentary=False,
        )
    )

    blocks.append(heading_3("Quality", learn_more_url=LEARN_MORE_URL_QUALITY))
    # No commentary callout - the table's own "Notes" column is where notes
    # go instead.
    blocks.extend(_section_blocks(_quality_content(squad.get("quality")), append_commentary=False))

    if not skip_sprint_data:
        blocks.append(heading_3("Previous Sprint"))
        blocks.extend(_section_blocks(_previous_sprint_content(squad.get("previousSprint"))))

    blocks.append(heading_3("Kudos"))
    blocks.extend(_kudos_content())

    blocks.append(heading_3("Dependency/Availability", learn_more_url=LEARN_MORE_URL_DEPENDENCY))
    blocks.extend(_dependency_availability_content())

    return blocks


def publish_dashboard_to_notion(
    dashboard_data: Dict[str, Any],
    parent_page_id: Optional[str] = None,
    client: Optional[NotionClient] = None,
    skip_sprint_data: bool = True,
    only_star_projects: bool = False,
) -> Dict[str, Any]:
    """Create an "EPD Report <date>" sub-page of `parent_page_id` (defaults
    to the workspace's Product Ops Reports page): an intro callout, a table
    of contents linking to each squad, then one `heading_2` per team with
    `heading_3` Projects / Quality / Previous Sprint / Kudos /
    Dependency-Availability sections underneath.

    If `skip_sprint_data` is set, every squad drops its Previous Sprint
    section entirely. If `only_star_projects` is unset, the Projects
    section also includes every other current-quarter project, collapsed
    under an "Other Projects" toggle heading, immediately followed by an
    "Other Asks" toggle heading - see `build_team_blocks`."""
    client = client or NotionClient()
    parent_page_id = parent_page_id or DEFAULT_PARENT_PAGE_ID
    squads = dashboard_data["squads"]

    title = f"EPD Report {datetime.now(_PACIFIC).strftime('%b %-d, %Y')}"
    page = client.create_page(parent_page_id, title)

    # Build one flat top-level block list: the intro callout, then TOC
    # placeholders (so they render at the top of the page), then every
    # squad's blocks. We track where each squad's `heading_2` and each TOC
    # placeholder land in that list so we can look up their real block IDs
    # once created, and link each TOC item to its matching heading.
    intro_callout = callout("🤖", [rich_text(REPORT_INTRO_TEXT, color="gray")])
    blocks: List[Dict[str, Any]] = [intro_callout]
    toc_start = len(blocks)
    blocks.extend(_toc_placeholder(squad["team"]["name"]) for squad in squads)
    heading_positions: List[int] = []
    for squad in squads:
        heading_positions.append(len(blocks))
        blocks.extend(
            build_team_blocks(
                squad, skip_sprint_data=skip_sprint_data, only_star_projects=only_star_projects
            )
        )

    created = create_nested_blocks(client, page["id"], blocks)

    page_url = page.get("url")
    if page_url:
        for i, squad in enumerate(squads):
            heading_id = created[heading_positions[i]]["id"].replace("-", "")
            link = f"{page_url}#{heading_id}"
            client.update_block(
                created[toc_start + i]["id"],
                bulleted_item([rich_text(squad["team"]["name"], link=link)]),
            )

    return {"pageId": page["id"], "url": page_url, "title": title}
