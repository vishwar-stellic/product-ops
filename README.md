# Linear Sprint Status Service

Reports the current and previous cycle ("sprint") status for every team in
Linear, directly via Linear's **GraphQL API** (`https://api.linear.app/graphql`).
This service never uses the Linear MCP tools — all data comes from raw
GraphQL queries in `sprint_status/`.

For each team:

**Current sprint**
- Number of tickets assigned per person
- Breakdown of ticket status per person (e.g. Todo: 2, In Progress: 3, Done: 3)

**Previous sprint**
- Number of tickets that were assigned per person
- Which ones were completed
- Which ones were moved out to the next sprint (rolled over, and actually
  landed in what is now the current cycle)
- Which ones were added during the sprint cycle (created or pulled in after
  the sprint had already started)

Teams are auto-discovered from the Linear workspace; only teams with
`cyclesEnabled: true` are included (teams without sprints have nothing to
report). Optionally restrict to specific teams with `--team` / `SPRINT_STATUS_TEAMS`
(comma-separated team keys or names, e.g. `PROG,PLAN,CARE,EXP,DEVX,PLAT,INT`).

## How it determines each bucket

- **Current sprint** — `Team.activeCycle`, then `Cycle.issues` grouped by
  assignee and `state.name`.
- **Previous sprint** — the cycle where `Cycle.isPrevious == true` (Linear's
  own "most recently completed cycle" flag).
  - **Total assigned** = the cycle's current `issues` connection, unioned
    with `uncompletedIssuesUponClose` (issues that were still open when the
    cycle closed but may since have moved elsewhere) — this way tickets that
    have already rolled out of the closed cycle aren't lost from the count.
  - **Completed** = issues with `state.type == "completed"`.
  - **Moved to next sprint** = issues from `uncompletedIssuesUponClose` whose
    *current* `cycle.id` now equals the team's active cycle.
  - **Added during the cycle** = for each issue, look at `issue.history` for
    a node where `toCycle.number` matches the sprint; if found, its
    `updatedAt` is when it was pulled in. If no such node exists, the issue
    was created straight into the cycle, so `issue.createdAt` is used
    instead. Either timestamp being after `Cycle.startsAt` means it was
    added mid-cycle rather than present at kickoff.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in LINEAR_API_KEY
```

`LINEAR_API_KEY` is a Linear personal API key (Settings → API → Personal API
keys, format `lin_api_...`). If you already have one set up in
`/Users/vishwa/.claude/skills/product-ops/.env` on this machine, the service
will pick it up automatically without any extra setup.

## Usage

### CLI (on-demand, one-shot)

```bash
.venv/bin/python -m sprint_status.cli                    # all teams, current + previous
.venv/bin/python -m sprint_status.cli --team CARE,PLAN    # specific teams
.venv/bin/python -m sprint_status.cli --only previous      # previous sprint only
.venv/bin/python -m sprint_status.cli --json > report.json # machine-readable output
```

### HTTP service (query "at any time")

```bash
.venv/bin/uvicorn sprint_status.server:app --port 8008
```

Then:

```bash
curl http://127.0.0.1:8008/sprints                       # current + previous, all teams
curl http://127.0.0.1:8008/sprints/current                # current sprint only
curl http://127.0.0.1:8008/sprints/previous?team=CARE     # previous sprint, one team
curl "http://127.0.0.1:8008/sprints?fresh=true"           # bypass the 120s cache
```

Responses are cached in-process for `SPRINT_STATUS_CACHE_TTL` seconds
(default 120) so repeated calls are near-instant and don't burn API quota.
Pass `?fresh=true` to force a live refetch.

## Project layout

```
sprint_status/
  config.py         # API key + team filter resolution (.env aware)
  linear_client.py   # raw GraphQL client: auth, retries, pagination, aliasing
  cycles.py          # team + cycle (activeCycle / isPrevious) lookups
  issues.py           # cycle issue scope, uncompletedIssuesUponClose, mid-cycle detection via issue.history
  report.py          # assembles the per-team, per-assignee report
  cli.py             # command-line entry point (rich tables or --json)
  server.py           # FastAPI service for on-demand HTTP queries
```

## Notes

- Every GraphQL connection is fully paginated (`pageInfo.hasNextPage` /
  `endCursor`) — no result is silently truncated.
- Requests are retried with exponential backoff on rate limits (`429`) and
  `5xx` responses.
- `issue.history` lookups are batched using GraphQL aliases (20 issues per
  request) to keep query counts low for large teams.
