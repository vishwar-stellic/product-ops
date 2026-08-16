"""Command-line entry point: prints current + previous sprint status per team.

Usage:
    python -m product_status.cli
    python -m product_status.cli --team PROG,PLAN
    python -m product_status.cli --only current
    python -m product_status.cli --json > report.json
    python -m product_status.cli --summit
    python -m product_status.cli --summit --summit-label "For Summit" --json
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table

from .config import get_team_filter
from .linear_client import LinearClient
from .report import build_full_report, build_previous_sprint, build_current_sprint
from .cycles import fetch_teams
from .projects import DEFAULT_SUMMIT_LABEL, build_summit_projects_report

console = Console()


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Linear sprint status (current + previous cycle)")
    parser.add_argument(
        "--team",
        help="Comma-separated team keys/names to include (overrides SPRINT_STATUS_TEAMS)",
    )
    parser.add_argument(
        "--only",
        choices=["current", "previous", "both"],
        default="both",
        help="Which sprint(s) to report",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of tables")
    parser.add_argument(
        "--summit",
        action="store_true",
        help="Print the project summary (with milestones) for projects tagged "
        "with --summit-label instead of the sprint report",
    )
    parser.add_argument(
        "--summit-label",
        default=DEFAULT_SUMMIT_LABEL,
        help=f'Project label to filter on for --summit (default: "{DEFAULT_SUMMIT_LABEL}")',
    )
    return parser.parse_args(argv)


def _resolve_team_filter(args: argparse.Namespace) -> Optional[List[str]]:
    if args.team:
        return [t.strip() for t in args.team.split(",") if t.strip()]
    return get_team_filter()


def _render_current_table(team_name: str, current: Optional[Dict[str, Any]]) -> None:
    console.rule(f"[bold cyan]{team_name} — Current Sprint")
    if not current:
        console.print("[yellow]No active cycle for this team.[/yellow]")
        return

    cycle = current["cycle"]
    console.print(f"[bold]{cycle['name']}[/bold]  ({cycle['startsAt'][:10]} → {cycle['endsAt'][:10]})")
    console.print(f"Total issues in sprint: {current['totalIssues']}\n")

    table = Table(show_lines=False)
    table.add_column("Assignee")
    table.add_column("Total", justify="right")
    statuses = current.get("statuses", [])
    for status in statuses:
        table.add_column(status, justify="right")

    for row in current["byAssignee"]:
        breakdown = [str(row["statusBreakdown"].get(status, 0)) for status in statuses]
        table.add_row(row["assignee"], str(row["total"]), *breakdown)

    console.print(table)
    console.print()


def _render_previous_table(team_name: str, previous: Optional[Dict[str, Any]]) -> None:
    console.rule(f"[bold magenta]{team_name} — Previous Sprint")
    if not previous:
        console.print("[yellow]No completed cycle found for this team.[/yellow]")
        return

    cycle = previous["cycle"]
    console.print(f"[bold]{cycle['name']}[/bold]  ({cycle['startsAt'][:10]} → {cycle['endsAt'][:10]})")
    console.print(f"Total issues assigned to sprint: {previous['totalIssues']}\n")

    table = Table(show_lines=False)
    table.add_column("Assignee")
    table.add_column("Assigned", justify="right")
    table.add_column("Completed", justify="right")
    table.add_column("Moved to next sprint", justify="right")
    table.add_column("Added during cycle", justify="right")

    for row in previous["byAssignee"]:
        table.add_row(
            row["assignee"],
            str(row["totalAssigned"]),
            str(row["completed"]["count"]),
            str(row["movedToNextSprint"]["count"]),
            str(row["addedDuringCycle"]["count"]),
        )

    console.print(table)
    console.print()


def _render_summit_projects(report: Dict[str, Any]) -> None:
    console.rule(f"[bold green]Projects tagged \"{report['label']}\"")

    if not report["projects"]:
        console.print(f"[yellow]No projects found with label \"{report['label']}\".[/yellow]")
        return

    for project in report["projects"]:
        target = project["targetDate"] or "—"
        start = project["startDate"] or "—"
        console.print(
            f"\n[bold]{project['name']}[/bold]  "
            f"[dim]({project['status']})[/dim]  "
            f"{start} → {target}"
        )
        console.print(
            f"Milestones completed: {project['completedMilestones']}/{project['totalMilestones']}"
        )

        if not project["milestones"]:
            console.print("[yellow]No milestones defined.[/yellow]")
            continue

        table = Table(show_lines=False)
        table.add_column("Milestone")
        table.add_column("Target date")
        table.add_column("Status")
        table.add_column("Complete", justify="center")

        for milestone in project["milestones"]:
            table.add_row(
                milestone["name"],
                milestone["targetDate"] or "—",
                milestone["status"],
                "✅" if milestone["completed"] else "—",
            )

        console.print(table)

    console.print()


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    team_filter = _resolve_team_filter(args)

    try:
        client = LinearClient()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if args.summit:
        report = build_summit_projects_report(client=client, label_name=args.summit_label)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            _render_summit_projects(report)
        return 0

    if args.json:
        if args.only == "both":
            report = build_full_report(client=client, team_filter=team_filter)
        else:
            teams = fetch_teams(client, team_filter=team_filter)
            report = {"generatedAt": None, "teams": []}
            for team in teams:
                entry = {"team": {"id": team["id"], "key": team["key"], "name": team["name"]}}
                if args.only == "current":
                    entry["currentSprint"] = build_current_sprint(client, team)
                else:
                    entry["previousSprint"] = build_previous_sprint(client, team)
                report["teams"].append(entry)
        print(json.dumps(report, indent=2, default=str))
        return 0

    teams = fetch_teams(client, team_filter=team_filter)
    if not teams:
        console.print("[yellow]No teams matched the given filter (or none have cycles enabled).[/yellow]")
        return 0

    for team in teams:
        if args.only in ("current", "both"):
            current = build_current_sprint(client, team)
            _render_current_table(team["name"], current)
        if args.only in ("previous", "both"):
            previous = build_previous_sprint(client, team)
            _render_previous_table(team["name"], previous)

    return 0


if __name__ == "__main__":
    sys.exit(main())
