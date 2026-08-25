"""Creates the canonical tracked milestones (`milestones.KEY_MILESTONE_NAMES`)
on every project belonging to a given team, via Linear's
`projectMilestoneCreate` mutation.

Safe to re-run: any project that already has a milestone fuzzy-matching a
canonical name (see `milestones.match_key_milestones_map`) is left alone for
that name - only the missing ones are created.
"""

from typing import Any, Callable, Dict, List, Optional

from .cycles import fetch_teams
from .linear_client import LinearClient
from .milestones import KEY_MILESTONE_NAMES, match_key_milestones_map
from .projects import quarter_bounds

# Called with (project, missing_names) before creating milestones for a
# project that has at least one missing; returns the subset of
# missing_names to actually create for that project (may be all, none, or
# anything in between - different projects can get different answers).
SelectFn = Callable[[Dict[str, Any], List[str]], List[str]]

_TEAM_PROJECTS_QUERY = """
query TeamProjects($first: Int!, $after: String, $filter: ProjectFilter!) {
  projects(first: $first, after: $after, filter: $filter) {
    nodes {
      id
      name
      url
      projectMilestones(first: 50) {
        nodes { id name }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_CREATE_MILESTONE_MUTATION = """
mutation CreateProjectMilestone($input: ProjectMilestoneCreateInput!) {
  projectMilestoneCreate(input: $input) {
    success
    projectMilestone { id name }
  }
}
"""


def find_team(client: LinearClient, team_name: str) -> Optional[Dict[str, Any]]:
    """Look up a single team by key or name (case-insensitive). Doesn't
    require cycles to be enabled - unlike the dashboard/sprint report, any
    team with projects is a valid target here."""
    matches = fetch_teams(client, team_filter=[team_name], require_cycles_enabled=False)
    return matches[0] if matches else None


def fetch_team_projects(client: LinearClient, team_id: str) -> List[Dict[str, Any]]:
    """Non-archived projects accessible to (shared with) this team that also
    have a start or target date in the current calendar quarter (see
    `projects.quarter_bounds`) - keeps `--add-tracked-milestones` scoped to
    projects actually being planned/delivered right now, rather than every
    project the team has ever touched."""
    quarter_start, quarter_end = quarter_bounds()
    return client.paginate(
        _TEAM_PROJECTS_QUERY,
        variables={
            "filter": {
                "and": [
                    {"accessibleTeams": {"some": {"id": {"eq": team_id}}}},
                    {
                        "or": [
                            {"startDate": {"gte": quarter_start, "lt": quarter_end}},
                            {"targetDate": {"gte": quarter_start, "lt": quarter_end}},
                        ]
                    },
                ]
            }
        },
        path=["projects"],
        page_size=25,
    )


def create_project_milestone(client: LinearClient, project_id: str, name: str) -> Dict[str, Any]:
    data = client.query(_CREATE_MILESTONE_MUTATION, {"input": {"projectId": project_id, "name": name}})
    payload = data["projectMilestoneCreate"]
    if not payload["success"]:
        raise RuntimeError(f'Linear rejected creating milestone "{name}" on project {project_id}')
    return payload["projectMilestone"]


def add_tracked_milestones_for_team(
    client: LinearClient,
    team: Dict[str, Any],
    dry_run: bool = False,
    select_milestones: Optional[SelectFn] = None,
    milestone_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """For every project belonging to `team` (a team dict from `find_team`),
    create whichever of the target tracked milestones it doesn't already
    have (fuzzy-matched by name). Existing milestones are never modified or
    duplicated.

    `milestone_names` sets the ceiling of which of the five canonical
    milestones are ever considered - defaults to all of
    `milestones.KEY_MILESTONE_NAMES` (see `milestones.resolve_milestone_names`
    to turn free-form user input into a valid subset).

    If `select_milestones` is given, it's called once per project that has
    at least one missing (targeted) milestone, *before* creating anything
    for that project, and only the names it returns are created - since
    different projects can need different subsets, this is where per-project
    choices happen (see `cli.py`'s interactive prompt). Anything not
    returned is recorded as "declined" rather than "created". Ignored when
    `dry_run` is set, since nothing is created either way.
    """
    target_names = milestone_names if milestone_names is not None else KEY_MILESTONE_NAMES

    projects = fetch_team_projects(client, team["id"])
    project_results = []
    for project in projects:
        existing = project.get("projectMilestones", {}).get("nodes", [])
        already_present = match_key_milestones_map(existing)
        missing = [name for name in target_names if name not in already_present]

        created_names: List[str] = []
        declined_names: List[str] = []
        if missing:
            if dry_run:
                created_names = list(missing)
            else:
                to_create = select_milestones(project, missing) if select_milestones is not None else missing
                declined_names = [name for name in missing if name not in to_create]
                for name in to_create:
                    created = create_project_milestone(client, project["id"], name)
                    created_names.append(created["name"])

        project_results.append(
            {
                "project": {"id": project["id"], "name": project["name"], "url": project.get("url")},
                "created": created_names,
                "declined": declined_names,
                "alreadyPresent": [name for name in target_names if name in already_present],
            }
        )

    return {
        "team": {"id": team["id"], "key": team["key"], "name": team["name"]},
        "dryRun": dry_run,
        "projects": project_results,
    }
