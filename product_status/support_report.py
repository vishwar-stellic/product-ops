"""Support SLA snapshot for the "Support Report" dashboard tab - the "5
metrics" from Stellic's `support-sla-dashboard` Claude Skill, computed live
from Intercom instead of that skill's Notion-maintained register (this tab
has no persistent per-ticket store, hand-maintained columns, or per-PDL
breakdown - those require the skill's manually-uploaded Vitally CSV, which
isn't available to this service; it shows area-level totals only).

## The 5 metrics (Key User tickets only - see the skill for the full spec)
1. Total open KU tickets - open + snoozed, excluding any already marked
   "Resolved" at the ticket level (a ticket can be "Resolved" while its
   conversation is still technically open in Intercom).
2. New KU tickets this week - created since the start of the current
   calendar week, regardless of current state.
3. KU tickets closed this week - first closed (`statistics.first_close_at`)
   since the start of the current calendar week, regardless of when
   created.
4. Out of first-response SLA - no genuine admin/bot reply within
   `FR_TARGET_HOURS` *business* hours (weekends don't tick), including
   never-answered.
5. Out of resolution SLA - open more than `RES_TARGET_DAYS` calendar days
   AND priority is Urgent or High.

"This week" (metrics 2 and 3) is a **calendar week-to-date** counter, not a
rolling trailing-N-days window: it's everything since the most recent
Monday 00:00 *Pacific time* (`_current_week_start` - Pacific to match how
the team refers to dates day-to-day elsewhere, e.g.
`notion_report.py:_PACIFIC`), so it grows through the week and snaps back
down to (near) zero at each Monday reset - a genuine week-to-date number
rather than an always-full "last 7 days" figure. This matters once this
report runs on a schedule (a daily cron, say): each day's snapshot reflects
that day's actual progress through the week, not a smeared-out trailing
average.

"Open" always means Intercom state `open` **or** `snoozed` - snoozing is a
working convenience, not a resolution (the skill's own hard rule, born from
a ticket that sat snoozed and invisible for weeks).

## Why some tickets need an extra API call
`statistics.first_admin_reply_at` is `null` for admin-initiated and
escalated conversations even when a genuine reply happened (an assignment
or comment part with a real body doesn't set it) - trusting a `null` there
as "never answered" produces false positives. So any *open* Key User
ticket missing that timestamp gets its full `conversation_parts` fetched
and scanned for the first genuine customer-facing reply
(`_first_customer_facing_reply_at`), mirroring the skill's own verification
rule. This is the expensive part of a refresh (one extra HTTP call per such
ticket) - `_verify_replies` runs those concurrently.

## Product Area mapping
Intercom's "Product Area" custom attribute (conversation- or ticket-level,
matched by prefix - e.g. "Progress: Foo" still counts as "Progress", same
as the skill's `match()`) is mapped to this dashboard's squad keys via
`AREAS`, in display order (no "Dev-ex" - it has no customer-facing
Intercom area, so it was dropped from the table entirely rather than
showing an always-"—" column).

## Ticket-level detail (drill-down)
Each squad's metrics also carry the underlying ticket list
(`openKUTickets`/`newKUTickets`/`closedKUTickets`) so the dashboard can show
"which tickets" behind a number without a second live Intercom call - the
"out of first response" / "out of resolution" rows are just a client-side
filter over `openKUTickets` (`firstResponseSLA != "Met"` /
`outOfResolutionSLA`), since every open KU ticket already carries both
flags. Two different "who" fields are included per ticket:
- `userName` - the individual requester. For a normal (`user`-authored)
  conversation this is just `source.author.name`. But a sizeable chunk of
  tickets are *admin-initiated* (created via API/integration, or on a
  customer's behalf) - there `source.author` is a Stellic admin/bot (often
  literally named "Support Team"), which isn't a customer name at all and
  would be misleading here. For those, the real requester is looked up
  from the conversation's linked `contacts` entry via a batched
  `/contacts/search` (`id IN [...]`) call - see
  `_build_contact_name_map` - rather than trusting `source.author`.
- `partnerName` - the institution, resolved by `partner_identity.py`
  (shared with `partner_insights.py`) the same way as the skill's
  `resolve_partner`: the conversation's `company.name` if present, else the
  partner code embedded in a contact's `external_id` (commonly
  `<user>@<code>`, e.g. `cjp260@newcastle`) looked up against a
  `company_id -> name` map built once per refresh from `list_companies`,
  else the requester's email domain against a small manual map for a few
  known non-obvious domains (`partner_identity.DOMAIN_TO_PARTNER`).
  Unmatched stays "(unknown)" rather than guessing.

## Trend history
Every time this module actually runs (a cache-miss GET or a forced
Update - *not* every page load, which usually just reads the 24h cache -
see `server.py`'s `_get_support_report`), it appends one snapshot of the
top table's numbers to a small history log in the same cache backend
(`cache.read_raw`/`write_raw`, bypassing the usual TTL/version wrapping
since this is an accumulating log, not a point-in-time entry). Each
snapshot records, per metric row, the Total plus each squad's value at
that moment - the dashboard's trend chart reads this via
`get_support_report_history` / `GET /api/support-report/history`. Capped
at `SUPPORT_REPORT_HISTORY_MAX_POINTS` (oldest points drop off) so the log
can't grow unbounded; recording is best-effort (wrapped so a storage
hiccup never breaks the report itself).

## Weekly SLA cohort bars (trend chart)
Each refresh also computes `weeklyCohorts` (last
`SUPPORT_REPORT_WEEKLY_COHORT_WEEKS` Pacific Monday weeks, one point per
week): Key User tickets **created** that week. Resolution bar = % of
Urgent/High in the cohort that **met** resolution SLA as of now: closed
within the 21-day window = Met; past 21 days (open or closed) = Breached;
still open and not yet past it = "Pending". The denominator is every
eligible ticket (Pending included), so recent weeks start low and rise as
tickets resolve; the UI shades the pending share
(`pctResolutionSlaPotential` is the ceiling). First-response bar = % of the cohort's tickets (any state) whose first reply
landed within the SLA window, graded like the drill-down table; tickets still
"Pending" (no reply yet, clock not run out) are excluded. Bars use
the chart's right axis (0–100%); the existing refresh history lines stay on
the left.

## Daily Stellic engagement series
`dailyEngagement` has one row per Pacific calendar day (from the start of the
cohort window through today, today partial): `byColumn[col].stellicResponses`
/ `ticketsResponded` plus the `tickets` behind them - how many customer-facing
replies Stellic *sent* on Key User tickets that day, whether or not those
tickets ever close. It's by reply date, not ticket-creation cohort, and covers
every Key User ticket touched in the window (the cohort pull is derived from
an `updated_at` search for this reason). A response = a human teammate's
non-note part with a body (`_stellic_response_times`; bots excluded unless
`ENGAGEMENT_COUNT_BOTS`). Reply times come from `conversation_parts`, cached
per ticket by `updated_at` (`_collect_stellic_responses`).
"""

