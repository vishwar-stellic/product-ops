"""Partner Insights' "Escalations" column/drilldown - flags partner emails
(synced into Vitally from Gmail/Outlook, not Intercom) that look like a
live or brewing escalation, using Claude against a fixed triage prompt
supplied by the product team (kept close to verbatim in
`_TRIAGE_SYSTEM_PROMPT` below).

## Scope narrowing before anything reaches Claude
Vitally's `source: "google"` conversations (see `vitally_client.py`) are
almost entirely calendar invites/updates and OOO auto-replies once you
look at real data (a single account can have 1000+ of these) -
`Message.type == "inbound"` already narrows to "sent by the partner side,
not Stellic" (mirrors `partner_insights.py`'s Support score using `type`
similarly), but that alone still lets all of that calendar/auto-reply
noise through. `_looks_auto_generated` mechanically drops the
highest-volume, unambiguous cases (calendar invite/response subjects, OOO
auto-reply subjects, a couple of body-text tells) before anything is sent
to Claude - both to keep the token bill sane and because these are never
going to be a "live-fire" candidate anyway. Anything subtler (newsletters,
vendor marketing, recruiting spam, automated system alerts) is left to
Claude's own judgment per the prompt's SCOPE section, since that needs to
read the actual content to decide.

## Incremental caching - "only look at the last email"
Re-running the full triage prompt over 3 days of email on every refresh
would be slow, expensive, and would re-litigate threads Claude already
assessed. Instead, per partner, `cache.read_raw`/`write_raw` (same
raw-JSON-blob pattern as `partner_insights.py`'s support-score log) stores
`{"lastMessageAt": <iso>, "items": [...]}` - the newest message timestamp
already incorporated, and Claude's current tracked-item list. On the next
force-refresh, only messages newer than `lastMessageAt` (but never further
back than `ESCALATION_LOOKBACK_DAYS`, so a long gap between refreshes
doesn't silently expand scope past what the prompt asks for) are fetched
and handed to Claude *alongside* the existing tracked items, with
instructions to adjust (add/update/drop) rather than start over. A partner
with no new eligible email since last time costs nothing - the cached
items are served as-is. "Days since last movement" is deliberately not a
number Claude writes once and which then goes stale - each item carries a
`lastMovementAt` timestamp, and the frontend computes the day count live
on every page load.

Only runs on an explicit forced refresh (the Partner Insights tab's
"Update" button) - there's no separate nightly cron for this, unlike the
Support Report. See `partner_insights.py:build_partner_insights_report`'s
`force` plumbing.

## Where the "link to the thread" bit of the prompt's evidence format
comes from
Vitally's REST API doesn't expose a clickable URL back to the original
Gmail/Outlook thread (`Conversation.externalUrl` is `None` for
Gmail-sourced conversations in this workspace, confirmed against live
data) - so evidence links to that partner's Account page in the Vitally
web app instead (`VITALLY_APP_SUBDOMAIN`, optional - see `.env.example`),
which does surface the same conversation under its "Conversations" tab,
just not deep-linked to the exact thread. Left `None` (not shown as a
link at all) when `VITALLY_APP_SUBDOMAIN` isn't set, rather than guessing
at a subdomain.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import cache
from .vitally_client import VitallyClient

ESCALATION_STATE_CACHE_KEY = "partner-insights-escalations"

# How far back "analyze the last N days worth of emails" looks, per the
# triage prompt's SCOPE section - also the hard cap on how far a stale
# `lastMessageAt` can reach back after a long gap between refreshes (see
# module docstring).
ESCALATION_LOOKBACK_DAYS = 3

# How many of an account's most-recent conversations to walk before giving
# up - conversations come back sorted by `updatedAt` desc (see
# `vitally_client.list_account_conversations`), so this is a safety valve
# for accounts with an unusually high update rate, not the normal stopping
# condition (that's the lookback-window cutoff below).
_MAX_CONVERSATIONS_PER_ACCOUNT = 60

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-latest"


def _anthropic_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set - see .env.example. Needed for Partner Insights' "
            "escalation triage."
        )
    return key


def _anthropic_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)


def escalations_configured() -> bool:
    """Both Vitally (the email source) and Claude (the triage) are needed -
    `False` degrades the same way as the other optional Partner Insights
    columns: the feature just doesn't show up rather than erroring."""
    from .vitally_client import is_configured as vitally_configured

    return _anthropic_configured() and vitally_configured()


