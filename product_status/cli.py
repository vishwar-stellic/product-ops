"""Command-line entry point: prints current + previous sprint status per team.

Usage:
    python -m product_status.cli
    python -m product_status.cli --team PROG,PLAN
    python -m product_status.cli --only current
    python -m product_status.cli --json > report.json
    python -m product_status.cli --summit
    python -m product_status.cli --summit --summit-label "Star Project" --json
    python -m product_status.cli --add-tracked-milestones --team Progress
    python -m product_status.cli --add-tracked-milestones --team Progress --dry-run
    python -m product_status.cli --add-tracked-milestones --team Progress --yes
    python -m product_status.cli --add-tracked-milestones --team Progress --milestones "Product: Define,Early Access"
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .config import get_team_filter
from .linear_client import LinearClient, LinearGraphQLError
from .milestone_setup import add_tracked_milestones_for_team, find_team
from .milestones import KEY_MILESTONE_NAMES, resolve_milestone_names
from .report import build_full_report, build_previous_sprint, build_current_sprint
from .cycles import fetch_teams
from .projects import DEFAULT_SUMMIT_LABEL, build_summit_projects_report, quarter_label

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
    parser.add_argument(
        "--add-tracked-milestones",
        action="store_true",
        help="Add any missing canonical tracked milestones (Product: Define, Design: Shape, "
        "Design: Refine, Early Access, Public Launch) to every project for --team "
        "with a start or target date in the current quarter, then exit. Requires "
        "--team (comma-separated for multiple teams). Safe to re-run - projects "
        "that already have a matching milestone are left alone. For each project "
        "with missing milestones, asks which of them to actually create (since "
        "projects can need different ones) unless --dry-run or --yes is also given.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --add-tracked-milestones, show what would be created without calling "
        "Linear or prompting for confirmation",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="With --add-tracked-milestones, skip the per-project prompt and create "
        "every missing tracked milestone for every project automatically",
    )
    parser.add_argument(
        "--milestones",
        help="With --add-tracked-milestones, comma-separated subset of the tracked "
        "milestones to ever consider (default: all five) - narrows what's offered "
        "at the per-project prompt (or created outright with --yes). Options: "
        f"{', '.join(KEY_MILESTONE_NAMES)}",
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


def _select_project_milestones(project: Dict[str, Any], missing: List[str]) -> List[str]:
    """Ask which of `missing`'s milestones to create for this one project -
    projects can have different needs, so this is asked per project rather
    than decided once for the whole team."""
    console.print(f'\n[bold]{project["name"]}[/bold]')
    for i, name in enumerate(missing, start=1):
        console.print(f"  {i}. {name}")

    if len(missing) == 1:
        return missing if Confirm.ask(f"  Add {missing[0]}?", console=console, default=True) else []

    while True:
        answer = (
            Prompt.ask(
                "  Add which? ('all', 'none', or comma-separated numbers)",
                console=console,
                default="all",
            )
            .strip()
            .lower()
        )
        if answer in ("all", "a"):
            return list(missing)
        if answer in ("none", "n"):
            return []
        try:
            indices = {int(part.strip()) for part in answer.split(",") if part.strip()}
        except ValueError:
            console.print("  [red]Enter 'all', 'none', or comma-separated numbers (e.g. 1,3).[/red]")
            continue
        if not indices or not indices.issubset(set(range(1, len(missing) + 1))):
            console.print(f"  [red]Numbers must be between 1 and {len(missing)}.[/red]")
            continue
        return [missing[i - 1] for i in sorted(indices)]


def _render_tracked_milestones_result(result: Dict[str, Any]) -> None:
    if not result["projects"]:
        console.print("[yellow]No projects found for this team.[/yellow]")
        return

    verb = "Would create" if result["dryRun"] else "Created"
    for entry in result["projects"]:
        console.print(f"\n[bold]{entry['project']['name']}[/bold]")
        if entry["created"]:
            console.print(f"  [green]{verb}:[/green] {', '.join(entry['created'])}")
        if entry.get("declined"):
            console.print(f"  [yellow]Skipped:[/yellow] {', '.join(entry['declined'])}")
        if entry["alreadyPresent"]:
            console.print(f"  [dim]Already present:[/dim] {', '.join(entry['alreadyPresent'])}")
        if not entry["created"] and not entry["alreadyPresent"] and not entry.get("declined"):
            console.print("  [dim]No milestones matched or created.[/dim]")

    console.print()


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    team_filter = _resolve_team_filter(args)

    try:
        client = LinearClient()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if args.add_tracked_milestones:
        if not args.team:
            console.print("[red]--team is required with --add-tracked-milestones[/red]")
            return 1
        team_names = [t.strip() for t in args.team.split(",") if t.strip()]

        milestone_names = None
        if args.milestones:
            requested = [m.strip() for m in args.milestones.split(",") if m.strip()]
            try:
                milestone_names = resolve_milestone_names(requested)
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                return 1

        # Prompting per project only makes sense in an interactive terminal
        # session that isn't already dry-running (nothing to confirm) or
        # emitting machine-readable JSON (nothing to prompt against).
        interactive = not args.dry_run and not args.json and not args.yes

        results = []
        exit_code = 0
        for team_name in team_names:
            team = find_team(client, team_name)
            if not team:
                console.print(f'[red]No Linear team found matching "{team_name}"[/red]')
                exit_code = 1
                continue

            if not args.json:
                suffix = " [dim](dry run)[/dim]" if args.dry_run else ""
                console.rule(f"[bold green]{team['name']} — Tracked Milestones{suffix}")
                console.print(f"[dim]Scoped to projects in {quarter_label()}.[/dim]")

            try:
                result = add_tracked_milestones_for_team(
                    client,
                    team,
                    dry_run=args.dry_run,
                    select_milestones=_select_project_milestones if interactive else None,
                    milestone_names=milestone_names,
                )
            except LinearGraphQLError as exc:
                console.print(f"[red]Linear rejected the request: {exc}[/red]")
                if any("scope" in str(e.get("message", "")).lower() for e in exc.errors):
                    console.print(
                        "[yellow]Your LINEAR_API_KEY looks like it's missing the 'Write' scope. "
                        "Create a new personal API key at https://linear.app/settings/api with "
                        "Read and Write access, then update LINEAR_API_KEY in your .env file.[/yellow]"
                    )
                return 1
            results.append(result)
            if not args.json:
                _render_tracked_milestones_result(result)
        if args.json:
            print(json.dumps(results, indent=2, default=str))
        return exit_code

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