import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import cache
from .intercom_client import IntercomClient
from .partner_identity import build_company_map, partner_name

# Matches `notion_report.py:_PACIFIC` - "this week" resets on Pacific-time
# Mondays, not UTC ones, to match how the team actually thinks about weeks.
_PACIFIC = ZoneInfo("America/Los_Angeles")

SUPPORT_REPORT_CACHE_KEY = "dashboard-support-report"

# Bump whenever this module's output shape or underlying metric logic
# changes - see `milestones_report.py:MILESTONES_REPORT_CACHE_VERSION` for
# why (same cache has no schema of its own).
SUPPORT_REPORT_CACHE_VERSION = 20

# Separate raw key (not versioned/aged like the main report - see
# `cache.read_raw`) for the trend chart's accumulating history log.
SUPPORT_REPORT_HISTORY_KEY = "dashboard-support-report-history"
# ~1.5 years of daily snapshots in storage (one point per real refresh); the
# chart reads only the most recent `SUPPORT_REPORT_TREND_CHART_MAX_POINTS`.
SUPPORT_REPORT_HISTORY_MAX_POINTS = 500
SUPPORT_REPORT_TREND_CHART_MAX_POINTS = 36

# Pacific Monday weeks for the trend chart's SLA cohort bars (one point per week).
SUPPORT_REPORT_WEEKLY_COHORT_WEEKS = 6

# Weekly "Stellic responses" engagement series (see `_collect_stellic_responses`).
# Per-ticket reply timestamps are cached here (raw, unversioned) keyed by the
# ticket's `updated_at`, so only tickets that changed get re-fetched.
ENGAGEMENT_CACHE_KEY = "dashboard-support-report-replies"
# Count automated (bot / Fin) replies as Stellic responses? Default no: the
# series is meant to show human engagement.
ENGAGEMENT_COUNT_BOTS = False
# Stop fetching new ticket parts once the whole refresh has been running this
# long (cold-cache protection: `vercel.json` caps the function at 300s and the
# Intercom searches alone can take ~200s, so the first refresh after deploy
# may only get partway; later refreshes finish the job from the cache).
ENGAGEMENT_DEADLINE_SECONDS = 235.0
ENGAGEMENT_FETCH_CHUNK = 64

INTERCOM_INBOX_PREFIX = "g60t55rg"

FR_TARGET_HOURS = 24.0
RES_TARGET_DAYS = 21.0

RESOLVED_TICKET_STATE = "Resolved"

# The 5 metric row keys, in table order - mirrors the frontend's
# `SUPPORT_REPORT_ROWS` (`static/app.js`) and each area's `_area_metrics`
# dict keys. Used by `_history_snapshot` to know which keys to log.
SUPPORT_REPORT_METRIC_KEYS: List[str] = [
    "totalOpenKU",
    "newKUThisWeek",
    "closedKUThisWeek",
    "outOfFirstResponseSLA",
    "outOfResolutionSLA",
]

# Intercom "Product Area" prefix -> this dashboard's squad key/label, in
# display order (per request: Progress, then Plan/Platform/Integration/
# Care/Explore - no Dev-ex, see module docstring).
AREAS: List[Dict[str, str]] = [
    {"squad": "PROG", "label": "Progress", "intercomArea": "Progress"},
    {"squad": "PLAN", "label": "Plan", "intercomArea": "Plan"},
    {"squad": "PLAT", "label": "Platform", "intercomArea": "Platform"},
    {"squad": "INT", "label": "Integration", "intercomArea": "Data & Integration"},
    {"squad": "CARE", "label": "Care", "intercomArea": "Care"},
    {"squad": "EXP", "label": "Explore", "intercomArea": "Explore"},
]
_AREA_BY_INTERCOM_NAME = {a["intercomArea"]: a["squad"] for a in AREAS if a["intercomArea"]}


def _match_prefix(value: str, prefix: str) -> bool:
    return value == prefix or value.startswith(prefix + ":")


def _conv_product_area(conversation: Dict[str, Any]) -> str:
    return (conversation.get("custom_attributes") or {}).get("Product Area") or ""


def _ticket_product_area(conversation: Dict[str, Any]) -> str:
    value = ((conversation.get("ticket") or {}).get("custom_attributes") or {}).get("Product Area")
    return (value.get("value") if isinstance(value, dict) else value) or ""


def _squad_for(conversation: Dict[str, Any]) -> Optional[str]:
    conv_area = _conv_product_area(conversation)
    ticket_area = _ticket_product_area(conversation)
    for intercom_area, squad in _AREA_BY_INTERCOM_NAME.items():
        if _match_prefix(conv_area, intercom_area) or _match_prefix(ticket_area, intercom_area):
            return squad
    return None


def _is_key_user(conversation: Dict[str, Any]) -> bool:
    attrs = conversation.get("custom_attributes") or {}
    return attrs.get("Key User for Support") is True or attrs.get("Star User for Support") is True


def _priority(conversation: Dict[str, Any]) -> Optional[str]:
    attrs = conversation.get("custom_attributes") or {}
    value = attrs.get("Priority")
    if value in ("Urgent", "High", "Medium", "Low"):
        return value
    urgency = ((conversation.get("ticket") or {}).get("custom_attributes") or {}).get("Urgency")
    urgency = urgency.get("value") if isinstance(urgency, dict) else urgency
    return urgency if urgency in ("Urgent", "High", "Medium", "Low") else None