def vitally_app_account_url(account_id: str) -> Optional[str]:
    """`None` when `VITALLY_APP_SUBDOMAIN` isn't set - see module
    docstring's "link to the thread" section. Used by
    `partner_insights.py` to give each partner's escalation block a
    best-effort "open in Vitally" link (the account's Conversations tab,
    not the exact thread - Vitally's REST API doesn't expose a deep link
    to that)."""
    subdomain = os.environ.get("VITALLY_APP_SUBDOMAIN")
    if not subdomain:
        return None
    return f"https://{subdomain}.vitally.io/accounts/{account_id}"


# ---------------------------------------------------------------------------
# Mechanical pre-filtering (see module docstring)
# ---------------------------------------------------------------------------

_CALENDAR_SUBJECT_RE = re.compile(
    r"^(invitation|accepted|declined|tentative|updated invitation|canceled event|cancelled event"
    r"|new event|updated event|event reminder|reminder|updated invite|invite)\s*:",
    re.IGNORECASE,
)
_AUTO_REPLY_SUBJECT_RE = re.compile(
    r"(automatic reply|auto-?reply|out of office|away from (my|the) (email|office)|vacation response)",
    re.IGNORECASE,
)
_AUTO_GENERATED_BODY_MARKERS = (
    "you have been invited by",
    "this event has been canceled",
    "this event has been cancelled",
    "when: ",
    "where: ",
    "joining info",
    "google meet joining info",
)


def _strip_html(value: Optional[str]) -> str:
    """Same 2-line helper as `partner_insights.py`/`support_report.py` -
    duplicated locally per this project's existing convention (see those
    modules' docstrings) rather than imported."""
    if not value:
        return ""
    return re.sub(r"<[^>]+>", " ", value).strip()


def _looks_auto_generated(subject: str, body_text: str) -> bool:
    """Mechanical (non-LLM) filter for the highest-volume, unambiguous
    auto-generated cases - calendar invites/responses and OOO auto-replies.
    Deliberately conservative (only the obvious cases) - anything subtler
    (newsletters, marketing, recruiting) is left to Claude per the
    prompt's SCOPE section, since that needs to read actual content to
    judge."""
    if _CALENDAR_SUBJECT_RE.match(subject.strip()):
        return True
    if _AUTO_REPLY_SUBJECT_RE.search(subject):
        return True
    lowered = body_text.lower()
    return any(marker in lowered for marker in _AUTO_GENERATED_BODY_MARKERS)


# ---------------------------------------------------------------------------
# Fetching new, eligible emails from Vitally
# ---------------------------------------------------------------------------


def _resolve_sender(message: Dict[str, Any], full_conversation: Dict[str, Any]) -> str:
    """Best-effort human-readable sender for a message's `from` Participant
    - looked up against the parent conversation's own `users` list (each
    full Conversation response embeds the Users/Admins it involves, see
    module docstring) rather than a separate API call per message."""
    sender_id = (message.get("from") or {}).get("id")
    for user in full_conversation.get("users") or []:
        if user.get("id") == sender_id:
            return user.get("name") or user.get("email") or sender_id or "(unknown)"
    return sender_id or "(unknown)"


def _collect_new_human_emails(
    vitally_client: VitallyClient,
    account_id: str,
    since_iso: str,
) -> List[Dict[str, Any]]:
    """Every partner-authored (`type == "inbound"`), non-auto-generated
    email (`source == "google"`) for one account, strictly newer than
    `since_iso`, oldest first (so Claude reads them in chronological
    order). Conversations arrive sorted by `updatedAt` desc, so this stops
    walking them as soon as it hits one that's entirely too old to
    matter - see `_MAX_CONVERSATIONS_PER_ACCOUNT` for the other (rarer)
    stopping condition."""
    candidates: List[Dict[str, Any]] = []
    checked = 0
    for summary in vitally_client.list_account_conversations(account_id, page_size=25):
        checked += 1
        if checked > _MAX_CONVERSATIONS_PER_ACCOUNT:
            break
        if summary.get("source") != "google":
            continue
        updated_at = summary.get("updatedAt") or ""
        if updated_at and updated_at < since_iso:
            break  # sorted desc - nothing further back can be newer than since_iso either
        full = vitally_client.get_conversation(summary["id"])
        subject = full.get("subject") or "(no subject)"
        for message in full.get("messages") or []:
            if message.get("type") != "inbound":
                continue
            timestamp = message.get("timestamp") or message.get("createdAt") or ""
            if not timestamp or timestamp <= since_iso:
                continue
            body_text = _strip_html(message.get("message"))
            if not body_text or _looks_auto_generated(subject, body_text):
                continue
            candidates.append(
                {
                    "from": _resolve_sender(message, full),
                    "subject": subject,
                    "date": timestamp,
                    "body": body_text[:4000],
                }
            )
    candidates.sort(key=lambda c: c["date"])
    return candidates


# ---------------------------------------------------------------------------
# Claude triage
# ---------------------------------------------------------------------------

