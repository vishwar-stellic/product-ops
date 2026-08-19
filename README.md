# Linear Product Status Service

Reports the current and previous cycle ("sprint") status for every team in
Linear, plus project summaries (with milestones) for projects tagged with a
given label, directly via Linear's **GraphQL API**
(`https://api.linear.app/graphql`). This service never uses the Linear MCP
tools — all data comes from raw GraphQL queries in `product_status/`.

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

Also reports a **project summary for projects tagged with a given label**
(default `"Star Project"`): for each matching project, its status, dates, and
every milestone with its target date and whether it's complete.

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
- **Project summary** = `Query.projects` filtered by
  `filter: { labels: { some: { name: { eq: <label> } } } }`, then each
  project's `projectMilestones` connection. A milestone counts as complete
  when its `status` enum is `done` (the other possible values are
  `unstarted`, `next`, and `overdue`).

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
.venv/bin/python -m product_status.cli                    # all teams, current + previous
.venv/bin/python -m product_status.cli --team CARE,PLAN    # specific teams
.venv/bin/python -m product_status.cli --only previous      # previous sprint only
.venv/bin/python -m product_status.cli --json > report.json # machine-readable output

.venv/bin/python -m product_status.cli --summit             # projects tagged "Star Project" + milestones
.venv/bin/python -m product_status.cli --summit --summit-label "Q3 Project" --json

# Add any missing canonical tracked milestones (Product: Define, Design: Shape,
# Design: Refine, Early Access, Public Launch) to every project for a team
# that has a start or target date in the current calendar quarter (same
# quarter window as the dashboard's "Other projects" group - see
# product_status/projects.py:quarter_bounds). Safe to re-run - projects that
# already have a matching milestone are skipped (fuzzy-matched by name, see
# product_status/milestones.py). --team accepts a team key or name,
# comma-separated for multiple teams. By default, for each project with
# missing milestones, lists them and asks which ones (by number, 'all', or
# 'none') to actually create - different projects can get different
# answers, since not every project needs every milestone.
.venv/bin/python -m product_status.cli --add-tracked-milestones --team Progress --dry-run  # preview only, no prompts
.venv/bin/python -m product_status.cli --add-tracked-milestones --team Progress            # asks per project
.venv/bin/python -m product_status.cli --add-tracked-milestones --team Progress --yes      # no prompts, create all missing

# --milestones narrows which of the five are ever offered/created (default: all
# five) - still applies uniformly across projects; use the per-project prompt
# above for per-project differences. Fuzzy-matched, comma-separated.
.venv/bin/python -m product_status.cli --add-tracked-milestones --team Progress --milestones "Product: Define,Early Access"
```

### HTTP service (query "at any time")

```bash
.venv/bin/uvicorn product_status.server:app --port 8008
```

Then:

```bash
curl http://127.0.0.1:8008/sprints                       # current + previous, all teams
curl http://127.0.0.1:8008/sprints/current                # current sprint only
curl http://127.0.0.1:8008/sprints/previous?team=CARE     # previous sprint, one team
curl "http://127.0.0.1:8008/sprints?fresh=true"           # bypass the 120s cache