def _ticket_state(conversation: Dict[str, Any]) -> str:
    return (conversation.get("ticket") or {}).get("ticket_custom_state_admin_label") or "(blank)"


def _week_end(week_start: float) -> float:
    return week_start + 7 * 86400.0


def _list_week_starts(now: float, num_weeks: int) -> List[float]:
    """Pacific Monday 00:00 boundaries, oldest first, including the current week."""
    current = _current_week_start(now)
    return [current - i * 7 * 86400.0 for i in range(num_weeks - 1, -1, -1)]


def _resolution_close_ts(conversation: Dict[str, Any]) -> tuple:
    """`(epoch, source)` for when this ticket stopped its resolution clock, or
    `(None, None)` if it's still unresolved.

    - Ticket status "Resolved" counts as finished even if the conversation is
      still open; Intercom's close stats may be stale from an earlier close
      then, so `updated_at` is used (or the latest close if the conversation
      is closed too).
    - A conversation that's open/snoozed and not Resolved is **still open**,
      whatever its history: an earlier close (a reopened ticket) means
      nothing for the resolution SLA, so the clock keeps running.
    - A closed conversation uses its *latest* close (`last_close_at`, then
      `first_close_at`) - if it was reopened and closed again, the final
      close is the real one; falls back to `updated_at` when Intercom
      recorded neither (common for tickets closed via API/automation).
    """
    stats = conversation.get("statistics") or {}
    state = conversation.get("state")
    resolved = _ticket_state(conversation) == RESOLVED_TICKET_STATE
    updated = conversation.get("updated_at")

    if state != "closed":
        # Open or snoozed: only a Resolved ticket status ends the clock.
        if resolved and updated:
            return updated, "updated_at (ticket Resolved, conversation open)"
        return None, None

    if stats.get("last_close_at"):
        return stats["last_close_at"], "last_close_at"
    if stats.get("first_close_at"):
        return stats["first_close_at"], "first_close_at"
    if updated:
        return updated, "updated_at (no close timestamp)"
    return None, None


def _resolution_age_days_at(conversation: Dict[str, Any], eval_ts: float) -> Optional[float]:
    """Days from creation until the earlier of its close and `eval_ts` - the
    clock the 21-day resolution SLA runs against. `None` if the ticket
    didn't exist yet at `eval_ts`."""
    created = conversation.get("created_at")
    if not created or created >= eval_ts:
        return None
    close_ts, _ = _resolution_close_ts(conversation)
    end = min(eval_ts, close_ts) if close_ts else eval_ts
    return (end - created) / 86400.0


def _resolution_breached_at(conversation: Dict[str, Any], eval_ts: float) -> bool:
    if _priority(conversation) not in ("Urgent", "High"):
        return False
    age = _resolution_age_days_at(conversation, eval_ts)
    return age is not None and age > RES_TARGET_DAYS


def _resolution_label(conversation: Dict[str, Any], eval_ts: float) -> str:
    """Weekly-cohort resolution SLA outcome for one ticket:

    - "Not eligible" - not Urgent/High (no resolution SLA applies).
    - "Breached"     - older than `RES_TARGET_DAYS` at close (or, if still
                       open, as of `eval_ts`).
    - "Met"          - closed within `RES_TARGET_DAYS`.
    - "Pending"      - still open and not yet past `RES_TARGET_DAYS`: no
                       outcome yet. It counts in the weekly bar's denominator
                       (all eligible tickets) but not its numerator, so the
                       bar is a lower bound that rises as tickets resolve.
    """
    if _priority(conversation) not in ("Urgent", "High"):
        return "Not eligible"
    age = _resolution_age_days_at(conversation, eval_ts)
    if age is None:
        return "Pending"
    if age > RES_TARGET_DAYS:
        return "Breached"
    close_ts, _ = _resolution_close_ts(conversation)
    return "Met" if close_ts else "Pending"


def _conversation_url(conversation_id: Any) -> str:
    return f"https://app.intercom.com/a/inbox/{INTERCOM_INBOX_PREFIX}/inbox/shared/all/conversation/{conversation_id}"


def _cohort_ticket_debug(
    conversation: Dict[str, Any],
    reply_overrides: Dict[str, Optional[float]],
    eval_ts: float,
    now: float,
) -> Dict[str, Any]:
    """Per-ticket inputs and outcomes behind the weekly cohort bars, so the
    dashboard's debug panel can show exactly what each bar counted."""
    stats = conversation.get("statistics") or {}
    created = conversation.get("created_at")
    stat_reply = stats.get("first_admin_reply_at")
    override_reply = reply_overrides.get(conversation["id"])
    reply = stat_reply or override_reply
    priority = _priority(conversation) or "(blank)"
    eligible = priority in ("Urgent", "High")
    age = _resolution_age_days_at(conversation, eval_ts)
    close_ts, close_source = _resolution_close_ts(conversation)
    return {
        "id": conversation.get("id"),
        "url": _conversation_url(conversation.get("id")),
        "description": _ticket_description(conversation),
        "squad": _squad_for(conversation) or "",
        "createdAt": _epoch_to_iso(created),
        "priority": priority,
        "state": conversation.get("state"),
        "ticketState": _ticket_state(conversation),
        "firstReplyAt": _epoch_to_iso(reply),
        "replySource": "intercom statistics" if stat_reply else ("conversation parts" if override_reply else None),
        "frBusinessHours": round(_business_hours_between(created, reply or now), 1),
        "frLabel": _first_response_label(conversation, reply_overrides, now),
        "closedAt": _epoch_to_iso(close_ts),
        "closedSource": close_source,
        "resolutionEligible": eligible,
        "resolutionAgeDays": round(age, 1) if age is not None else None,
        "resolutionBreached": bool(eligible and age is not None and age > RES_TARGET_DAYS),
        "resolutionLabel": _resolution_label(conversation, eval_ts),
    }