# The product team's fixed triage framework, kept close to verbatim - only
# the OUTPUT section is adapted from free-form prose into a strict JSON
# schema (this app needs structured fields to render a table/badges, not a
# markdown essay), and an INCREMENTAL UPDATE section is appended so Claude
# adjusts the existing tracked list rather than re-deriving it from
# scratch every time (see module docstring).
_TRIAGE_SYSTEM_PROMPT = """You are triaging emails from partners for risk.

SCOPE
- Emails within last 3 days. Only from partner emails, not Stellic generated emails. Only human-written emails, not auto generated emails.
- Exclude: newsletters, vendor marketing, recruiting, automated system alerts unless a human replied to them.

WHAT COUNTS AS A LIVE ESCALATION
- Explicit dissatisfaction, complaint, or "this isn't working" with a clear frustrating tone, disambiguate between a bug report and a bug report of consequence.
- A deadline, go-live, or term-start date at risk
- Anything referencing contract, renewal, legal, security review, lack of trust, or exec involvement

WHAT COUNTS AS A BUBBLING FIRE (weak signals — look hard for these)
- A partner asked something twice, or followed up on their own message
- A thread where someone senior got added to the cc line mid-conversation
- Tone shift across a thread: cooperative → formal, or first names → titles
- A thread that went silent after a partner raised a problem
- Words like "still," "again," "as mentioned," "circling back," "any update"
- A commitment we made with a date attached that has now passed
- A workaround being used repeatedly instead of a fix
- Multiple unrelated people at the same institution raising friction in the same period

RULES
- Do not infer or embellish. Every claim needs a quote.
- If a thread is ambiguous, list it under WATCH and say what's unclear rather than guessing.
- If you find nothing, say so plainly (return an empty "items" array). Do not manufacture concern.

INCREMENTAL UPDATE
You are given (1) the currently-tracked escalation items from the last run (may be empty on a first \
run) and (2) new emails received since then for this same partner (may include emails you've never \
seen and, if a thread continued, more from a thread you already tracked). Update the tracked list:
- Add a new item for any new escalation-worthy signal in the new emails.
- If a new email clearly continues/updates a thread you already tracked, update that existing item \
in place (its evidence, severity, blockedOn, lastMovementAt, lastEmailDate) rather than creating a \
duplicate.
- If a new email makes it clear an existing item is now resolved (e.g. a fix confirmed, an apology \
accepted, the ball explicitly no longer with either side), drop it from the list.
- Leave any existing item untouched if none of the new emails relate to it - do not reassess or \
reword it just because this run happened.
- Base every judgment only on the emails actually provided (previous items' own evidence, plus the \
new emails below) - never assume context that isn't shown to you.

OUTPUT
Respond with ONLY a single JSON object and nothing else - no markdown fences, no commentary. Shape:
{{"items": [
  {{
    "headline": "<one line, specific to this partner/thread>",
    "severity": "LIVE_FIRE" | "SMOLDERING" | "WATCH",
    "severityReason": "<why this level, one short sentence>",
    "evidence": [{{"quote": "<short direct quote, <=200 chars>", "sender": "<name>", "date": "<ISO date from the email>"}}],
    "blockedOn": "us" | "them" | "unclear",
    "blockedOnReason": "<one short sentence>",
    "lastMovementAt": "<ISO date of the most recent relevant email>",
    "from": "<sender of the most recent relevant email>",
    "subject": "<subject of the most recent relevant email>",
    "lastEmailDate": "<ISO date of the most recent relevant email>"
  }}
]}}
Ambiguous threads still need every field above - use severity "WATCH" and put what's unclear in \
severityReason.

PREVIOUSLY TRACKED ITEMS (JSON, adjust per INCREMENTAL UPDATE above):
{previous_items}

NEW EMAILS FOR THIS PARTNER (chronological, oldest first):
{new_emails}
"""


def _format_emails_for_prompt(emails: List[Dict[str, Any]]) -> str:
    blocks = []
    for e in emails:
        blocks.append(f"From: {e['from']}\nDate: {e['date']}\nSubject: {e['subject']}\nBody:\n{e['body']}")
    return "\n\n---\n\n".join(blocks)


def _extract_json_object(text: str) -> str:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


_VALID_SEVERITIES = {"LIVE_FIRE", "SMOLDERING", "WATCH"}
_VALID_BLOCKED_ON = {"us", "them", "unclear"}