curl http://127.0.0.1:8008/projects/summit                # projects tagged "Star Project" + milestones
curl "http://127.0.0.1:8008/projects/summit?label=Q3+Project"
```

Responses are cached in-process for `SPRINT_STATUS_CACHE_TTL` seconds
(default 120) so repeated calls are near-instant and don't burn API quota.
Pass `?fresh=true` to force a live refetch.

### Web dashboard

With the HTTP service running, open **http://127.0.0.1:8008/** for a
dashboard (with its own favicon, `product_status/static/favicon.svg`)
organized as one section per squad — Progress, Plan, Care, Explore,
Platform, Integration, and DevX (`product_status/dashboard.py:DASHBOARD_TEAMS`)
— in that order, regardless of what other teams exist in the Linear
workspace.

Each squad's header stays docked just below the top bar while you scroll
through that squad's section (`position: sticky`, offset by the top bar's
measured height - see `product_status/static/app.js:syncTopbarHeight`), so
you always know which team's data you're looking at; scrolling past the
section lets the next squad's header take over. Click a squad's header to
collapse/expand its section. Each squad's section has, in order:

1. **Projects** — linked to that squad's team in Linear, each with its
   milestones (target date + done/not done — completed milestones are
   shown with a filled checkmark, not struck through). A project shared
   across multiple teams (e.g. also shared with "Docs") shows up under
   every squad it's linked to, not just one. Split into two groups
   (`product_status/projects.py:build_dashboard_projects_report`):
   1. **Star Project** — projects carrying the configured label (default
      `"Star Project"`).
   2. **Other projects** — projects *not* carrying that label, but with a
      start or target date in the current calendar quarter (e.g. "Q3
      2026") — surfaces what else is planned/landing this quarter beyond
      the labeled set. A project matching both is only ever shown once,
      under "Star Project".

   Each project card's milestones table only shows five canonical lifecycle
   milestones - Product: Define, Design: Shape, Design: Refine, Early
   Access, Public Launch (`product_status/static/app.js:KEY_MILESTONE_NAMES`,
   mirrored in `product_status/notion_report.py:KEY_MILESTONE_NAMES`) - in
   that fixed order, regardless of due date. Any other milestone the
   project has is left out of the table entirely. Matching is fuzzy
   (case/punctuation/whitespace-insensitive, e.g. "product define" or
   "PRODUCT - DEFINE (v2)" both match "Product: Define") so small naming
   variations in Linear don't cause a milestone to be dropped.

   Below the milestones, each card shows the project's **last update**
   pulled from Linear's own project updates (author, health -
   On track/At risk/Off track, colored like the status badges - date, and
   the update's body text). `ProjectUpdate.body` is markdown; since neither
   the dashboard nor the Notion export renders markdown, embedded images
   are shown as `[image]` and link/emphasis syntax is stripped down to
   plain text (`product_status/projects.py:_clean_update_body`) so raw
   syntax doesn't leak through. If a project has multiple updates, the one
   with the latest `createdAt` (from the last 10 fetched) is used - but only
   if it's from the last 2 weeks; anything exactly 2 weeks old or older is
   treated as if there's no update at all, rather than showing a stale one
   (`product_status/projects.py:LATEST_UPDATE_MAX_AGE`).
2. **Quality** — SLA/bug health for issues carrying the workspace "Bug"
   label (`product_status/quality.py`), shown in this order:
   1. **SLA Quality Total** *(bold, scored)* — sum of the next two rows.
   2. **Currently Out of SLA** — Bug-labeled issues still open past their
      SLA deadline (Linear's `slaStatus: Breached`). For Progress
      specifically, bugs also carrying the `gurobi-solves` label are
      excluded from this row only
      (`product_status/quality.py:CURRENTLY_OUT_OF_SLA_EXCLUDED_LABELS`).
   3. **Failed SLA This Month** — Bug-labeled issues closed (completed or
      canceled) this calendar month after already breaching their SLA
      (`slaStatus: Failed`).
   4. **Incoming Bugs with High or Urgent priority this month** *(bold,
      scored)* — Bug-labeled issues created this calendar month with
      Urgent or High priority.

   Only Urgent/High priority bugs carry an SLA in this workspace, so rows 2
   and 3 implicitly cover just those; "this month" is the current UTC
   calendar month.

   **Scoring:** rows 1 and 4 — the two "main goal" rows — are each shown
   against a per-team limit and colored green (within limit) or red (over
   limit). Progress, Plan, and Integration — the higher-volume squads — get
   a limit of **10**; every other squad gets **5**
   (`product_status/quality.py:HIGH_VOLUME_QUALITY_TEAMS`/
   `HIGH_VOLUME_QUALITY_THRESHOLD`/`DEFAULT_QUALITY_THRESHOLD`).
3. **Current sprint** — a status-totals summary line (issue count per
   status, summed across assignees), then a table of assignees with total
   issues and status breakdown.
4. **Previous sprint** — a totals summary line (assigned / completed /
   moved to next / removed / added mid-cycle, summed across assignees),
   then a table of the same counts broken out by assignee.

Unlike the endpoints above, the dashboard is backed by an **on-disk cache**,
with one cache file per squad (`.cache/dashboard-squad-<key>.json`) plus one
for the team list (`.cache/dashboard-teams.json`), so each squad refreshes
independently and the cache persists across server restarts. Loading the
page only queries Linear for a squad if that squad's cache is missing,
older than 24h (`DASHBOARD_CACHE_MAX_AGE_SECONDS`), or was written by an
older cache schema `version` (see below); otherwise it's served instantly
from disk. Each squad has its own **Update** button next to its header to
force a fresh pull for just that squad, regardless of age — this hits
`POST /api/dashboard/refresh/{team_key}` under the hood (`GET
/api/dashboard` is the read path the page itself uses on load, and `POST
/api/dashboard/refresh` force-refreshes every squad at once for
scripting/automation use).

**Cache schema versioning:** because the on-disk cache has no schema of its
own, changing what `build_squad_data` returns (e.g. adding a new field)
would otherwise be silently masked by a same-day cache hit for up to 24h.
`product_status/dashboard.py:SQUAD_CACHE_VERSION` guards against this - bump
it whenever `build_squad_data`'s shape changes, and every squad's cache is
treated as stale on the next load regardless of age (see
`cache.get_or_refresh`'s `version` parameter).

### Publish to Notion

The **Publish to Notion** button in the top bar (`POST
/api/dashboard/publish-notion`) exports the currently cached dashboard as a
new Notion page titled `Product Ops <date>` (`<date>` is today's date in
Pacific time - `product_status/notion_report.py:_PACIFIC` - not UTC), created
as a sub-page of the workspace's [Product Ops Reports](https://app.notion.com/p/stellic/Product-Ops-Reports-3be9dd09f473806c875bc8356f5c71a4)
page. It uses whatever's already cached per squad rather than forcing a
fresh Linear pull - hit a squad's own Update button first if you want the
export to reflect the very latest data.

The **Skip sprint data** checkbox next to the button (sent as the
`skip_sprint_data` query param) omits each squad's Previous Sprint section
entirely and reduces Current Sprint to just its heading plus a commentary
callout - useful for weeks where sprint stats aren't the focus of the
write-up.

Structure of the generated page (`product_status/notion_report.py`):

```
Product Ops <date>                    (page)
  <Team name>                         (bulleted link, one per squad - table of contents)
  ...
  <Team name>                         (heading_2, one per squad)
    Projects                          (heading_3)
      Star Project (Projects with label "<label>")  (bold paragraph)
        <project name> (<status>)    (toggle, one per project - status shown
                                       in the title, visible without expanding)
          status  ·  dates
          Milestone / Due Date / Status table (canonical milestones only, see below)
          📁 Last update by <author> · <date> · <health> - <body>   (callout)
          💡 Additional commentary from PL/TL/Designer:     (callout - for manual notes)
      Other projects (<quarter>)      (bold paragraph)
        ...same per-project structure, for projects not labeled "For
           Summit" but starting/due in the current quarter...
    Quality                           (heading_3)
      ...same 4-row Metric/Value/Goal table, colored by threshold...
      💡 Additional commentary from PL/TL/Designer:
    Current Sprint                    (heading_3)
      ...status summary line, then the same per-assignee table...
      💡 Additional commentary from PL/TL/Designer:
    Previous Sprint                   (heading_3)
      ...status summary line, then the same per-assignee table...
      💡 Additional commentary from PL/TL/Designer:
