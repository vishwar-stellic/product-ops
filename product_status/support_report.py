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
2. New KU tickets this week - created in the trailing `WEEK_WINDOW_DAYS`
   window, regardless of current state.
3. KU tickets closed this week - first closed (`statistics.first_close_at`)
   in that window, regardless of when created.
4. Out of first-response SLA - no genuine admin/bot reply within
   `FR_TARGET_HOURS` *business* hours (weekends don't tick), including
   never-answered.
5. Out of resolution SLA - open more than `RES_TARGET_DAYS` calendar days
   AND priority is Urgent or High.

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
- `userName` - the individual requester (`source.author.name`).
- `partnerName` - the institution, resolved the same way as the skill's
  `resolve_partner`: the conversation's `company.name` if present, else the
  partner code embedded in a contact's `external_id` (commonly
  `<user>@<code>`, e.g. `cjp260@newcastle`) looked up against a
  `company_id -> name` map built once per refresh from `list_companies`,
  else the requester's email domain against a small manual map for a few
  known non-obvious domains (`_DOMAIN_TO_PARTNER`). Unmatched stays
  "(unknown)" rather than guessing.
"""

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .intercom_client import IntercomClient

SUPPORT_REPORT_CACHE_KEY = "dashboard-support-report"

# Bump whenever this module's output shape or underlying metric logic
# changes - see `milestones_report.py:MILESTONES_REPORT_CACHE_VERSION` for
# why (same cache has no schema of its own).
SUPPORT_REPORT_CACHE_VERSION = 3

INTERCOM_INBOX_PREFIX = "g60t55rg"

FR_TARGET_HOURS = 24.0
RES_TARGET_DAYS = 21.0
WEEK_WINDOW_DAYS = 7

RESOLVED_TICKET_STATE = "Resolved"

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


def _user_name(conversation: Dict[str, Any]) -> str:
    author = (conversation.get("source") or {}).get("author") or {}
    return author.get("name") or author.get("email") or "(unknown)"


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
        "userName": _user_name(conversation),
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
    now: float,
    window_start: float,
) -> Dict[str, Any]:
    ku_open = [c for c in open_register if _squad_for(c) == squad and _is_key_user(c)]
    new_ku = [
        c
        for c in created_raw
        if _squad_for(c) == squad and _is_key_user(c) and c.get("created_at") and window_start <= c["created_at"] < now
    ]
    closed_ku = [
        c
        for c in closed_raw
        if _squad_for(c) == squad
        and _is_key_user(c)
        and (c.get("statistics") or {}).get("first_close_at")
        and window_start <= c["statistics"]["first_close_at"] < now
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
        return [_ticket_record(c, squad, label, reply_overrides, company_map, now) for c in conversations]

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


def build_support_report(client: Optional[IntercomClient] = None) -> Dict[str, Any]:
    client = client or IntercomClient()
    now = time.time()
    window_start = now - WEEK_WINDOW_DAYS * 86400

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
                client.search_conversations({"field": "created_at", "operator": ">=", "value": int(window_start)})
            )
        )
        closed_future = pool.submit(
            lambda: list(
                client.search_conversations(
                    {"field": "statistics.first_close_at", "operator": ">=", "value": int(window_start)}
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
    reply_overrides = _verify_replies(client, needs_verification)

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
                now,
                window_start,
            ),
        }
        for area in AREAS
    ]

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "asOf": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "windowDays": WEEK_WINDOW_DAYS,
        "frTargetHours": FR_TARGET_HOURS,
        "resTargetDays": RES_TARGET_DAYS,
        "areas": areas,
    }