def _sanitize_item(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    severity = str(raw.get("severity") or "").upper().replace(" ", "_")
    if severity not in _VALID_SEVERITIES:
        severity = "WATCH"
    blocked_on = str(raw.get("blockedOn") or "unclear").lower()
    if blocked_on not in _VALID_BLOCKED_ON:
        blocked_on = "unclear"
    headline = str(raw.get("headline") or "").strip()
    if not headline:
        return None
    evidence = [
        {
            "quote": str(e.get("quote") or "")[:300],
            "sender": str(e.get("sender") or "")[:200],
            "date": str(e.get("date") or ""),
        }
        for e in (raw.get("evidence") or [])
        if isinstance(e, dict) and e.get("quote")
    ][:2]
    return {
        "headline": headline[:300],
        "severity": severity,
        "severityReason": str(raw.get("severityReason") or "")[:400],
        "evidence": evidence,
        "blockedOn": blocked_on,
        "blockedOnReason": str(raw.get("blockedOnReason") or "")[:400],
        "lastMovementAt": str(raw.get("lastMovementAt") or "") or None,
        "from": str(raw.get("from") or "")[:200],
        "subject": str(raw.get("subject") or "")[:300],
        "lastEmailDate": str(raw.get("lastEmailDate") or "") or None,
    }


def _claude_update_escalations(
    previous_items: List[Dict[str, Any]],
    new_emails: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """One Claude call producing the updated tracked-items list - `None` on
    any failure (bad response, timeout, malformed JSON) so one partner's
    flaky call never blocks the rest of the batch (mirrors
    `partner_insights.py:_claude_score_conversation`)."""
    prompt = _TRIAGE_SYSTEM_PROMPT.format(
        previous_items=json.dumps(previous_items, indent=2),
        new_emails=_format_emails_for_prompt(new_emails),
    )
    try:
        response = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": _anthropic_api_key(),
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": _anthropic_model(),
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt[:60000]}],
            },
            timeout=60,
        )
        response.raise_for_status()
        text = response.json()["content"][0]["text"]
        parsed = json.loads(_extract_json_object(text))
        items = [_sanitize_item(i) for i in (parsed.get("items") or []) if isinstance(i, dict)]
        return [i for i in items if i is not None]
    except Exception as exc:  # noqa: BLE001 - one bad partner shouldn't break the batch
        print(f"[escalation_report] Claude triage failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _get_state() -> Dict[str, Any]:
    return cache.read_raw(ESCALATION_STATE_CACHE_KEY) or {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        cache.write_raw(ESCALATION_STATE_CACHE_KEY, state)
    except Exception as exc:  # noqa: BLE001 - best-effort, mirrors support_report.py's history writes
        print(f"[escalation_report] failed to save state: {exc}")


def refresh_partner_escalations(
    registry: List[Dict[str, Any]],
    vitally_client: VitallyClient,
    force: bool,
) -> Dict[str, Any]:
    """`partnerId -> {"items": [...], "checkedAt": <iso>}` for every
    partner with a matched Vitally account. Only does real work (fetching
    new emails, calling Claude) when `force=True` - the Partner Insights
    tab's "Update" button - see module docstring; a passive/cached read
    just serves whatever's already in `cache.read_raw` untouched."""
    state = _get_state()
    if not force or not escalations_configured():
        return state

    now_iso = datetime.now(timezone.utc).isoformat()
    lookback_cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=ESCALATION_LOOKBACK_DAYS)).isoformat()
    partners_with_vitally = [p for p in registry if p.get("vitallyAccountId")]

    def _process(partner: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
        account_id = partner["vitallyAccountId"]
        prior = state.get(partner["partnerId"]) or {}
        # Never look back further than the lookback window even if it's
        # been a while since the last forced refresh (see module
        # docstring) - but never re-fetch anything already incorporated
        # either, hence the max() of the two bounds.
        since_iso = max(prior.get("lastMessageAt") or "", lookback_cutoff_iso)
        try:
            new_emails = _collect_new_human_emails(vitally_client, account_id, since_iso)
        except Exception as exc:  # noqa: BLE001 - one partner's Vitally hiccup shouldn't break the batch
            print(f"[escalation_report] fetch failed for {partner['name']}: {exc}")
            return None
        if not new_emails:
            return partner["partnerId"], prior or {"items": [], "lastMessageAt": None, "checkedAt": now_iso}

        updated_items = _claude_update_escalations(prior.get("items") or [], new_emails)
        if updated_items is None:
            # Claude failed - keep the prior items rather than silently
            # dropping them, but don't advance `lastMessageAt` so these
            # emails get retried next time.
            return partner["partnerId"], {**prior, "checkedAt": now_iso}

        newest_seen = max(e["date"] for e in new_emails)
        return partner["partnerId"], {
            "items": updated_items,
            "lastMessageAt": newest_seen,
            "checkedAt": now_iso,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_process, partners_with_vitally))

    for result in results:
        if result is not None:
            partner_id, payload = result
            state[partner_id] = payload

    _save_state(state)
    return state
