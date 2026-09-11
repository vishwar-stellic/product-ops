"""Partner Insights' "Escalations" column/drilldown - flags partner emails
and Intercom conversations (both mirrored into Vitally - see
`vitally_client.py`) that look like a live or brewing escalation, using an
LLM (OpenAI - see `openai_client.py`) against a fixed triage prompt
supplied by the product team (kept close to verbatim in
`_TRIAGE_SYSTEM_PROMPT` below).

## Scope narrowing before anything reaches the LLM
Only `source in ("google", "intercom")` conversations are considered -
Vitally also mirrors other channels (e.g. Slack) this feature has never
been asked to cover. `source: "google"` (Gmail/Outlook-synced email) is
almost entirely calendar invites/updates and OOO auto-replies once you
look at real data (a single account can have 1000+ of these), so
`_is_partner_authored` (see below) narrows to "sent by the partner side,
not Stellic" before anything else runs, but that alone still lets all of
that calendar/auto-reply noise through. `_looks_auto_generated`
mechanically drops the highest-volume, unambiguous cases (calendar
invite/response subjects, OOO auto-reply subjects, a couple of body-text
tells) before anything is sent to the LLM - both to keep the token bill
sane and because these are never going to be a "live-fire" candidate
anyway. Anything subtler (newsletters, vendor marketing, recruiting spam,
automated system alerts) is left to the LLM's own judgment per the
prompt's SCOPE section, since that needs to read the actual content to
decide.

`source: "intercom"` conversations were added after the standalone
Intercom conversation-scoring feature (this module's `partner_insights.py`
sibling, "Support score") was removed entirely - without this, genuine
partner-reported bugs/issues raised only through Intercom (not email)
would go completely unanalyzed by anything in this app. Confirmed against
live data (University of Wisconsin-Stout, Sep 2026): a partner's bug
report thread existed as an Intercom conversation with correctly-typed
`inbound`/`outbound` messages *and* as three separate Gmail-mirrored
copies of the same thread - but every message in those Gmail copies was
tagged `type: "outbound"` by Vitally regardless of actual author (one
even resolved `from.id` to the partner contact's own user record despite
being marked outbound). That's why `_is_partner_authored` below doesn't
trust `type` alone.

## Authorship: `type` field isn't fully reliable
`Message.type == "inbound"` is Vitally's own signal for "sent by the
partner side, not Stellic" (mirrors the removed Support score's use of
`type` similarly) and is right the overwhelming majority of the time -
but per the live counter-example above, `_is_partner_authored` also
treats a message as partner-authored when its `from.id` matches one of
the conversation's own `users` (external contacts), regardless of what
`type` says, to catch the cases where Vitally's own sync mislabels an
unambiguous reply.

## Incremental caching - "only look at the last email"
Re-running the full triage prompt over 3 days of email on every refresh
would be slow, expensive, and would re-litigate threads already assessed.
Instead, per partner, `cache.read_raw`/`write_raw` (same raw-JSON-blob
pattern as `partner_insights.py`'s support-score log) stores
`{"lastMessageAt": <iso>, "items": [...]}` - the newest message timestamp
already incorporated, and the LLM's current tracked-item list. On the
next force-refresh, only messages newer than `lastMessageAt` (but never
further back than `ESCALATION_LOOKBACK_DAYS`, so a long gap between
refreshes doesn't silently expand scope past what the prompt asks for)
are fetched and handed to the LLM *alongside* the existing tracked items,
with instructions to adjust (add/update/drop) rather than start over. A
partner with no new eligible email since last time costs nothing - the
cached items are served as-is. "Days since last movement" is deliberately
not a number the LLM writes once and which then goes stale - each item
carries a `lastMovementAt` timestamp, and the frontend computes the day
count live on every page load.

Only runs on an explicit forced refresh - either a person clicking the
Partner Insights tab's whole-roster or per-partner "Update" button, or
`GET /api/cron/refresh-escalations` (Vercel Cron, every 2 hours during
business hours - see server.py's `_in_escalation_run_window` and
`vercel.json`). All three ultimately call this same function with
`force=True`; see `partner_insights.py:build_partner_insights_report`'s
`force` plumbing for the button paths.

## Slack alerting on newly-flagged escalations
Whenever a run's LLM triage produces an item that's newly LIVE_FIRE or
SMOLDERING - either a brand-new item, or an existing tracked item that
just got escalated up from a lower severity (e.g. WATCH -> SMOLDERING) -
`_notable_severity_changes` flags it, and `refresh_partner_escalations`
sends one Slack message (`slack_client.send_message`) summarizing every such item
across every partner processed in that run, best-effort (a Slack failure
never breaks the refresh itself - see the try/except around that call).
An item that stays at the same severity run-over-run (already-known
Smoldering, still Smoldering) never re-alerts - only the moment it first
crosses into Fire/Smoldering territory does. "Matching" a new item back
to a prior one uses an exact `headline` match (items have no separate
stable ID) - a reasonable proxy given the triage prompt's own
INCREMENTAL UPDATE instructions keep an existing item's headline
unchanged when updating it in place, only ever writing a new headline
for a genuinely new item.

This fires regardless of which of the three trigger paths above caused
the refresh (cron or either Update button) - deliberately, since the
point is "tell me the moment this happens," not "only tell me if the
scheduled job happens to be the one that notices." No-op entirely when
`SLACK_BOT_TOKEN`/`SLACK_ALERT_TARGET` aren't set (see `.env.example`) -
the latter can be either a person's Slack member ID (DM) or a channel ID
(posts to that channel instead) - see `slack_client.py`'s module
docstring for the difference in setup.

## `recentEmails` - showing the source emails, not just extracted quotes
Alongside `items`, each partner's cached state also carries `recentEmails`
- the raw (subject/from/date/body) emails that the *latest* batch actually
fed to the LLM (capped at `_RECENT_EMAILS_MAX`), so the Partner Insights
drilldown can show the actual source material next to the LLM's
extracted evidence quotes, rather than only the 1-2 quotes per item the
triage prompt happens to pull out. This overwrites on each run (same "only
the newest" framing as `items`) - it is not an accumulating email archive,
just "what did the most recent check actually look at".

## Slack `(source)` links back to the Vitally conversation
Vitally's REST API doesn't return a web URL (`Conversation.externalUrl`
is `None` here), but the app deep-links conversations at
`https://<subdomain>.vitally.io/conversations/active/<conversationId>`
(see `vitally_app_conversation_url`). Each Slack alert uses the Vitally
conversation UUID we matched from the triaged email batch
(`vitallyConversationId` on the finding). Requires
`VITALLY_APP_SUBDOMAIN` (see `.env.example`); falls back to the partner's
Vitally account page only when no conversation id was matched.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import cache, openai_client, slack_client
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

# Cap on how many raw emails from the latest triage batch are kept in state
# per partner (see `refresh_partner_escalations`'s `recentEmails`) - shown
# alongside the LLM's findings in the Partner Insights drilldown so a
# reviewer can read the actual source emails, not just the extracted
# evidence quotes. Each body is already capped at 4000 chars (see
# `_collect_new_human_emails`), so this bounds total cache size, not per-
# email size.
_RECENT_EMAILS_MAX = 25


def escalations_configured() -> bool:
    """Both Vitally (the email source) and the LLM (the triage) are
    needed - `False` degrades the same way as the other optional Partner
    Insights columns: the feature just doesn't show up rather than
    erroring."""
    from .vitally_client import is_configured as vitally_configured

    return openai_client.is_configured() and vitally_configured()


def _vitally_app_subdomain() -> Optional[str]:
    subdomain = os.environ.get("VITALLY_APP_SUBDOMAIN")
    return subdomain or None


def vitally_app_account_url(account_id: str) -> Optional[str]:
    """`None` when `VITALLY_APP_SUBDOMAIN` isn't set - see module
    docstring's "link to the thread" section. Used by
    `partner_insights.py` to give each partner's escalation block a
    best-effort "open in Vitally" link (the account's Conversations tab,
    not the exact thread - Vitally's REST API doesn't expose a deep link
    to that)."""
    subdomain = _vitally_app_subdomain()
    if not subdomain:
        return None
    return f"https://{subdomain}.vitally.io/accounts/{account_id}"


def vitally_app_conversation_url(conversation_id: str) -> Optional[str]:
    """Deep link to one Vitally conversation in the web app
    (`/conversations/active/<id>`). Same subdomain requirement as
    `vitally_app_account_url`. Not returned by Vitally's REST API - built
    from the conversation id we already have from `get_conversation`."""
    subdomain = _vitally_app_subdomain()
    if not subdomain or not conversation_id:
        return None
    return f"https://{subdomain}.vitally.io/conversations/active/{conversation_id}"


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
    (newsletters, marketing, recruiting) is left to the LLM per the
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


# Vitally conversation sources this feature looks at - see module
# docstring's "Scope narrowing" section for why each is included.
_ELIGIBLE_SOURCES = ("google", "intercom")


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


def _is_partner_authored(message: Dict[str, Any], full_conversation: Dict[str, Any]) -> bool:
    """A message counts as partner-authored either when Vitally's own
    `type` field says `"inbound"` (the normal, reliable case), or - since
    that field turns out to mislabel some genuinely partner-authored
    messages as `"outbound"` (confirmed against live data, see module
    docstring's "Authorship" section) - when the message's `from.id`
    matches one of the conversation's own `users` (its external
    contacts), regardless of what `type` says."""
    if message.get("type") == "inbound":
        return True
    sender_id = (message.get("from") or {}).get("id")
    if not sender_id:
        return False
    return any(user.get("id") == sender_id for user in full_conversation.get("users") or [])


def _collect_new_human_emails(
    vitally_client: VitallyClient,
    account_id: str,
    since_iso: str,
) -> List[Dict[str, Any]]:
    """Every partner-authored (`_is_partner_authored`), non-auto-generated
    message (`source` in `_ELIGIBLE_SOURCES`) for one account, strictly
    newer than `since_iso`, oldest first (so the LLM reads them in
    chronological order). Conversations arrive sorted by `updatedAt` desc,
    so this stops walking them as soon as it hits one that's entirely too
    old to matter - see `_MAX_CONVERSATIONS_PER_ACCOUNT` for the other
    (rarer) stopping condition."""
    candidates: List[Dict[str, Any]] = []
    checked = 0
    for summary in vitally_client.list_account_conversations(account_id, page_size=25):
        checked += 1
        if checked > _MAX_CONVERSATIONS_PER_ACCOUNT:
            break
        if summary.get("source") not in _ELIGIBLE_SOURCES:
            continue
        updated_at = summary.get("updatedAt") or ""
        if updated_at and updated_at < since_iso:
            break  # sorted desc - nothing further back can be newer than since_iso either
        conversation_id = summary["id"]
        full = vitally_client.get_conversation(conversation_id)
        subject = full.get("subject") or "(no subject)"
        for message in full.get("messages") or []:
            if not _is_partner_authored(message, full):
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
                    "vitallyConversationId": conversation_id,
                }
            )
    candidates.sort(key=lambda c: c["date"])
    return candidates


# ---------------------------------------------------------------------------
# LLM triage
# ---------------------------------------------------------------------------

# The product team's fixed triage framework, kept close to verbatim - only
# the OUTPUT section is adapted from free-form prose into a strict JSON
# schema (this app needs structured fields to render a table/badges, not a
# markdown essay), and an INCREMENTAL UPDATE section is appended so the
# LLM adjusts the existing tracked list rather than re-deriving it from
# scratch every time (see module docstring).
_TRIAGE_SYSTEM_PROMPT = """You are triaging emails from partners for risk.