def _build_weekly_sla_cohorts(
    cohort_conversations: List[Dict[str, Any]],
    reply_overrides: Dict[str, Optional[float]],
    now: float,
) -> List[Dict[str, Any]]:
    """One row per Pacific calendar week (cohort = Key User tickets created
    that week, in any state today): % resolution SLA met (Urgent/High,
    evaluated as of now - see `_resolution_label`: Met = closed within
    `RES_TARGET_DAYS`; Breached and still-open "Pending" tickets are not
    met, but all eligible tickets stay in the denominator so recent weeks
    show a rising lower bound; evaluating at week end would be meaningless
    since a ticket can't be older than 7 days then)
    and % first-response SLA met - the share of
    cohort tickets whose first reply landed within `FR_TARGET_HOURS` business
    hours, using the same grading as the drill-down table
    (`_first_response_label`). Tickets still "Pending" (no reply yet, clock
    not run out) have no outcome yet and are left out of the denominator."""
    week_starts = _list_week_starts(now, SUPPORT_REPORT_WEEKLY_COHORT_WEEKS)
    column_keys = ["TOTAL"] + [a["squad"] for a in AREAS]
    rows: List[Dict[str, Any]] = []

    for week_start in week_starts:
        week_end = _week_end(week_start)
        eval_ts = now
        cohort = [
            c
            for c in cohort_conversations
            if _is_key_user(c)
            and c.get("created_at")
            and week_start <= c["created_at"] < week_end
        ]
        by_column: Dict[str, Dict[str, Any]] = {}
        for col in column_keys:
            if col == "TOTAL":
                scoped = cohort
            else:
                scoped = [c for c in cohort if _squad_for(c) == col]

            res_eligible = [c for c in scoped if _priority(c) in ("Urgent", "High")]
            res_labels = [_resolution_label(c, eval_ts) for c in res_eligible]
            res_met = sum(1 for label in res_labels if label == "Met")
            res_graded = sum(1 for label in res_labels if label in ("Met", "Breached"))
            res_pending = sum(1 for label in res_labels if label == "Pending")
            fr_labels = [_first_response_label(c, reply_overrides, now) for c in scoped]
            fr_met = sum(1 for label in fr_labels if label == "Met")
            fr_graded = sum(1 for label in fr_labels if label in ("Met", "Not Met"))

            # Denominator = every Urgent/High ticket in the cohort, so recent
            # weeks (mostly still-open "Pending" tickets) read as a lower
            # bound that rises as tickets get resolved. The "potential" value
            # is the ceiling if every pending ticket ends up Met.
            pct_res = round(100.0 * res_met / len(res_eligible), 1) if res_eligible else None
            pct_res_potential = (
                round(100.0 * (res_met + res_pending) / len(res_eligible), 1) if res_eligible else None
            )
            pct_fr = round(100.0 * fr_met / fr_graded, 1) if fr_graded else None
            by_column[col] = {
                "pctResolutionSlaMet": pct_res,
                "pctResolutionSlaPotential": pct_res_potential,
                "pctFirstResponseSlaMet": pct_fr,
                "resolutionEligible": len(res_eligible),
                "resolutionGraded": res_graded,
                "resolutionPending": res_pending,
                "resolutionSlaMetCount": res_met,
                "firstResponseGraded": fr_graded,
                "firstResponseSlaMetCount": fr_met,
            }

        rows.append(
            {
                "weekStartAt": datetime.fromtimestamp(week_start, timezone.utc).isoformat(),
                "evaluatedAt": datetime.fromtimestamp(eval_ts, timezone.utc).isoformat(),
                "byColumn": by_column,
                # Every cohort ticket once (the dashboard filters by squad
                # for the debug panel) - see `_cohort_ticket_debug`.
                "tickets": [_cohort_ticket_debug(c, reply_overrides, eval_ts, now) for c in cohort],
            }
        )
    return rows


def _current_week_start(now: float) -> float:
    """Epoch timestamp for 00:00 Pacific time on the most recent Monday - the
    "this week" boundary for `newKUThisWeek`/`closedKUThisWeek` (see module
    docstring). A calendar week-to-date window, not a rolling trailing-7-days
    one: it resets to (near) zero every Monday rather than always covering a
    full 7 days."""
    now_pacific = datetime.fromtimestamp(now, _PACIFIC)
    # datetime.weekday(): Monday=0 ... Sunday=6, i.e. already "days elapsed
    # since the most recent Monday".
    monday_date = (now_pacific - timedelta(days=now_pacific.weekday())).date()
    week_start = datetime(monday_date.year, monday_date.month, monday_date.day, tzinfo=_PACIFIC)
    return week_start.timestamp()


def _business_hours_between(start: Optional[float], end: Optional[float]) -> float:
    """Elapsed hours between two epoch timestamps, counting only Mon-Fri
    (UTC) - Sat/Sun don't tick (metric 4 is weekend-aware; see docstring)."""
    if not start or not end or end <= start:
        return 0.0
    total_seconds = 0.0
    cursor = start
    while cursor < end:
        day = datetime.fromtimestamp(cursor, timezone.utc)
        day_end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() + 86400
        segment_end = min(end, day_end)
        if day.weekday() < 5:  # Mon=0 ... Fri=4
            total_seconds += segment_end - cursor
        cursor = segment_end
    return total_seconds / 3600.0


def _first_customer_facing_reply_at(conversation_parts: List[Dict[str, Any]]) -> Optional[float]:
    """Earliest epoch among `conversation_parts` that's a genuine
    customer-facing reply: author admin/bot, not an internal note, non-empty
    body (a `comment` or an `assignment` *with* a body both count - neither
    always sets `statistics.first_admin_reply_at`, which is why this exists
    at all - see module docstring)."""
    earliest: Optional[float] = None
    for part in conversation_parts or []:
        author = part.get("author") or {}
        if author.get("type") not in ("admin", "bot"):
            continue
        if part.get("part_type") == "note":
            continue
        if not (part.get("body") or "").strip():
            continue
        created_at = part.get("created_at")
        if created_at and (earliest is None or created_at < earliest):
            earliest = created_at
    return earliest