```

Section content sits as plain siblings after its `heading_3` rather than
nested under it - Notion headings can't have children unless they're
*toggleable* headings. The table of contents at the top only links to each
squad's `heading_2` (not the `heading_3` sub-sections): since Notion's
built-in table-of-contents block can't be filtered by heading level, it's
built manually - one bulleted item per squad, created as a placeholder and
then patched with a `#<block-id>` link once that squad's `heading_2` block
exists.

This calls the Notion REST API directly (`product_status/notion_client.py`,
`notion_oauth.py`) - it does not use Notion MCP tools, since this runs from
the FastAPI backend in response to a button click, not from an agent
session.

Notion's public API has no property for a page's "Full width" layout
setting - that's a display preference only exposed in the Notion UI, so
after publishing you'll need to toggle it on by hand once per page (page
"•••" menu → **Full width**).

**Setup - OAuth (recommended; no Notion "workspace owner" permission
needed):** creating an *internal* integration under Settings → Connections
requires being a workspace owner in Notion. A *public connection* avoids
that entirely - any member can authorize one for just the page(s) they
personally have access to, via a normal OAuth consent screen.

1. Create a connection at <https://www.notion.so/my-integrations> and
   switch its type to **Public** (Notion's Developer Portal calls this a
   "public connection").
2. Set its redirect URI to `http://localhost:8008/notion/oauth/callback`
   (or whatever port you run the server on). Notion rejects literal IP
   addresses here (e.g. `127.0.0.1` → *"Redirect URIs can't use IP
   addresses"*) but explicitly allows `localhost` for local development -
   no tunneling (ngrok) or deployment (Vercel, etc.) needed.
3. Copy its **OAuth Client ID** and **Client Secret** into `.env` as
   `NOTION_OAUTH_CLIENT_ID` / `NOTION_OAUTH_CLIENT_SECRET` (see
   `.env.example`). Optionally set `NOTION_OAUTH_REDIRECT_URI` if it
   differs from the default above.
4. Restart the server, click **Connect to Notion** in the dashboard's top
   bar, and on Notion's consent screen pick the
   [Product Ops Reports](https://app.notion.com/p/stellic/Product-Ops-Reports-3be9dd09f473806c875bc8356f5c71a4)
   page (or its parent) to share. The button then becomes **Publish to
   Notion**, and the resulting access token is stored at
   `.cache/notion_oauth_token.json` (gitignored) so you only authorize once.
   A **Disconnect** link next to the button lets you re-authorize (e.g. a
   different workspace) later.

**Setup - internal integration (alternative, only if you *do* have
workspace-owner permission):** create an internal integration instead,
share the target Notion page with it via its "•••" menu → **Connections**,
and set `NOTION_API_KEY=secret_...` in `.env`. If both an OAuth connection
and `NOTION_API_KEY` are present, the OAuth connection takes priority.

Without either configured, the dashboard shows **Connect to Notion**, and
clicking it (or attempting to publish) surfaces a clear setup error instead
of failing silently.

## Project layout

```
product_status/
  config.py         # API key + team filter resolution (.env aware)
  linear_client.py   # raw GraphQL client: auth, retries, pagination, aliasing
  cycles.py          # team + cycle (activeCycle / isPrevious) lookups
  issues.py           # cycle issue scope, uncompletedIssuesUponClose, mid-cycle detection via issue.history
  report.py          # assembles the per-team, per-assignee report
  projects.py         # project summaries (status, dates, milestones) for a given project label
  quality.py           # per-team SLA/bug counts (out of SLA, failed SLA, incoming high/urgent bugs)
  dashboard.py         # combines sprints + summit projects + quality, grouped by squad, for the web UI
  cache.py             # on-disk JSON cache keyed by age (used by the dashboard, 24h default)
  notion_client.py      # raw Notion REST API client (auth, retries, nested block creation)
  notion_oauth.py       # Notion OAuth ("public connection") flow - no workspace-owner permission needed
  notion_report.py      # builds the "Product Ops <date>" Notion page from dashboard data
  cli.py             # command-line entry point (rich tables or --json)
  server.py           # FastAPI service for on-demand HTTP queries + the dashboard
  static/              # dashboard web UI (plain HTML/CSS/JS, no build step)
```

## Notes

- Every GraphQL connection is fully paginated (`pageInfo.hasNextPage` /
  `endCursor`) — no result is silently truncated.
- Requests are retried with exponential backoff on rate limits (`429`) and
  `5xx` responses.
- `issue.history` lookups are batched using GraphQL aliases (20 issues per
  request) to keep query counts low for large teams.