Your job is to score each distinct issue 0-5 on real operational consequence.
Only 4 and 5 are fires. Everything else is context. Most real problems are 3s.
Being sparing is correct - a list where everything is a fire is a useless list.

SCOPE
- Emails within last 3 days. Only from partner emails, not Stellic generated emails. Only \
human-written emails, not auto generated emails.
- Exclude: newsletters, vendor marketing, recruiting, automated system alerts unless a human \
replied to them.

THE TWO QUESTIONS THAT DETERMINE SCORE
Answer both before scoring. They dominate everything else.

1. BLAST RADIUS - who is actually blocked right now?
   - Many students or many staff, or multiple campuses ....... high
   - One student, one advisor, one record ..................... low
   - Nobody yet; this is a question, a request, or a risk ..... none

2. ENVIRONMENT - where is it happening?
   - Production, or post-go-live ............................... high
   - Test, staging, sandbox, pre-integration .................. low
   Production is close to a prerequisite for 4+. A broken thing in test that
   nobody is depending on this week is a 3, no matter how broken.

SCORING RUBRIC

Score 5 - FIRE. Reserve this. Requires ONE of:
  (a) Students or staff are blocked in Production from completing a time-bound
      institutional action - registering, enrolling, adding/dropping, applying
      to graduate, being certified - AND the deadline has passed or lands
      within roughly 48 hours. The irreversibility is what makes it a 5.
  (b) A cluster of separate incidents at one partner that the partner
      themselves ties to go-live viability or to losing trust in Stellic.
      Not three tickets. Three tickets plus the partner saying the go-live or
      the relationship is in question.