def _verify_replies(client: IntercomClient, conversations: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """For each conversation missing `statistics.first_admin_reply_at`,
    fetch it in full and look for a genuine reply the summary field missed
    - see module docstring. Only ever called for *open* Key User tickets,
    which keeps the extra-fetch set to a fraction of the total queue."""

    def _fetch(conversation: Dict[str, Any]) -> tuple:
        full = client.get_conversation(conversation["id"])
        parts = ((full.get("conversation_parts") or {}).get("conversation_parts")) or []
        return conversation["id"], _first_customer_facing_reply_at(parts)

    if not conversations:
        return {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        return dict(pool.map(_fetch, conversations))


def _stellic_response_times(conversation_parts: List[Dict[str, Any]]) -> List[float]:
    """Epoch of every genuine Stellic response in `conversation_parts`: a
    human teammate (author `admin`; `bot` too only if `ENGAGEMENT_COUNT_BOTS`),
    not an internal note, with a non-empty body - the same bar as
    `_first_customer_facing_reply_at`, but every part rather than the first."""
    allowed = ("admin", "bot") if ENGAGEMENT_COUNT_BOTS else ("admin",)
    times: List[float] = []
    for part in conversation_parts or []:
        author = part.get("author") or {}
        if author.get("type") not in allowed:
            continue
        if part.get("part_type") == "note":
            continue
        if not (part.get("body") or "").strip():
            continue
        created_at = part.get("created_at")
        if created_at:
            times.append(float(created_at))
    return sorted(times)


def _collect_stellic_responses(
    client: IntercomClient,
    conversations: List[Dict[str, Any]],
    since_ts: float,
    now: float,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """Stellic response timestamps (>= `since_ts`) per Key User ticket, for
    the weekly engagement series. Reply times only exist in
    `conversation_parts` (one `get_conversation` call per ticket), so they're
    cached per ticket keyed by its `updated_at`: a ticket is re-fetched only
    when Intercom says it changed (any new reply bumps `updated_at`), which
    makes every refresh after the first cheap. A deadline keeps a cold
    cache from blowing the function's max duration - whatever isn't fetched
    in time falls back to its previous cache entry (or is skipped) and is
    picked up on the next refresh; `complete` is False meanwhile.

    Returns {"byTicket": {conversation_id: [epoch, ...]}, "complete": bool,
    "notFetched": int}."""
    stored = cache.read_raw(ENGAGEMENT_CACHE_KEY) or {}
    cached: Dict[str, Any] = stored.get("tickets") or {}

    by_ticket: Dict[str, List[float]] = {}
    fresh: Dict[str, Any] = {}
    to_fetch: List[Dict[str, Any]] = []
    for conversation in conversations:
        cid = str(conversation["id"])
        entry = cached.get(cid)
        if entry and entry.get("updatedAt") == conversation.get("updated_at"):
            fresh[cid] = entry
            by_ticket[cid] = [t for t in entry.get("replies") or [] if t >= since_ts]
        else:
            to_fetch.append(conversation)

    def _fetch(conversation: Dict[str, Any]) -> Optional[tuple]:
        try:
            full = client.get_conversation(conversation["id"])
        except Exception as exc:  # one bad ticket must not sink the report
            print(f"support_report: parts fetch failed for {conversation.get('id')}: {exc}", file=sys.stderr)
            return None
        parts = ((full.get("conversation_parts") or {}).get("conversation_parts")) or []
        return str(conversation["id"]), conversation.get("updated_at"), _stellic_response_times(parts)

    if deadline is None:
        deadline = time.time() + ENGAGEMENT_DEADLINE_SECONDS
    not_fetched = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for start in range(0, len(to_fetch), ENGAGEMENT_FETCH_CHUNK):
            chunk = to_fetch[start : start + ENGAGEMENT_FETCH_CHUNK]
            if time.time() > deadline:
                for conversation in chunk + to_fetch[start + ENGAGEMENT_FETCH_CHUNK :]:
                    cid = str(conversation["id"])
                    stale = cached.get(cid)
                    if stale:
                        # Last known replies beat nothing; keep the old entry
                        # (and its old updatedAt) so it refetches next time.
                        fresh[cid] = stale
                        by_ticket[cid] = [t for t in stale.get("replies") or [] if t >= since_ts]
                    not_fetched += 1
                break
            for result in pool.map(_fetch, chunk):
                if result is None:
                    not_fetched += 1
                    continue
                cid, updated_at, times = result
                replies = [t for t in times if t >= since_ts]
                fresh[cid] = {"updatedAt": updated_at, "replies": replies}
                by_ticket[cid] = replies

    try:
        cache.write_raw(ENGAGEMENT_CACHE_KEY, {"updatedAt": now, "tickets": fresh})
    except Exception as exc:  # best-effort, like the trend history
        print(f"support_report: could not save engagement cache: {exc}", file=sys.stderr)
    return {"byTicket": by_ticket, "complete": not_fetched == 0, "notFetched": not_fetched}


def _build_daily_engagement(
    conversations: List[Dict[str, Any]],
    engagement: Dict[str, Any],
    since_ts: float,
    now: float,
) -> List[Dict[str, Any]]:
    """One row per Pacific calendar day from `since_ts` through today: per
    Total/squad, how many Stellic responses went out on Key User tickets that
    day - by when the response was *sent*, regardless of when the ticket was
    created or whether it ever closes - and on how many distinct tickets,
    plus the tickets themselves for the dashboard's drill-down. Today is
    partial (so far)."""
    column_keys = ["TOTAL"] + [a["squad"] for a in AREAS]
    by_id = {str(c["id"]): c for c in conversations}

    # {pacific date: {conversation id: responses that day}}
    per_day: Dict[Any, Dict[str, List[float]]] = {}
    for cid, times in engagement["byTicket"].items():
        for t in times:
            if t < since_ts or t > now:
                continue
            day = datetime.fromtimestamp(t, _PACIFIC).date()
            per_day.setdefault(day, {}).setdefault(cid, []).append(t)

    rows: List[Dict[str, Any]] = []
    day = datetime.fromtimestamp(since_ts, _PACIFIC).date()
    today = datetime.fromtimestamp(now, _PACIFIC).date()
    while day <= today:
        day_start = datetime(day.year, day.month, day.day, tzinfo=_PACIFIC)
        tickets: List[Dict[str, Any]] = []
        for cid, times in (per_day.get(day) or {}).items():
            c = by_id.get(cid)
            if not c:
                continue
            tickets.append(
                {
                    "id": c.get("id"),
                    "url": _conversation_url(c.get("id")),
                    "description": _ticket_description(c),
                    "squad": _squad_for(c) or "",
                    "createdAt": _epoch_to_iso(c.get("created_at")),
                    "priority": _priority(c) or "(blank)",
                    "state": c.get("state"),
                    "ticketState": _ticket_state(c),
                    "responses": len(times),
                    "lastResponseAt": _epoch_to_iso(max(times)),
                }
            )
        by_column: Dict[str, Dict[str, int]] = {}
        for col in column_keys:
            scoped = tickets if col == "TOTAL" else [t for t in tickets if t["squad"] == col]
            by_column[col] = {
                "stellicResponses": sum(t["responses"] for t in scoped),
                "ticketsResponded": len(scoped),
            }
        rows.append(
            {
                "date": day.isoformat(),
                "dayStartAt": day_start.astimezone(timezone.utc).isoformat(),
                "partial": day == today,
                "byColumn": by_column,
                "tickets": tickets,
            }
        )
        day += timedelta(days=1)
    return rows


def _fr_breach(conversation: Dict[str, Any], reply_overrides: Dict[str, Optional[float]], now: float) -> bool:
    created = conversation.get("created_at")
    if not created:
        return False
    reply = (conversation.get("statistics") or {}).get("first_admin_reply_at") or reply_overrides.get(
        conversation["id"]
    )
    if reply:
        return _business_hours_between(created, reply) > FR_TARGET_HOURS
    # Never answered - only a breach once the business-hour clock has
    # actually run out (a brand new ticket isn't "out" yet).
    return _business_hours_between(created, now) > FR_TARGET_HOURS


def _first_response_label(conversation: Dict[str, Any], reply_overrides: Dict[str, Optional[float]], now: float) -> str:
    """"Met" / "Not Met" / "Pending" (still within the clock, no reply yet)
    - the same grading `_fr_breach` uses, spelled out for the ticket table."""
    created = conversation.get("created_at")
    if not created:
        return "Pending"
    reply = (conversation.get("statistics") or {}).get("first_admin_reply_at") or reply_overrides.get(
        conversation["id"]
    )
    if reply:
        return "Met" if _business_hours_between(created, reply) <= FR_TARGET_HOURS else "Not Met"
    return "Pending" if _business_hours_between(created, now) <= FR_TARGET_HOURS else "Not Met"


def _strip_html(value: Optional[str]) -> str:
    if not value:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", " ", value)).strip()


def _ticket_description(conversation: Dict[str, Any]) -> str:
    ticket_attrs = ((conversation.get("ticket") or {}).get("custom_attributes")) or {}
    title = ticket_attrs.get("_default_title_")
    title = title.get("value") if isinstance(title, dict) else title
    if title:
        return _strip_html(title)
    subject = (conversation.get("source") or {}).get("subject")
    if subject:
        return _strip_html(subject)
    return f"Conversation {conversation.get('id')}"


def _primary_contact_id(conversation: Dict[str, Any]) -> Optional[str]:
    contacts = ((conversation.get("contacts") or {}).get("contacts")) or []
    return contacts[0].get("id") if contacts and contacts[0].get("id") else None


def _needs_contact_lookup(conversation: Dict[str, Any]) -> bool:
    """True when `source.author` is a Stellic admin/bot rather than the
    customer - see module docstring's `userName` section."""
    author = (conversation.get("source") or {}).get("author") or {}
    return author.get("type") in ("admin", "bot")


def _build_contact_name_map(client: IntercomClient, conversations: List[Dict[str, Any]]) -> Dict[str, str]:
    """contact id -> display name (name, falling back to email), for every
    contact behind an admin/bot-authored Key User conversation - batched via
    `/contacts/search`'s `id IN [...]` (Intercom caps composite `IN` queries
    at 15 values) rather than one `/contacts/{id}` call each."""
    contact_ids = sorted(
        {
            _primary_contact_id(c)
            for c in conversations
            if _is_key_user(c) and _needs_contact_lookup(c) and _primary_contact_id(c)
        }
    )
    if not contact_ids:
        return {}

    batch_size = 15
    batches = [contact_ids[i : i + batch_size] for i in range(0, len(contact_ids), batch_size)]

    def _fetch(batch: List[str]) -> Dict[str, str]:
        result = {}
        for contact in client.search_contacts({"field": "id", "operator": "IN", "value": batch}):
            display = contact.get("name") or contact.get("email")
            if contact.get("id") and display:
                result[contact["id"]] = display
        return result

    mapping: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(_fetch, batches):
            mapping.update(result)
    return mapping


def _user_name(conversation: Dict[str, Any], contact_name_map: Dict[str, str]) -> str:
    author = (conversation.get("source") or {}).get("author") or {}
    if not _needs_contact_lookup(conversation):
        return author.get("name") or author.get("email") or "(unknown)"
    contact_id = _primary_contact_id(conversation)
    return (contact_name_map.get(contact_id) if contact_id else None) or "(unknown)"


def _build_assignee_map(client: IntercomClient) -> Dict[str, str]:
    """`"admin:<id>"` / `"team:<id>"` -> display name, for resolving a
    conversation's assignee. Best effort: if the token lacks permission to
    read admins/teams (or the call fails), return what we have and let
    `_assignee_name` fall back to a bare id rather than failing the whole
    report."""
    names: Dict[str, str] = {}
    try:
        for admin in client.list_admins():
            if admin.get("id") is not None and admin.get("name"):
                names[f"admin:{admin['id']}"] = admin["name"]
    except Exception as exc:  # noqa: BLE001 - never fail the report over a label
        print(f"[support_report] couldn't load Intercom admins: {exc}", file=sys.stderr)
    try:
        for team in client.list_teams():
            if team.get("id") is not None and team.get("name"):
                names[f"team:{team['id']}"] = team["name"]
    except Exception as exc:  # noqa: BLE001
        print(f"[support_report] couldn't load Intercom teams: {exc}", file=sys.stderr)
    return names


def _assignee_name(conversation: Dict[str, Any], assignee_map: Dict[str, str]) -> str:
    """Who the conversation is assigned to in Intercom: the teammate if there
    is one, else the team, else "(unassigned)"."""
    admin_id = conversation.get("admin_assignee_id")
    if admin_id:
        return assignee_map.get(f"admin:{admin_id}") or f"Admin {admin_id}"
    team_id = conversation.get("team_assignee_id")
    if team_id:
        return f"{assignee_map.get(f'team:{team_id}') or f'Team {team_id}'} (team)"
    return "(unassigned)"


def _epoch_to_iso(value: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def _ticket_record(
    conversation: Dict[str, Any],
    squad: str,
    squad_label: str,
    reply_overrides: Dict[str, Optional[float]],
    company_map: Dict[str, str],
    contact_name_map: Dict[str, str],
    assignee_map: Dict[str, str],
    now: float,
) -> Dict[str, Any]:
    created = conversation.get("created_at")
    priority = _priority(conversation) or "(blank)"
    out_of_resolution = bool(
        created and (now - created) / 86400.0 > RES_TARGET_DAYS and priority in ("Urgent", "High")
    )
    first_reply = (conversation.get("statistics") or {}).get("first_admin_reply_at") or reply_overrides.get(
        conversation["id"]
    )
    return {
        "id": conversation.get("id"),
        "url": _conversation_url(conversation.get("id")),
        "squad": squad,
        "squadLabel": squad_label,
        "createdAt": _epoch_to_iso(created),
        "firstReplyAt": _epoch_to_iso(first_reply),
        "ticketState": _ticket_state(conversation),
        "conversationState": conversation.get("state"),
        "updatedAt": _epoch_to_iso(conversation.get("updated_at")),
        "userName": _user_name(conversation, contact_name_map),
        "partnerName": partner_name(conversation, company_map),
        "assignee": _assignee_name(conversation, assignee_map),
        "priority": priority,
        "description": _ticket_description(conversation),
        "firstResponseSLA": _first_response_label(conversation, reply_overrides, now),
        "outOfResolutionSLA": out_of_resolution,
    }


def _area_metrics(
    squad: str,
    label: str,
    open_register: List[Dict[str, Any]],
    created_raw: List[Dict[str, Any]],
    closed_raw: List[Dict[str, Any]],
    reply_overrides: Dict[str, Optional[float]],
    company_map: Dict[str, str],
    contact_name_map: Dict[str, str],
    assignee_map: Dict[str, str],
    now: float,
    week_start: float,
) -> Dict[str, Any]:
    ku_open = [c for c in open_register if _squad_for(c) == squad and _is_key_user(c)]
    new_ku = [
        c
        for c in created_raw
        if _squad_for(c) == squad and _is_key_user(c) and c.get("created_at") and week_start <= c["created_at"] < now
    ]
    closed_ku = [
        c
        for c in closed_raw
        if _squad_for(c) == squad
        and _is_key_user(c)
        and (c.get("statistics") or {}).get("first_close_at")
        and week_start <= c["statistics"]["first_close_at"] < now
    ]
    out_of_first_response = sum(1 for c in ku_open if _fr_breach(c, reply_overrides, now))
    out_of_resolution = sum(
        1
        for c in ku_open
        if c.get("created_at")
        and (now - c["created_at"]) / 86400.0 > RES_TARGET_DAYS
        and _priority(c) in ("Urgent", "High")
    )

    def _records(conversations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            _ticket_record(c, squad, label, reply_overrides, company_map, contact_name_map, assignee_map, now)
            for c in conversations
        ]

    return {
        "totalOpenKU": len(ku_open),
        "newKUThisWeek": len(new_ku),
        "closedKUThisWeek": len(closed_ku),
        "outOfFirstResponseSLA": out_of_first_response,
        "outOfResolutionSLA": out_of_resolution,
        # Ticket-level detail for the dashboard's drill-down table - see
        # module docstring ("Ticket-level detail"). "Out of first response"/
        # "out of resolution" reuse `openKUTickets` client-side rather than
        # getting their own lists, since every record already carries both
        # flags.
        "openKUTickets": _records(ku_open),
        "newKUTickets": _records(new_ku),
        "closedKUTickets": _records(closed_ku),
    }


def _history_snapshot(report: Dict[str, Any]) -> Dict[str, Any]:
    """One point-in-time row for the trend chart: per metric key, the Total
    across squads plus each squad's own value - see module docstring."""
    metrics: Dict[str, Dict[str, int]] = {}
    for row in SUPPORT_REPORT_METRIC_KEYS:
        by_squad: Dict[str, int] = {}
        total = 0
        for area in report["areas"]:
            value = (area.get("metrics") or {}).get(row)
            if isinstance(value, (int, float)):
                by_squad[area["squad"]] = value
                total += value
        by_squad["TOTAL"] = total
        metrics[row] = by_squad
    return {"at": report["generatedAt"], "metrics": metrics}


def _record_history(report: Dict[str, Any]) -> None:
    """Best-effort append to the trend history log - a storage hiccup here
    should never fail the report itself (see module docstring)."""
    try:
        existing = cache.read_raw(SUPPORT_REPORT_HISTORY_KEY) or {}
        points = existing.get("points") or []
        points.append(_history_snapshot(report))
        points = points[-SUPPORT_REPORT_HISTORY_MAX_POINTS:]
        cache.write_raw(SUPPORT_REPORT_HISTORY_KEY, {"points": points})
    except Exception as exc:  # noqa: BLE001 - never let history logging break the report
        print(f"[support_report] failed to record history point: {exc}")


def get_support_report_history() -> Dict[str, Any]:
    """Trend history for the chart - `{"points": [...]}` (oldest first among
    the returned slice), capped at `SUPPORT_REPORT_TREND_CHART_MAX_POINTS`."""
    existing = cache.read_raw(SUPPORT_REPORT_HISTORY_KEY) or {}
    points = existing.get("points") or []
    return {
        "points": points[-SUPPORT_REPORT_TREND_CHART_MAX_POINTS:],
        "totalPointsStored": len(points),
    }


def build_support_report(client: Optional[IntercomClient] = None) -> Dict[str, Any]:
    client = client or IntercomClient()
    started = now = time.time()
    week_start = _current_week_start(now)
    cohort_since = _list_week_starts(now, SUPPORT_REPORT_WEEKLY_COHORT_WEEKS)[0]

    # Intercom's search API can't filter on the "Product Area" custom
    # attribute (or its prefix-match semantics), so - like the skill - this
    # pulls each whole cohort once and filters/groups by area in Python
    # (`_area_metrics`) rather than querying per area. Each cohort can be
    # hundreds of conversations and Intercom's search endpoint runs
    # ~10s/page regardless of query, so the four independent pulls run
    # concurrently rather than one after another (a full sequential pull
    # took ~185s in practice; see `vercel.json`'s maxDuration for the
    # resulting worst-case budget on the force-refresh endpoint).
    with ThreadPoolExecutor(max_workers=7) as pool:
        open_future = pool.submit(
            lambda: list(client.search_conversations({"field": "state", "operator": "=", "value": "open"}))
        )
        snoozed_future = pool.submit(
            lambda: list(client.search_conversations({"field": "state", "operator": "=", "value": "snoozed"}))
        )
        created_future = pool.submit(
            lambda: list(
                client.search_conversations({"field": "created_at", "operator": ">=", "value": int(week_start)})
            )
        )
        # Everything *updated* in the cohort window. Every conversation
        # created in the window was also updated in it, so this is a strict
        # superset of the "created >= cohort_since" pull it replaces (the
        # cohort is derived from it below) - and it additionally catches
        # older tickets that got a Stellic reply this window, which the
        # weekly-engagement series needs. No extra search cost.
        updated_future = pool.submit(
            lambda: list(
                client.search_conversations({"field": "updated_at", "operator": ">=", "value": int(cohort_since)})
            )
        )
        closed_future = pool.submit(
            lambda: list(
                client.search_conversations(
                    {"field": "statistics.first_close_at", "operator": ">=", "value": int(week_start)}
                )
            )
        )
        company_map_future = pool.submit(lambda: build_company_map(client))
        assignee_map_future = pool.submit(lambda: _build_assignee_map(client))
        open_raw = open_future.result()
        snoozed_raw = snoozed_future.result()
        created_raw = created_future.result()
        updated_raw = updated_future.result()
        closed_raw = closed_future.result()
        company_map = company_map_future.result()
        assignee_map = assignee_map_future.result()

    # Weekly cohort = tickets *created* in the window (any state today).
    cohort_raw = [c for c in updated_raw if (c.get("created_at") or 0) >= cohort_since]

    # "Open" = open + snoozed, always (see module docstring); a ticket
    # marked Resolved at the ticket-state level is done even if Intercom
    # still shows the conversation itself as open.
    open_register = [c for c in open_raw + snoozed_raw if _ticket_state(c) != RESOLVED_TICKET_STATE]

    # Key User tickets missing a reliable first-reply timestamp need
    # verifying - see `_verify_replies`: open (not snoozed) ones for the
    # table, plus every ticket in the weekly cohort window (any state) so the
    # first-response bars don't count answered tickets as unanswered.
    needs_verification: List[Dict[str, Any]] = []
    seen_verification_ids = set()
    for c in [x for x in open_register if x.get("state") == "open"] + cohort_raw:
        cid = c.get("id")
        if (
            cid
            and cid not in seen_verification_ids
            and _is_key_user(c)
            and not (c.get("statistics") or {}).get("first_admin_reply_at")
        ):
            seen_verification_ids.add(cid)
            needs_verification.append(c)
    # These two extra lookups are independent of each other, so run them
    # side by side rather than one after another.
    ku_updated = [c for c in updated_raw if _is_key_user(c) and c.get("id")]
    with ThreadPoolExecutor(max_workers=3) as pool:
        reply_future = pool.submit(_verify_replies, client, needs_verification)
        contact_name_future = pool.submit(
            _build_contact_name_map, client, open_register + created_raw + closed_raw
        )
        engagement_future = pool.submit(
            _collect_stellic_responses,
            client,
            ku_updated,
            cohort_since,
            now,
            started + ENGAGEMENT_DEADLINE_SECONDS,
        )
        reply_overrides = reply_future.result()
        contact_name_map = contact_name_future.result()
        engagement = engagement_future.result()

    areas = [
        {
            "squad": area["squad"],
            "label": area["label"],
            "metrics": _area_metrics(
                area["squad"],
                area["label"],
                open_register,
                created_raw,
                closed_raw,
                reply_overrides,
                company_map,
                contact_name_map,
                assignee_map,
                now,
                week_start,
            ),
        }
        for area in AREAS
    ]

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "asOf": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        # "This week" resets every Monday (Pacific) rather than being a
        # rolling N-day window - see `_current_week_start` and the module
        # docstring. `weekStartAt` tells the frontend exactly which Monday
        # this particular report's "this week" figures are counting from.
        "weekStartAt": datetime.fromtimestamp(week_start, timezone.utc).isoformat(),
        "frTargetHours": FR_TARGET_HOURS,
        "resTargetDays": RES_TARGET_DAYS,
        "weeklyCohorts": _build_weekly_sla_cohorts(cohort_raw, reply_overrides, now),
        "dailyEngagement": _build_daily_engagement(ku_updated, engagement, cohort_since, now),
        "engagement": {
            "complete": engagement["complete"],
            "ticketsTracked": len(engagement["byTicket"]),
            "ticketsNotYetFetched": engagement["notFetched"],
            "countsBots": ENGAGEMENT_COUNT_BOTS,
        },
        "areas": areas,
    }
    _record_history(report)
    return report
