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
Sunday 00:00 *Pacific time* (`_current_week_start` - Pacific to match how
the team refers to dates day-to-day elsewhere, e.g.
`notion_report.py:_PACIFIC`), so it grows through the week and snaps back
down to (near) zero at each Sunday reset - a genuine week-to-date number
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
- `partnerName` - the institution, resolved the same way as the skill's
  `resolve_partner`: the conversation's `company.name` if present, else the
  partner code embedded in a contact's `external_id` (commonly
  `<user>@<code>`, e.g. `cjp260@newcastle`) looked up against a
  `company_id -> name` map built once per refresh from `list_companies`,
  else the requester's email domain against a small manual map for a few
  known non-obvious domains (`_DOMAIN_TO_PARTNER`). Unmatched stays
  "(unknown)" rather than guessing.

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
"""

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import cache
from .intercom_client import IntercomClient

# Matches `notion_report.py:_PACIFIC` - "this week" resets on Pacific-time
# Sundays, not UTC ones, to match how the team actually thinks about weeks.
_PACIFIC = ZoneInfo("America/Los_Angeles")

SUPPORT_REPORT_CACHE_KEY = "dashboard-support-report"

# Bump whenever this module's output shape or underlying metric logic
# changes - see `milestones_report.py:MILESTONES_REPORT_CACHE_VERSION` for
# why (same cache has no schema of its own).
SUPPORT_REPORT_CACHE_VERSION = 5

# Separate raw key (not versioned/aged like the main report - see
# `cache.read_raw`) for the trend chart's accumulating history log.
SUPPORT_REPORT_HISTORY_KEY = "dashboard-support-report-history"
# ~1.5 years of daily snapshots (one point per real refresh, so in practice
# far slower than daily) - generous headroom while keeping the blob small.
SUPPORT_REPORT_HISTORY_MAX_POINTS = 500

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

# Fallback for when a requester's email domain doesn't obviously map to
# their institution's name (Partner resolution's last resort - see
# `_partner_name` and the module docstring). Carried over from the
# support-sla-dashboard skill's manual map.
_DOMAIN_TO_PARTNER = {
    "uchicago.edu": "University of Chicago",
    "uc": "University of Chicago",
    "jh.edu": "Johns Hopkins",
    "uon.edu.au": "The University of Newcastle",
    "osu.edu": "The Ohio State University",
    "case.edu": "Case Western Reserve",
    "csc.edu": "Chadron State College",
    "academyart.edu": "Academy of Art University",
}


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


def _current_week_start(now: float) -> float:
    """Epoch timestamp for 00:00 Pacific time on the most recent Sunday - the
    "this week" boundary for `newKUThisWeek`/`closedKUThisWeek` (see module
    docstring). A calendar week-to-date window, not a rolling trailing-7-days
    one: it resets to (near) zero every Sunday rather than always covering a
    full 7 days."""
    now_pacific = datetime.fromtimestamp(now, _PACIFIC)
    # datetime.weekday(): Monday=0 ... Sunday=6. Days elapsed since the most
    # recent Sunday:
    days_since_sunday = (now_pacific.weekday() + 1) % 7
    sunday_date = (now_pacific - timedelta(days=days_since_sunday)).date()
    week_start = datetime(sunday_date.year, sunday_date.month, sunday_date.day, tzinfo=_PACIFIC)
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


def _build_company_map(client: IntercomClient) -> Dict[str, str]:
    """`company_id` (a short human-set code, e.g. "fsu", "udel") -> company
    name, for every company in the workspace - see `_partner_name`."""
    mapping: Dict[str, str] = {}
    for company in client.list_companies():
        company_id = company.get("company_id")
        name = company.get("name")
        if company_id and name:
            mapping[company_id.lower()] = name
    return mapping


def _partner_name(conversation: Dict[str, Any], company_map: Dict[str, str]) -> str:
    company = conversation.get("company") or {}
    if company.get("name"):
        return company["name"]

    for contact in ((conversation.get("contacts") or {}).get("contacts")) or []:
        external_id = contact.get("external_id") or ""
        if "@" in external_id:
            code = external_id.rsplit("@", 1)[1].strip().lower()
            if code in company_map:
                return company_map[code]

    email = ((conversation.get("source") or {}).get("author") or {}).get("email") or ""
    if "@" in email:
        domain = email.rsplit("@", 1)[1].strip().lower()
        if domain in _DOMAIN_TO_PARTNER:
            return _DOMAIN_TO_PARTNER[domain]

    return "(unknown)"


def _epoch_to_iso(value: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def _ticket_record(
    conversation: Dict[str, Any],
    squad: str,
    squad_label: str,
    reply_overrides: Dict[str, Optional[float]],
    company_map: Dict[str, str],
    contact_name_map: Dict[str, str],
    now: float,
) -> Dict[str, Any]:
    created = conversation.get("created_at")
    priority = _priority(conversation) or "(blank)"
    out_of_resolution = bool(
        created and (now - created) / 86400.0 > RES_TARGET_DAYS and priority in ("Urgent", "High")
    )
    return {
        "id": conversation.get("id"),
        "url": f"https://app.intercom.com/a/inbox/{INTERCOM_INBOX_PREFIX}/inbox/shared/all/conversation/{conversation.get('id')}",
        "squad": squad,
        "squadLabel": squad_label,
        "createdAt": _epoch_to_iso(created),
        "updatedAt": _epoch_to_iso(conversation.get("updated_at")),
        "userName": _user_name(conversation, contact_name_map),
        "partnerName": _partner_name(conversation, company_map),
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
            _ticket_record(c, squad, label, reply_overrides, company_map, contact_name_map, now)
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
    """The accumulated trend history log, for `GET
    /api/support-report/history` - `{"points": [...]}`, oldest first."""
    return cache.read_raw(SUPPORT_REPORT_HISTORY_KEY) or {"points": []}


def build_support_report(client: Optional[IntercomClient] = None) -> Dict[str, Any]:
    client = client or IntercomClient()
    now = time.time()
    week_start = _current_week_start(now)

    # Intercom's search API can't filter on the "Product Area" custom
    # attribute (or its prefix-match semantics), so - like the skill - this
    # pulls each whole cohort once and filters/groups by area in Python
    # (`_area_metrics`) rather than querying per area. Each cohort can be
    # hundreds of conversations and Intercom's search endpoint runs
    # ~10s/page regardless of query, so the four independent pulls run
    # concurrently rather than one after another (a full sequential pull
    # took ~185s in practice; see `vercel.json`'s maxDuration for the
    # resulting worst-case budget on the force-refresh endpoint).
    with ThreadPoolExecutor(max_workers=5) as pool:
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
        closed_future = pool.submit(
            lambda: list(
                client.search_conversations(
                    {"field": "statistics.first_close_at", "operator": ">=", "value": int(week_start)}
                )
            )
        )
        company_map_future = pool.submit(lambda: _build_company_map(client))
        open_raw = open_future.result()
        snoozed_raw = snoozed_future.result()
        created_raw = created_future.result()
        closed_raw = closed_future.result()
        company_map = company_map_future.result()

    # "Open" = open + snoozed, always (see module docstring); a ticket
    # marked Resolved at the ticket-state level is done even if Intercom
    # still shows the conversation itself as open.
    open_register = [c for c in open_raw + snoozed_raw if _ticket_state(c) != RESOLVED_TICKET_STATE]

    # Only *open* (not snoozed) Key User tickets missing a reliable
    # first-reply timestamp need verifying - see `_verify_replies`.
    needs_verification = [
        c
        for c in open_register
        if c.get("state") == "open"
        and _is_key_user(c)
        and not (c.get("statistics") or {}).get("first_admin_reply_at")
    ]
    # These two extra lookups are independent of each other, so run them
    # side by side rather than one after another.
    with ThreadPoolExecutor(max_workers=2) as pool:
        reply_future = pool.submit(_verify_replies, client, needs_verification)
        contact_name_future = pool.submit(
            _build_contact_name_map, client, open_register + created_raw + closed_raw
        )
        reply_overrides = reply_future.result()
        contact_name_map = contact_name_future.result()

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
                now,
                week_start,
            ),
        }
        for area in AREAS
    ]

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "asOf": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        # "This week" resets every Sunday (Pacific) rather than being a
        # rolling N-day window - see `_current_week_start` and the module
        # docstring. `weekStartAt` tells the frontend exactly which Sunday
        # this particular report's "this week" figures are counting from.
        "weekStartAt": datetime.fromtimestamp(week_start, timezone.utc).isoformat(),
        "frTargetHours": FR_TARGET_HOURS,
        "resTargetDays": RES_TARGET_DAYS,
        "areas": areas,
    }
    _record_history(report)
    return report