Score 4 - FIRE. Requires at least ONE of:
  (a) A Production or post-go-live defect affecting many students or staff, or
      spanning multiple campuses or programs.
  (b) A specific, named, partner-facing event within roughly three weeks is at
      risk: advisor training on a date, a go-live, a production cutover, a
      scheduled student notification, a scheduled key or credential rotation.
      The event must be named and dated. "Before the semester" is not a date.
  (c) The partner is visibly escalating: asking for commitments and dates,
      requesting an urgent meeting, saying they are losing confidence, or
      pulling in someone senior on their side to force movement.
  (d) A Production defect that has recurred after Stellic told them it was
      fixed. The broken promise is what elevates it, not the repetition.
  (e) A daily operational pipeline is down in Production - data refresh
      failed, sync failed, API returning 5xx - such that the partner cannot
      run their day.

Score 3 - REAL, NOT A FIRE. This is the default for a genuine problem.
  Typical shapes, all of which are 3s:
  - A request for an ETA, roadmap, timeline, or plan. Even an impatient one.
  - A configuration or "how should we send this" question.
  - An enhancement or feature request, however reasonable.
  - A single student, single record, or single advisor affected.
  - A defect in test, staging, or pre-integration with no dated dependency.
  - A follow-up on an open ticket with no deadline attached.
  - Contract or legal language being drafted, negotiated, or reviewed.
  - An access, whitelisting, or SSO request with no date attached.
  - A bug with a working workaround in place.

