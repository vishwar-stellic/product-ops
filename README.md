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
  - **Canceled** = issues with `state.type == "canceled"` or `"duplicate"` -
    both are terminal, non-completed outcomes. These are checked before
    falling back to `uncompletedIssuesUponClose` below, since a ticket
    canceled *during* the cycle never appears in that connection (it only
    covers issues that were still open when the cycle closed) - without
    this bucket, such tickets would count toward "Total assigned" but show
    up in none of the other columns, so `Completed + Canceled + Moved to
    next + Removed` wouldn't add back up to "Total assigned".
  - **Moved to next sprint** = issues from `uncompletedIssuesUponClose` whose
    *current* `cycle.id` now equals the team's active cycle.
  - **Removed from cycle** = everything else in `uncompletedIssuesUponClose`
    (was still open at close, but didn't roll into the active cycle either).
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

With the HTTP service running, open **http://127.0.0.1:8008/** for the
dashboard (with its own favicon, `product_status/static/favicon.svg`). The
tab bar just below the header (`product_status/static/index.html`) splits
the site into separate capabilities, each its own tab:

- **EPD Report** — everything described below: squads, projects, quality,
  sprints, and Publish to Notion.
- **Sprint Report** — one section per squad with **Current sprint** /
  **Previous sprint** sub-tabs (`product_status/static/app.js:renderSprintReportTeam`),
  plus its own "Publishes to"/**Publish to Notion** bar (see "Publish to
  Notion" below). Reuses the same per-squad data already loaded for the EPD
  Report tab (`state.squadsByKey`), so it needs no separate fetch or cache.
  Each sub-tab shows one table with a row per team member:
  - **Current sprint** — **Assigned**, **Completed**, and **Added
    mid-cycle** counts. There's no "moved to next"/"removed" breakdown here
    since the cycle's still in progress (`renderSprintReportAssigneeTable`).
  - **Previous sprint** — the same breakdown as the EPD Report's Previous
    Sprint table: **Assigned**, **Completed**, **Canceled**, **Moved to
    next**, **Removed**, and **Added mid-cycle** - every assigned ticket
    that *didn't* get completed is accounted for as either canceled, moved
    into the next cycle, or removed from the cycle entirely, not just
    folded into "Assigned" with no further breakdown
    (`renderPreviousSprintReportAssigneeTable`). "Canceled" also covers
    Linear's separate "duplicate" state - see "How it determines each
    bucket" above.

  "Added mid-cycle" comes from the same per-issue history walk used for the
  EPD Report's Previous Sprint section (see
  `product_status/issues.py:fetch_added_during_cycle`), just applied to the
  active cycle too for the Current sprint sub-tab
  (`product_status/report.py:build_current_sprint`).
- **Project Milestones** — every project *starting* in the current
  calendar quarter, plotted on one shared timeline (one row per project,
  sorted by start date, milestones as dots positioned by target date). The
  project name column (full names, never truncated — they wrap instead)
  stays docked on the left (`position: sticky`) while the dated track
  scrolls independently to the right — granular weekly gridlines, hidden
  scrollbar but still scrollable via trackpad swipe or shift+scroll, so a
  plain vertical scroll over the timeline still scrolls the page as
  expected. On top of the timeline sits an **Overloaded people** callout:
  anyone who owns multiple milestones — across *different* projects —
  landing within 7 days of each other. A project with no dated milestones
  this quarter still gets an (empty) row rather than disappearing; a
  canonical milestone with no target date set can't be plotted at all, so
  it's listed out inline under that project's name instead ("⚠ Missing
  dates: ..."). Milestone ownership is derived from the Linear issues
  linked to that milestone rather than a single fixed "project owner",
  since different roles own different canonical milestones (Product Lead
  for *Product: Define*, Designer for *Design: Shape*/*Design: Refine*,
  Eng Lead for *Early Access*/*Public Launch*); a milestone with no linked
  issues (and no applicable fallback) shows as "Unassigned" rather than
  guessing. See `product_status/milestones_report.py`'s module docstring
  for the full design. Lazy-loaded on first visit to the tab, cached like
  the other tabs (24h, with its own **Update** button to force a refresh).
  Currently hidden from the tab bar (code kept intact) - remove the
  `hidden` class in `index.html` to bring it back.
- **Support Report** — the "5 metrics" from Stellic's `support-sla-dashboard`
  Claude Skill (Total open Key User tickets, New/Closed this week, Out of
  first-response SLA, Out of resolution SLA), computed live from Intercom
  rather than that skill's Notion-maintained register - a **Total** column
  first, then one (centered) column per squad in the order Progress, Plan,
  Platform, Integration, Care, Explore - no per-PDL breakdown (that needs
  the skill's manually uploaded Vitally CSV, which this service doesn't
  have) and no Dev-ex column (it has no customer-facing Intercom area, so
  it'd always read "—"). Requires `INTERCOM_ACCESS_TOKEN` (see
  `.env.example`); see `product_status/support_report.py`'s module
  docstring for exactly how each metric is derived (business-hours-aware
  first-response clock, high-priority-only resolution SLA, "this week" as a
  **calendar week-to-date** counter that resets every Monday (Pacific time)
  rather than a rolling trailing-7-days window - matters once this report
  runs on a schedule, since each day's snapshot should reflect that day's
  actual progress through the week, not a smeared trailing average - and
  why some tickets need an extra API call to confirm they were never
  actually replied to). Click any metric row to drill into the underlying
  tickets
  (across every squad) in a table below - Date Created, First Response
  SLA, Last Update, **User Name** (the individual requester) plus a
  separately-resolved **Partner Name** (the institution - company name,
  else a contact's `external_id` code looked up against Intercom's company
  list, else an email-domain fallback map), Priority, and a Ticket
  Description linking to Intercom - each column independently filterable;
  the "out of SLA" rows are just a client-side filter over the same
  open-ticket list, so no extra fetch is needed to drill into them
  (`product_status/static/app.js:renderSupportReportDrilldown`). This is
  the slowest tab to refresh - a full pull takes roughly a minute - so the
  **Update** button warns about that; lazy-loaded and cached (24h) like the
  other tabs. The first load of the day (or right after a cache-version
  bump) has to block on that same ~1-2 minute pull with nothing sent back
  in the meantime, which is long enough that some browsers/networks give up
  on the connection and throw a network-level `fetch` error even though the
  server keeps working and the cache still gets written - so a plain
  `fetch` failure here (not an HTTP error) auto-retries with backoff rather
  than surfacing an error immediately
  (`product_status/static/app.js:loadSupportReport`). Above the table sits
  a **Trend** chart - one line per metric row, one point per *actual*
  refresh (not per page view), with a radio picker (Total, or any one
  squad) for which column each of those 5 lines plots - defaults to Total
  across all squads. Every real refresh appends a snapshot (every column's
  value, not just whichever one happens to be selected at the time) to a
  small history log stored alongside the main cache (`GET
  /api/support-report/history`; see `support_report.py`'s "Trend history"
  docstring section), so the chart fills in gradually over time rather than
  needing a backfill; it shows a placeholder until at least two points
  exist.
- **Partner Insights** — one row per partner institution *matched to a
  Vitally account* (see the "Partners" bullet below) with a **Bug score**
  (out of 100) and **Live Fire** / **Smoldering** escalation counts.
  **Feature score** is computed the same as Bug score but hidden from
  this main table — it's still shown in each partner's expanded-row
  breakdown (see below); only `PARTNER_INSIGHTS_SORT_COLUMNS`/the main
  `<tr>` in `app.js` needed to change to hide it, `partner_insights.py`
  still computes and returns it. Unlike every other tab, this one is
  hidden from the tab bar entirely unless the signed-in user's email is on
  `PARTNER_INSIGHTS_ALLOWED_EMAILS` (see "Partner Insights access" below) —
  most of the team doesn't need per-partner scoring visible.
  - **Partners** come from `product_status/partner_identity.py`'s
    `build_partner_registry` (every Intercom company cross-referenced
    against Linear's `Customer`/`CustomerNeed` objects and, optionally,
    Vitally's `Account` objects — see that module's docstring for the
    matching logic), but `build_partner_insights_report` then filters that
    list down to **only partners with a matched Vitally account** before
    it ever reaches the table — every remaining column (Bug/Feature score,
    Live Fire/Smoldering) is Linear- or Vitally-sourced, so a partner
    Vitally doesn't know about would only ever show empty cells. A ⚠ next
    to the name still flags a partner with no matched Linear customer
    (Escalations-only, "not linked" for Bug/Feature score).
  - **Bug score** and **Feature score** (Linear only, recomputed on every
    refresh, no LLM) — every Linear issue linked to a partner via a
    `CustomerNeed` is split into Bug-labeled vs. feature request/other, each
    scored independently rather than folded into one number:
    - **Bug score** — bug-SLA responsiveness: `100 × (1 − (currently-out-
      of-SLA + failed-this-month) / SLA-eligible bugs)`, defaulting to 100
      when a partner has no SLA-eligible (Urgent/High) bugs at all.
      "SLA-eligible bugs" only counts bugs still in an open workflow state
      (Backlog/Todo/In Progress/In Review/Triage) — a bug that already
      closed cleanly within SLA isn't part of today's open workload.
      "Failed this month" is the deliberate exception: it's inherently
      about bugs that already closed (missed SLA before closing), so it
      keeps counting closed bugs regardless of that open-state rule.
    - **Feature score** — a staleness proxy (feature requests have no
      formal SLA): `100 × (1 − (open >90 days, still unresolved) / total
      *open* feature requests/other)`, defaulting to 100 when a partner has
      none open.

    A partner with no linked Linear customer shows "not linked" for both
    instead of a score. See `product_status/partner_insights.py`'s module
    docstring (and `_product_metrics_for_customer`'s inline comments for
    exactly which row is open-state-only vs. all-statuses — it's a
    deliberate per-row product decision, not a blanket rule).
    - Every raw count in the expanded breakdown (total bugs, currently
      out-of-SLA, failed-SLA-this-month, SLA-eligible, total/new/stale
      feature requests) is clickable — it opens the exact matching Linear
      issues as an ad-hoc list view (`linear.app/<workspace>/issues/ID-1,
      ID-2,...`, the same URL scheme Linear itself uses for "open these
      issues together"). Zero-count cells aren't clickable. "Total" counts/
      links are open-state-only (matching the score denominators above);
      "new this month" counts/links intentionally include every status.
  - **Live Fire** / **Smoldering** (Vitally conversations, triaged by an LLM) —
    counts of that partner's currently-tracked escalation items at each
    severity, from triaging that partner's recent *human-written* email
    (Gmail/Outlook) and Intercom conversations — both mirrored into Vitally,
    see `product_status/vitally_client.py` — against a fixed risk-triage
    prompt (see `product_status/escalation_report.py`'s module docstring for
    the exact prompt and design, including why Intercom conversations are
    included despite the removed Support score also having used Intercom).
    A plain `-` for a genuine zero at that severity, or `not in Vitally`/
    `not configured` when there's no escalation data at all for that
    partner. A third severity, **Watch**, doesn't get its own column (lower
    signal) but is still visible in the expanded row.
    - Needs *both* `VITALLY_ACCESS_TOKEN` (the conversation source) and
      `OPENAI_API_KEY` (the triage, via `product_status/openai_client.py`
      — OpenAI's `us.api.openai.com` regional/US-data-residency endpoint by
      default) — either missing shows "not configured" for everyone.
    - Before anything reaches the LLM, calendar invites/responses and
      out-of-office auto-replies are dropped mechanically (these dominate
      Vitally's Gmail-synced conversation volume) — subtler auto-generated
      content (newsletters, marketing, recruiting, system alerts) is left to
      the LLM's own judgment per the prompt's SCOPE section.
    - A message counts as partner-authored either when Vitally's own
      `type` field says so, or when its sender resolves to one of that
      conversation's own external contacts — Vitally's `type` field turns
      out to mislabel some genuinely partner-authored Gmail-synced replies
      as "outbound" (confirmed against live data), so it isn't trusted
      alone. See `escalation_report.py`'s `_is_partner_authored`.
    - **Incremental, and only on a forced refresh** — unlike Bug/Feature
      score, this never runs on a passive 24h cache-age refresh, only the
      **Update** button. Each run only fetches emails newer than the last
      run's newest processed email (capped at a 3-day lookback), hands them
      to the LLM *alongside* the currently-tracked items, and asks it to
      adjust (add/update/drop) rather than re-derive the list from scratch —
      a partner with no new eligible email since last time costs nothing.
      "Days since last movement" is computed live on every page load from
      each item's `lastMovementAt`, not a number that goes stale between
      runs.
    - Click a partner to see every tracked item's full breakdown (a
      findings table plus one detail card per item — headline, severity +
      why, 1-2 quoted evidence lines with sender/date, who's blocked on
      whom, days since last movement, and the triggering email's
      from/subject/date), plus a best-effort "Open account in Vitally"
      link when `VITALLY_APP_SUBDOMAIN` is set (Vitally's API doesn't
      expose a direct link back to the original thread, only the account
      page) — **and** a "Recent emails analyzed" section listing the raw
      source emails (subject/from/date, expand for the full body) the
      latest batch actually looked at, from `escalations.recentEmails`
      (see `escalation_report.py`'s module docstring) — not just the 1-2
      quotes per item the triage prompt happens to pull out.
  - Click a partner row to expand it in place (an extra row directly below
    that partner, not a separate panel at the bottom of the table) with the
    full breakdown — the Bug/Feature metrics and the Escalations block
    above.
  - Cached the same way as the other tabs (24h, own **Update** button to
    force a refresh) — a forced refresh also re-runs escalation triage
    for every partner with new eligible email, so it's slow (Linear pull +
    Vitally pull + an LLM call per partner with new email).
  - Each row also has its own small ⟳ **Update** button (rightmost
    column) to force a refresh for *just that partner* —
    `POST /api/partner-insights/refresh/{partner_id}` /
    `partner_insights.refresh_single_partner` — instead of the whole
    roster: one Linear pull plus at most one LLM triage call, versus one
    LLM call per partner with new email for the top-of-tab Update button.
    It patches the already-cached full report in place (`cache.peek` /
    `cache.write_raw` on `PARTNER_INSIGHTS_CACHE_KEY`) so the change is
    visible on a normal page load too, not just the tab that triggered it.
  - An earlier version of this tab also had a Support score column,
    scoring Intercom conversations with an LLM via a daily batch job. That
    was removed entirely (not just hidden) when it was dropped in favor of
    Escalations — see git history (`compute_support_scores` et al. in
    `partner_insights.py`) if it's ever needed again.

#### Partner Insights access

The Partner Insights tab needs Google sign-in (see below) *and* an explicit
allowlist — set `PARTNER_INSIGHTS_ALLOWED_EMAILS` in `.env` to a
comma-separated list of emails (e.g. `alice@stellic.com,bob@stellic.com`).
Anyone not on that list never sees the tab button, and the underlying
`/api/partner-insights*` routes 403 them directly (the tab button being
hidden is just UX — the real boundary is server-side, see
`product_status/server.py:_require_partner_insights_access`). If Google
sign-in itself isn't configured, this check is skipped too, so the tab is
open to everyone locally by default — same "open for local dev" behavior as
the rest of the app.

#### Signing in

The dashboard (page and every `/api/*` route) can be gated behind Google
sign-in, restricted to one email domain — see `product_status/auth.py`'s
module docstring for full setup. In short:

1. Create an OAuth 2.0 Client ID ("Web application") at
   https://console.cloud.google.com/apis/credentials. If it belongs to the
   Stellic Google Workspace, setting the consent screen's "User type" to
   *Internal* restricts sign-in to `@stellic.com` accounts at Google's own
   login screen, on top of the server-side domain check below.
2. Add an authorized redirect URI for every host this runs on (e.g.
   `http://localhost:8008/auth/google/callback` locally, plus your deployed
   URL's `/auth/google/callback`).
3. Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
   `SESSION_SECRET` (any long random string) in `.env` — see
   `.env.example`. `ALLOWED_EMAIL_DOMAIN` defaults to `stellic.com`.

Leaving `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` unset keeps
the app open (no login required) — the default, so local dev works without
setting up Google credentials first.

The session itself is a signed, stateless cookie (7-day expiry) rather than
a server-side session store, since nothing survives between requests on a
serverless host (see "Deploying" below) — no session backend needed, and it
works identically locally and on Vercel. Signed-in users see their name and
a **Sign out** link in the top-right of the header.

The **EPD Report** tab is organized as one section per squad — Progress,
Plan, Care, Explore, Platform, Integration, and DevX
(`product_status/dashboard.py:DASHBOARD_TEAMS`) — in that order, regardless
of what other teams exist in the Linear workspace.

Each squad's header stays docked just below the top bar + tab bar while you
scroll through that squad's section (`position: sticky`, offset by their
combined measured height - see
`product_status/static/app.js:syncTopbarHeight`), so you always know which
team's data you're looking at; scrolling past the section lets the next
squad's header take over. Click a squad's header to collapse/expand its
section. Each squad's section has, in order:

1. **Projects** — linked to that squad's team in Linear, each with its
   milestones (target date + done/not done — completed milestones are
   shown with a filled checkmark, not struck through). A project shared
   across multiple teams (e.g. also shared with "Docs") shows up under
   every squad it's linked to, not just one. Split into two groups
   (`product_status/projects.py:build_dashboard_projects_report`):
   1. **Star Project** — projects carrying the configured label (default
      `"Star Project"`).
   2. **Other Projects** — projects *not* carrying that label, but with a
      start or target date in the current calendar quarter. A project
      matching both is only ever shown once, under "Star Project". Hidden
      entirely when the **Only Star Projects** checkbox (see below) is
      checked; shown otherwise (collapsed under an "Other Projects" toggle
      heading in the Notion export, immediately followed by an "Other
      Asks" toggle heading - see "Publish to Notion" below).

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
   On track/At risk/Off track, colored like the status badges - date (with
   how long ago it was posted, e.g. "Aug 12 (3 days ago)"), and the
   update's body text). `ProjectUpdate.body` is markdown; since neither
   the dashboard nor the Notion export renders markdown, embedded images
   are shown as `[image]` and link/emphasis syntax is stripped down to
   plain text (`product_status/projects.py:_clean_update_body`) so raw
   syntax doesn't leak through. If a project has multiple updates, the one
   with the latest `createdAt` (from the last 10 fetched) is used - but only
   if it's from the last 2 weeks; anything exactly 2 weeks old or older is
   treated as if there's no update at all, rather than showing a stale one
   (`product_status/projects.py:LATEST_UPDATE_MAX_AGE`).
2. **Quality** — SLA/bug health for issues carrying the workspace "Bug"
   label (`product_status/quality.py`), preceded by two definitions
   (`product_status/notion_report.py:_QUALITY_DEFINITIONS`, mirrored on the
   web page) and then a table shown in this order:
   1. **SLA Quality Total** *(bold, scored)* — sum of "Currently Out of
      SLA" and "Failed SLA This Month".
   2. **Currently Out of SLA** — open bugs that have breached SLA
      (Linear's `slaStatus: Breached`). For Progress specifically, bugs
      also carrying the `gurobi-solves` label are excluded from this row
      only (`product_status/quality.py:CURRENTLY_OUT_OF_SLA_EXCLUDED_LABELS`).
   3. **Failed SLA This Month** — bugs that were fixed this month after
      they had breached their SLA (closed - completed or canceled - this
      calendar month with `slaStatus: Failed`).
   4. **Currently Active High Bugs** — currently open Bug-labeled issues
      with High priority.
   5. **Number of bugs because of missing tests** — no automatic data pull
      for this row; the Value column is always left blank, filled in by
      hand.
   6. **Incoming Bugs with High or Urgent priority this month** *(bold,
      scored)* — Bug-labeled issues created this calendar month with
      Urgent or High priority.

   Only Urgent/High priority bugs carry an SLA in this workspace, so rows 2
   and 3 implicitly cover just those; "this month" is the current UTC
   calendar month.

   **Scoring:** rows 1 and 6 — the two "main goal" rows — are each shown
   against a per-team limit and colored green (within limit) or red (over
   limit). Progress, Plan, and Integration — the higher-volume squads — get
   a limit of **10**; every other squad gets **5**
   (`product_status/quality.py:HIGH_VOLUME_QUALITY_TEAMS`/
   `HIGH_VOLUME_QUALITY_THRESHOLD`/`DEFAULT_QUALITY_THRESHOLD`).
3. **Current sprint** and **Previous sprint** — still fetched per squad
   (issue counts and assignee breakdowns), but always hidden: the section
   collapses to just the "Current sprint" heading plus a "Sprint data
   hidden." note, with no checkbox to reveal it (`showSprintData` is a
   permanent `false` in `product_status/static/app.js`). This mirrors the
   Notion export, which never includes sprint data either - see "Publish to
   Notion" below.

**EPD Report toolbar checkboxes** — these control the web view's own
rendering (re-rendering everything already loaded, no refetch needed), and
**Only Star Projects** is also sent to **Publish to Notion** as the
`only_star_projects` query param (`product_status/static/app.js:renderAll`,
mirrored in `product_status/notion_report.py:build_team_blocks`); **Demo
run** only affects the Notion export:
- **Only Star Projects** *(unchecked by default)* — check to hide every
  non-"Star Project" project; unchecked (the default) also shows every
  other current-quarter project, under an "Other Projects" group (both on
  the web page and, collapsed into a toggle heading, in the Notion
  export).
- **Demo run** *(unchecked by default)* — only affects **Publish to
  Notion**: check to only publish the "Progress" squad (`?demo_run=true` on
  `/api/dashboard/publish-notion` - `product_status/server.py`), instead of
  every squad on the dashboard. Handy for trying out the export without
  writing every team's data to Notion.

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

Both the **EPD Report** and **Sprint Report** tabs have their own
"Publishes to" bar right at the top, showing which Notion page the tab's
**Publish to Notion** button will create a sub-page under, with an **Edit**
link to point it somewhere else and a **Reset to default** option
(`.notion-target-bar` in `product_status/static/index.html`/`app.js`).
Both default to the workspace's [Product Ops Reports](https://app.notion.com/p/stellic/Product-Ops-Reports-3be9dd09f473806c875bc8356f5c71a4)
page (`notion_report.DEFAULT_PARENT_PAGE_URL`, surfaced to the frontend via
`defaultParentPageUrl` on `GET /api/notion/status`) unless overridden -
overrides are per-tab and stored in the browser's `localStorage`, not on
the server, so pointing the Sprint Report export somewhere else (e.g. a
scratch page while testing) doesn't affect the EPD Report export or
persist across browsers/devices. Whatever's showing in a tab's bar is sent
as `?parent_page_url=...` on that tab's publish request.

The **Publish to Notion** button in the EPD Report tab (`POST
/api/dashboard/publish-notion`) exports the currently cached dashboard as a
new Notion page titled `EPD Report <date>` (`<date>` is today's date in
Pacific time - `product_status/notion_report.py:_PACIFIC` - not UTC). It
uses whatever's already cached per squad rather than forcing a fresh Linear
pull - hit a squad's own Update button first if you want the export to
reflect the very latest data.

Sprint data is always omitted from the export (`skip_sprint_data=true` is
always sent - there's no checkbox for it, see "Current sprint"/"Previous
sprint" above). The **Only Star Projects** checkbox in the EPD Report tab
(see "EPD Report toolbar checkboxes" above) is sent along as the
`only_star_projects` query param, controlling the Notion export as
described below. **Demo run** is sent as `demo_run` and only changes *which
squads* get published (just "Progress" instead of all of them) - it
doesn't change the structure of the page itself.

Structure of the generated page (`product_status/notion_report.py`) with
**Only Star Projects** checked (shows just the labeled set, without the
"Other Projects" toggle - unchecked by default, see "EPD Report toolbar
checkboxes" above). Sprint data (Previous Sprint) is never included - see
below:

```
EPD Report <date>                     (page)
  🤖 Report automatically generated from Linear...   (callout - standing
                                        disclaimer, shown once at the top)
  <Team name>                         (bulleted link, one per squad - table of contents)
  ...
  <Team name>                         (heading_2, one per squad)
    Projects with label "<label>"  Learn more   (heading_3 - title and
                                        content shift based on Only Star
                                        Projects; when unchecked, heading
                                        reverts to "Projects", the Star
                                        Project set is still shown directly,
                                        and every other current-quarter
                                        project is collapsed under an
                                        "Other Projects" toggle heading.
                                        "Learn more" links to the team's
                                        guidelines doc)
        <project name> (<status>, Last Update: <health>)   (toggle, one per
                                       project - status and latest update's
                                       health both shown in the title,
                                       visible without expanding)
          status  ·  dates
          Milestone / Due Date / Status table (canonical milestones only, see below)
          📁 Last update by <author> · <date> (<n> days ago) · <health> - <body>   (callout)
          💡 Additional commentary from PL/TL/Designer:     (callout, bold - for manual notes)
      Other Projects                    (toggle heading_4 - only when Only
                                          Star Projects is unchecked; same
                                          per-project toggle structure above)
      Other Asks                        (toggle heading_4 - always shown,
                                          right after "Other Projects")
        Asks / Goal / Status/Outcome / Commentary/analysis table - blank,
        filled in by hand
    Quality  Learn more               (heading_3 - "Learn more" as above)
      - Currently out of SLA: Open bugs that have breached SLA.
      - Failed SLA this month: Bugs that were fixed this month after they
        had breached their SLA
      Metric / Goal / Value / Notes table, colored by threshold - "Notes"
      is a blank column for jotting notes directly in the table (no
      separate commentary callout for this section)
    Kudos                             (heading_3 - no "Current Sprint" or
                                        "Previous Sprint" section anymore;
                                        sprint data is never published)
      Name / Contribution / Why it matters table - blank, filled in by hand
    Dependency/Availability  Learn more   (heading_3 - "Learn more" as above)
      (blank paragraph - filled in by hand)
```

Section content sits as plain siblings after its `heading_3` rather than
nested under it - Notion headings can't have children unless they're
*toggleable* headings. "Other Projects" and "Other Asks" are the exception:
they're `heading_4` blocks with `is_toggleable: true` set
(`product_status/notion_report.py:toggle_heading`), so their content
collapses into them the way a regular toggle's would, while still showing
up as a heading in Notion's block hierarchy. The table of contents at the
top only links to each squad's `heading_2` (not the `heading_3`
sub-sections): since Notion's built-in table-of-contents block can't be
filtered by heading level, it's built manually - one bulleted item per
squad, created as a placeholder and then patched with a `#<block-id>` link
once that squad's `heading_2` block exists.

**"Learn more" links** on the Projects, Quality, and Dependency/Availability
headings point at anchors inside the team's "Engineering Reviews: Some
Guidelines" Notion doc (`product_status/notion_report.py:LEARN_MORE_URL_PROJECTS`/
`LEARN_MORE_URL_QUALITY`/`LEARN_MORE_URL_DEPENDENCY`).

**Column widths:** the Notion API has no way to make the Kudos table's
"Contribution"/"Why it matters" columns wider than "Name" - per-column
width on a plain `table` block isn't exposed by the API (only `width_ratio`
for side-by-side page *columns*, and a `width` in pixels for full Notion
*database* views - neither applies to a basic table block). Resize those
columns by hand in the Notion UI after publishing if you want them wider.

**Sprint Report tab's Publish to Notion** (`POST
/api/dashboard/publish-sprint-report`) is a separate, simpler export -
same "Publishes to" bar/override behavior as above, but no
`skip_sprint_data`/`only_star_projects`/`demo_run` options (those are
EPD-Report-only). It creates a `Sprint Report <date>` page mirroring the
web dashboard's Sprint Report tab (`product_status/notion_report.py:publish_sprint_report_to_notion`):

```
Sprint Report <date>                  (page)
  🤖 Report automatically generated from Linear...   (callout)
  <Team name>                         (bulleted link, one per squad - table of contents)
  ...
  <Team name>                         (heading_2, one per squad)
    Current Sprint                    (heading_3)
      Team member / Assigned / Completed / Added mid-cycle table
    Previous Sprint                   (heading_3)
      status summary + Assignee / Assigned / Completed / Canceled / Moved
      to next / Removed / Added mid-cycle table
```

This calls the Notion REST API directly (`product_status/notion_client.py`,
`notion_oauth.py`) - it does not use Notion MCP tools, since this runs from
the FastAPI backend in response to a button click, not from an agent
session.

Notion's public API has no property for a page's "Full width" layout
setting - that's a display preference only exposed in the Notion UI, so
after publishing you'll need to toggle it on by hand once per page (page
"•••" menu → **Full width**).

**Setup - internal integration (recommended, requires Notion
"workspace owner" permission):** create an internal integration at
<https://www.notion.so/my-integrations>, share the target Notion page with
it via its "•••" menu → **Connections**, and set `NOTION_API_KEY=secret_...`
in `.env` (see `.env.example`). This is the simplest and most robust
option - it's just a static token, so it works identically on any host
(serverless included) and doesn't depend on a connect/disconnect flow or a
token file surviving between requests. When set, the dashboard's EPD Report
tab shows **Publish to Notion** directly - no "Connect to Notion" step, and
the Notion status badge reads "Notion: connected (API key)".

**Setup - OAuth (fallback; use only if you *don't* have workspace-owner
permission and can't set `NOTION_API_KEY` above):** creating an *internal*
integration requires being a workspace owner in Notion. A *public
connection* avoids that - any member can authorize one for just the
page(s) they personally have access to, via a normal OAuth consent screen.

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
4. Restart the server, click **Connect to Notion** in the dashboard's EPD
   Report tab, and on Notion's consent screen pick the
   [Product Ops Reports](https://app.notion.com/p/stellic/Product-Ops-Reports-3be9dd09f473806c875bc8356f5c71a4)
   page (or its parent) to share. The button then becomes **Publish to
   Notion**, and the resulting access token is stored at
   `.cache/notion_oauth_token.json` (gitignored) so you only authorize once.
   A **Disconnect** link next to the button lets you re-authorize (e.g. a
   different workspace) later.

If both `NOTION_API_KEY` and an OAuth connection are present,
`NOTION_API_KEY` takes priority (`notion_oauth.resolve_access_token`) - the
OAuth token is only ever used as a fallback for hosts/users without an
internal integration token.

Without either configured, the dashboard shows **Connect to Notion**, and
clicking it (or attempting to publish) surfaces a clear setup error instead
of failing silently.

## Deploying (e.g. Vercel)

`main.py` at the project root re-exports the FastAPI `app` from
`product_status/server.py` purely so Vercel's Python/FastAPI framework
preset can auto-detect it - Vercel only looks for an `app` instance in a
handful of default locations (`app.py`, `index.py`, `server.py`, `main.py`,
`wsgi.py`, `asgi.py` at the repo root or under `src/`/`app/`/`api/`), and
this project's app lives one directory deeper than that. Local development
is unaffected - keep using `uvicorn product_status.server:app`.

Set `LINEAR_API_KEY`, `NOTION_API_KEY` (see "Publish to Notion" above), and
`BLOB_READ_WRITE_TOKEN` (see below) as environment variables in the Vercel
project settings — `.env` is gitignored and won't be deployed. If you're
using Google sign-in ("Signing in" above), also set
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
`SESSION_SECRET` there, and make sure your deployed domain's
`/auth/google/callback` is an authorized redirect URI on the OAuth Client
ID - a redeploy is needed after adding/changing env vars for them to take
effect. For the **Support Report** tab, also set `INTERCOM_ACCESS_TOKEN`
(see `.env.example`). Note `vercel.json` sets `maxDuration: 300` on
`main.py` - a full Support Report refresh takes roughly a minute (several
whole-workspace Intercom pulls, run concurrently - see
`support_report.py`'s module docstring), well past Vercel's old 60s
default.

**Dashboard cache (fixed via Vercel Blob):** `product_status/cache.py`
writes to `config.CACHE_DIR`, which defaults to `.cache/` next to the code
- fine for a normal long-running process, but serverless platforms ship a
read-only filesystem except for `/tmp`, which is itself wiped on cold
starts and not shared across scaled-out instances, so nothing written
there actually persists between requests on Vercel. To fix this, add
**Vercel Blob** to the project (Vercel dashboard → Storage → Create →
Blob), which sets `BLOB_READ_WRITE_TOKEN` for you automatically; once
that's set, `cache.py` transparently stores the same `{fetchedAt, data,
version}` cache entries there instead of on disk (`product_status/blob_cache.py`)
- Blob storage does persist across invocations/deployments, so the 24h
cache actually holds and the dashboard/Sprint Report only re-queries Linear
when it's genuinely stale or you hit Update. Locally (or on any host with a
real persistent disk), leave `BLOB_READ_WRITE_TOKEN` unset and the on-disk
cache is used as before - override its location with
`PRODUCT_OPS_CACHE_DIR` if needed.

`blob_cache.py` talks to Vercel Blob's HTTP API directly with `requests`
(reverse-engineered from the open-source JS SDK - see that module's
docstring) rather than the official `vercel` Python package, since that
package requires Python ≥ 3.10 and this project targets 3.9. Every
operation is best-effort: any failure (misconfigured token, network error,
Blob outage) falls back to "just refetch from Linear," never a crash - see
that module for details.

**Notion OAuth token:** if you're using the OAuth fallback instead of
`NOTION_API_KEY` (see "Publish to Notion" above), note that
`notion_oauth.py`'s token file has the same `CACHE_DIR` persistence caveat
described above and isn't covered by the Vercel Blob fix yet - it'll ask
you to reconnect more often on Vercel than it would on a normal host.
`NOTION_API_KEY` doesn't have this problem (it's just a static env var), so
prefer that on Vercel.

**Nightly Support Report refresh (Vercel Cron):** `vercel.json` registers
a cron job that hits `GET /api/cron/refresh-support-report` once a night
(`"schedule": "0 7 * * *"`, i.e. 07:00 UTC = midnight Pacific during
daylight saving - Vercel Cron schedules are always UTC with no DST
adjustment, so this drifts to 11pm Pacific for the few months a year it's
on standard time; adjust the schedule if you want it pinned exactly). This
just forces the same slow full pull as the **Update** button
(`build_support_report()`, a whole-workspace Intercom pull), so the
dashboard's cache is already warm before anyone opens it that day rather
than the first visitor of the day eating the ~1-2 minute cold-pull penalty
described above. Secure it by setting `CRON_SECRET` (see `.env.example`)
as a Vercel env var - Vercel automatically sends it back as an
`Authorization: Bearer <CRON_SECRET>` header on every cron invocation,
which `_require_cron_secret` in `server.py` checks; leave it unset locally
(same "open for local dev" pattern as the rest of the app), but always set
it in production so the endpoint can't be triggered by anyone who finds
the URL. On the Hobby plan, cron jobs are capped at once/day and Vercel
may invoke anywhere within that hour rather than exactly on the minute -
see [Vercel's Cron Jobs docs](https://vercel.com/docs/cron-jobs) for
current plan limits.

## Project layout

```
main.py             # Vercel/deployment entrypoint - re-exports product_status.server:app
product_status/
  config.py         # API key + team filter resolution (.env aware), CACHE_DIR
  linear_client.py   # raw GraphQL client: auth, retries, pagination, aliasing
  cycles.py          # team + cycle (activeCycle / isPrevious) lookups
  issues.py           # cycle issue scope, uncompletedIssuesUponClose, mid-cycle detection via issue.history
  report.py          # assembles the per-team, per-assignee report
  projects.py         # project summaries (status, dates, milestones) for a given project label
  quality.py           # per-team SLA/bug counts (out of SLA, failed SLA, incoming high/urgent bugs)
  dashboard.py         # combines sprints + summit projects + quality, grouped by squad, for the web UI
  milestones_report.py  # cross-project milestone timeline + overloaded-person detection for the Project Milestones tab
  intercom_client.py     # raw Intercom REST API client (auth, retries, search pagination)
  support_report.py      # live Intercom SLA "5 metrics" per squad for the Support Report tab
  partner_identity.py    # shared Intercom<->Linear<->Vitally partner resolution (support_report.py + partner_insights.py)
  partner_insights.py    # per-partner Product (Linear) + Escalations for the Partner Insights tab (filtered to Vitally-matched partners)
  vitally_client.py      # raw Vitally REST API client (Basic Auth, cursor pagination) - escalation_report.py's email source + partner_identity.py's account matching
  escalation_report.py   # Vitally-synced partner emails, triaged by an LLM, for Partner Insights' Live Fire/Smoldering columns
  openai_client.py       # thin OpenAI Chat Completions wrapper shared by partner_insights.py + escalation_report.py
  cache.py             # JSON cache keyed by age (used by the dashboard, 24h default) - on disk, or...
  blob_cache.py         # ...Vercel Blob-backed, when BLOB_READ_WRITE_TOKEN is set (persists on serverless hosts)
  notion_client.py      # raw Notion REST API client (auth, retries, nested block creation)
  notion_oauth.py       # Notion OAuth ("public connection") flow - no workspace-owner permission needed
  notion_report.py      # builds the "EPD Report <date>" Notion page from dashboard data
  auth.py               # Google sign-in restricted to one email domain + signed session cookie
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