Score 0-2 - NOT AN ESCALATION AT ALL.
  - Meeting logistics: cannot attend, rescheduling, sending invites, confirming times.
  - A process step proceeding normally - a security review underway, an InfoSec
    questionnaire submitted and now with their legal team. Normal process is not
    an escalation just because it says "legal" or "security".
  - Problems with Stellic's own internal tooling rather than the product.
  - Informational updates, FYIs, acknowledgements.

MODIFIERS - these adjust, they never determine
These are the weak signals. They are NOT evidence of a fire on their own. In
practice, follow-up language appears more often on non-fires than on fires,
because following up on an open ticket is normal partner behavior.
  - Asked twice, "still", "again", "as mentioned", "circling back", "any update"
  - Tone shift across a thread: cooperative to formal, first names to titles
  - Someone senior added to cc mid-conversation
  - A thread that went silent after the partner raised a problem
  - A workaround being used repeatedly instead of a fix
  - Multiple unrelated people at the same institution raising friction

Use them ONLY like this:
  - They can lift a 3 to a 4 when the item ALREADY has Production impact or a
    named dated event. Repetition on top of real consequence means we are
    failing to respond to something that matters.
  - They can NEVER lift a 3 to a 4 on their own.
  - They can NEVER lift a 0-2 at all. A partner following up three times about
    a meeting time is still a meeting time.

CALIBRATION EXAMPLES - these are real, human-scored
  5  Students cannot enroll or add classes in Prod; add deadline has passed.
  5  Multiple grading, audit and catalog incidents together threaten the SOM
     go-live and the partner's trust.
  4  Data refresh failed to run today; partner requests overnight refresh.
  4  Advisor training Friday Sept 11 at risk over unresolved Plan Review
     definitions.
  4  Partner escalates lack of API support and requests a meeting tomorrow 2pm.
  4  Post-go-live: prereq checks failing, GPA wrong, double-counting, across
     several campuses.
  4  Production seat availability API returning 500 errors.
  4  Missing registration buttons still occurring after a reported August fix.
  4  Fix identified but not deployed, needed before the 22/09 student email.
  3  Partner requests a roadmap and ETA for CARE module role permissions.
  3  Partner asks which repeat codes Stellic accepts for Workday mappings.
  3  One advisor still sees "No Concentration" after admin work; workaround given.
  3  Partner cannot clone students in the test environment.
  3  Data integration blocked by 401 errors in test and prod, pre-go-live,
     partner followed up.
  3  Google Calendar integration needs new contract language; partner sent a draft.
  3  Enhancement submitted for per-student term load limits.
  0  InfoSec responses submitted, now under the partner's internal legal review.
  0  Partner cannot join a scheduled meeting this morning.
  0  User locked out of Stellic's own monday.com by unexpected MFA.

Note the pairs that look similar and score differently:
  - "401 errors blocking integration, pre-go-live, partner followed up" = 3.
    "Data refresh failed today in Production" = 4. Same family, different
    environment and different immediacy.
  - "Training Sept 30 may be impacted, partner requests timeline" = 3.
    "Training Sept 11 at risk over unresolved definitions" = 4. Proximity and
    whether the blocker is live, not just anticipated.
  - "Security review underway" = 0. "Partner says they are losing confidence" = 4.

RULES
- Do not infer or embellish. Every claim needs a quote.
- Score from what the email actually says, not from how urgent it sounds.
  Frustrated tone about a single test-environment record is still a 3. Calm
  tone about students blocked in Production past a deadline is still a 5.
- If you cannot tell whether it is Production, say so in severityReason and
  cap the score at 3.
- If a thread is ambiguous, score it 3 and say what's unclear rather than guessing.
- If you find nothing, say so plainly (return an empty "items" array). Do not
  manufacture concern. An empty list is a valid and common result.

INCREMENTAL UPDATE
You are given (1) the currently-tracked escalation items from the last run (may be empty on a first \
run) and (2) new emails received since then for this same partner (may include emails you've never \
seen and, if a thread continued, more from a thread you already tracked). Update the tracked list:
- Add a new item for any new escalation-worthy signal in the new emails.
- If a new email clearly continues/updates a thread you already tracked, update that existing item \
in place (its evidence, score, severity, blockedOn, lastMovementAt, lastEmailDate) rather than \
creating a duplicate.
- Re-score on update. A 3 becomes a 4 when it reaches Production, when a date attaches to it, or \
when the partner starts asking for commitments. A 4 drops back to 3 when the dated event passes \
without incident or the blast radius turns out to be one student.
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
    "score": 0 | 1 | 2 | 3 | 4 | 5,
    "isFire": true | false,
    "severity": "LIVE_FIRE" | "SMOLDERING" | "WATCH",
    "severityReason": "<why this score - name the blast radius and the environment, one sentence>",
    "blastRadius": "<who is blocked and roughly how many>",
    "environment": "production" | "test" | "staging" | "not_applicable" | "unclear",
    "datedEvent": "<the named event and its date, or null>",
    "evidence": [{{"quote": "<short direct quote, <=200 chars>", "sender": "<name>", "date": "<ISO date from the email>"}}],
    "blockedOn": "us" | "them" | "unclear",
    "blockedOnReason": "<one short sentence>",
    "lastMovementAt": "<ISO date of the most recent relevant email>",
    "from": "<sender of the most recent relevant email>",
    "subject": "<subject of the most recent relevant email>",
    "lastEmailDate": "<ISO date of the most recent relevant email>"
  }}
]}}

Field rules:
- "isFire" is true if and only if score is 4 or 5.
- "severity" is derived strictly from score: 5 -> "LIVE_FIRE", 4 -> "SMOLDERING", 0-3 -> "WATCH".
  Never set severity independently of score.
- "datedEvent" must be null unless a specific date or day is named in the email.
- Ambiguous threads still need every field - use score 3 and put what's unclear in severityReason.

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
        "vitallyConversationId": str(raw.get("vitallyConversationId") or "") or None,
    }


def _normalize_subject_for_match(subject: str) -> str:
    """Case/prefix-insensitive subject key for matching LLM items back to
    Vitally threads (Re:/Fwd: chains often differ slightly in casing)."""
    normalized = re.sub(r"\s+", " ", (subject or "").strip().lower())
    while True:
        stripped = re.sub(r"^(re|fw|fwd):\s*", "", normalized)
        if stripped == normalized:
            break
        normalized = stripped
    return normalized


def _match_conversation_id(item: Dict[str, Any], source_emails: List[Dict[str, Any]]) -> Optional[str]:
    """Best-effort link from an LLM item back to the Vitally conversation
    that supplied its source messages - matched on subject/from/date
    against the batch that was just analyzed."""
    if not source_emails:
        return None
    subject = (item.get("subject") or "").strip()
    subject_key = _normalize_subject_for_match(subject)
    last_date = item.get("lastEmailDate") or item.get("lastMovementAt")
    sender = (item.get("from") or "").strip()

    candidates = source_emails
    if subject_key:
        by_subject = [
            e
            for e in source_emails
            if _normalize_subject_for_match(e.get("subject") or "") == subject_key
        ]
        if by_subject:
            candidates = by_subject

    if last_date:
        for email in candidates:
            if email.get("date") == last_date and email.get("vitallyConversationId"):
                return email["vitallyConversationId"]

    if sender:
        for email in candidates:
            if email.get("from") == sender and email.get("vitallyConversationId"):
                return email["vitallyConversationId"]

    if len(candidates) == 1 and candidates[0].get("vitallyConversationId"):
        return candidates[0]["vitallyConversationId"]

    dated = [e for e in candidates if e.get("vitallyConversationId")]
    if dated:
        return max(dated, key=lambda e: e.get("date") or "")["vitallyConversationId"]
    return None


def _enrich_items_with_source_conversations(
    items: List[Dict[str, Any]],
    source_emails: List[Dict[str, Any]],
    prior_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach `vitallyConversationId` to each tracked item so Slack alerts
    and future runs can deep-link back to the Vitally thread."""
    prior_by_headline = {item.get("headline"): item for item in prior_items}
    enriched: List[Dict[str, Any]] = []
    for item in items:
        row = dict(item)
        conversation_id = _match_conversation_id(row, source_emails)
        if not conversation_id:
            prior = prior_by_headline.get(row.get("headline"))
            if prior:
                conversation_id = prior.get("vitallyConversationId")
        if conversation_id:
            row["vitallyConversationId"] = conversation_id
        enriched.append(row)
    return enriched


def _update_escalations(
    previous_items: List[Dict[str, Any]],
    new_emails: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """One LLM call (`openai_client.chat_completion`) producing the
    updated tracked-items list - `None` on any failure (bad response,
    timeout, malformed JSON) so one partner's flaky call never blocks the
    rest of the batch (mirrors
    `partner_insights.py:_score_conversation`)."""
    prompt = _TRIAGE_SYSTEM_PROMPT.format(
        previous_items=json.dumps(previous_items, indent=2),
        new_emails=_format_emails_for_prompt(new_emails),
    )
    try:
        text = openai_client.chat_completion(prompt[:60000], max_completion_tokens=6000)
        parsed = json.loads(_extract_json_object(text))
        items = [_sanitize_item(i) for i in (parsed.get("items") or []) if isinstance(i, dict)]
        return [i for i in items if i is not None]
    except Exception as exc:  # noqa: BLE001 - one bad partner shouldn't break the batch
        print(f"[escalation_report] LLM triage failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_NOTABLE_SEVERITIES = {"LIVE_FIRE", "SMOLDERING"}


def _notable_severity_changes(
    prior_items: List[Dict[str, Any]],
    updated_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Items in `updated_items` that just became LIVE_FIRE/SMOLDERING this
    run - either brand new (no prior item with the same `headline`), or an
    existing item that just got escalated up from a lower severity (e.g.
    WATCH -> SMOLDERING, SMOLDERING -> LIVE_FIRE) - for the Slack alert
    (see module docstring's "Slack alerting" section). An item that was
    already at the same severity last run is never included, so a
    long-running Fire/Smoldering item doesn't re-alert on every 2-hour
    check."""
    prior_by_headline = {item.get("headline"): item for item in prior_items}
    notable = []
    for item in updated_items:
        if item.get("severity") not in _NOTABLE_SEVERITIES:
            continue
        prior_match = prior_by_headline.get(item.get("headline"))
        if prior_match is None or prior_match.get("severity") != item.get("severity"):
            notable.append(item)
    return notable


_SEVERITY_SLACK_LABEL = {"LIVE_FIRE": "Live Fire", "SMOLDERING": "Smoldering"}


def _format_slack_summary(notable_changes: List[Dict[str, Any]]) -> str:
    """Slack mrkdwn text for one or more newly-notable items, grouped in
    the order they were processed (partners run concurrently, so this
    isn't a meaningful ranking - just stable enough to read). See module
    docstring's "Slack alerting" section for what counts as "newly
    notable"."""
    noun = "escalation" if len(notable_changes) == 1 else "escalations"
    lines = [f"*{len(notable_changes)} new {noun} flagged* (Partner Insights, automatic Vitally check):"]
    for item in notable_changes:
        label = _SEVERITY_SLACK_LABEL.get(item.get("severity"), item.get("severity"))
        header = f"\u2022 *{label}* \u2014 *{item.get('partnerName')}*: {item.get('headline')}"
        source_url = vitally_app_conversation_url(item.get("vitallyConversationId") or "")
        if not source_url:
            source_url = item.get("vitallyAccountUrl")
        if source_url:
            header += f" (<{source_url}|source>)"
        lines.append(header)
        evidence = item.get("evidence") or []
        if evidence:
            lines.append(f"    > {evidence[0].get('quote')}")
    return "\n".join(lines)


def _notify_slack(notable_changes: List[Dict[str, Any]]) -> None:
    """Best-effort - a Slack failure should never break the escalation
    refresh itself (mirrors this module's other "one bad thing shouldn't
    break the batch" try/excepts)."""
    if not notable_changes or not slack_client.is_configured():
        return
    try:
        slack_client.send_message(_format_slack_summary(notable_changes))
    except Exception as exc:  # noqa: BLE001
        print(f"[escalation_report] Slack notification failed: {exc}")


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
    new emails, calling the LLM) when `force=True` - a person clicking
    either Update button, or the Vercel Cron hitting
    `/api/cron/refresh-escalations` - see module docstring; a passive/
    cached read just serves whatever's already in `cache.read_raw`
    untouched. Also sends a Slack DM for any newly-flagged Fire/Smoldering
    item across this run (see module docstring's "Slack alerting"
    section) - a side effect, not reflected in this function's return
    value, so no caller needs to change to pick this up."""
    state = _get_state()
    if not force or not escalations_configured():
        return state

    now_iso = datetime.now(timezone.utc).isoformat()
    lookback_cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=ESCALATION_LOOKBACK_DAYS)).isoformat()
    partners_with_vitally = [p for p in registry if p.get("vitallyAccountId")]

    def _process(partner: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]]:
        account_id = partner["vitallyAccountId"]
        prior = state.get(partner["partnerId"]) or {}
        prior_items = prior.get("items") or []
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
            prior_items = prior.get("items") or []
            if any(not item.get("vitallyConversationId") for item in prior_items):
                backfill_emails = _collect_new_human_emails(
                    vitally_client, account_id, lookback_cutoff_iso
                )
                enriched_items = _enrich_items_with_source_conversations(
                    prior_items, backfill_emails, prior_items
                )
                payload = {**prior, "items": enriched_items, "checkedAt": now_iso}
            else:
                payload = prior or {"items": [], "lastMessageAt": None, "checkedAt": now_iso}
            return partner["partnerId"], payload, []

        updated_items = _update_escalations(prior_items, new_emails)
        if updated_items is None:
            # The LLM call failed - keep the prior items rather than silently
            # dropping them, but don't advance `lastMessageAt` so these
            # emails get retried next time.
            return partner["partnerId"], {**prior, "checkedAt": now_iso}, []

        updated_items = _enrich_items_with_source_conversations(updated_items, new_emails, prior_items)
        if any(not item.get("vitallyConversationId") for item in updated_items):
            # Items can predate conversation-id capture, or subjects can
            # drift across incremental runs - widen to the full lookback
            # window and try again before giving up on Slack source links.
            backfill_emails = _collect_new_human_emails(vitally_client, account_id, lookback_cutoff_iso)
            updated_items = _enrich_items_with_source_conversations(
                updated_items, backfill_emails, updated_items
            )

        newest_seen = max(e["date"] for e in new_emails)
        payload = {
            "items": updated_items,
            "lastMessageAt": newest_seen,
            "checkedAt": now_iso,
            # The raw emails this batch actually analyzed - see
            # `_RECENT_EMAILS_MAX`. Overwrites (not appends to) the prior
            # batch's list, same "only the newest" framing as `items`
            # itself - this is "what did we just look at", not an
            # accumulating email archive.
            "recentEmails": new_emails[-_RECENT_EMAILS_MAX:],
        }
        notable = [
            {**item, "partnerName": partner["name"], "vitallyAccountUrl": vitally_app_account_url(account_id)}
            for item in _notable_severity_changes(prior_items, updated_items)
        ]
        return partner["partnerId"], payload, notable

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_process, partners_with_vitally))

    notable_changes: List[Dict[str, Any]] = []
    for result in results:
        if result is not None:
            partner_id, payload, notable = result
            state[partner_id] = payload
            notable_changes.extend(notable)

    _save_state(state)
    _notify_slack(notable_changes)
    return state
