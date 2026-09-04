"""Partner Insights dashboard tab - a per-partner (institution) rollup of
independent scores:

## Product score - split into Bug score and Feature score (Linear only, no
## LLM, cheap - recomputed on every refresh)
For each partner, every Linear issue linked to it via a `CustomerNeed` (see
`partner_identity.py`'s module docstring) is fetched once
(`_fetch_customer_linked_issues`, filtered to `needs: {some: {}}` so the
result set is just customer-linked issues, not the whole workspace) and
grouped client-side by customer id - one bulk query rather than one query
per partner. Each issue is bucketed bug (carries the `Bug` label, same
`quality.BUG_LABEL` as the EPD dashboard's own SLA metrics) vs. feature
request/other (everything else - genuine feature requests plus any other
non-Bug-labeled ask).

`bugScore` (0-100) is driven by bug-SLA responsiveness (`1 - (breached +
failed this month) / SLA-eligible bugs`, defaulting to 100 when a partner
has no SLA-eligible bugs at all), derived from the directly-queryable
`slaBreachesAt` timestamp rather than Linear's `slaStatus` *filter* (that
one's filter-only - it can't be selected as a field, see `quality.py`'s
docstring for why the filter version exists) - comparing it against
`completedAt`/`canceledAt`/now reproduces the same Breached/Failed/
Completed semantics. "SLA-eligible bugs" (the denominator) is scoped to
currently-open bugs only (`_is_open_state` - Backlog/Todo/In Progress/In
Review/Triage, i.e. not Done/Canceled) - a bug that already closed cleanly
within SLA isn't part of today's open workload. "Failed this month" is the
deliberate exception: it's inherently about already-closed bugs (missed
SLA before closing), so it keeps using the full bug set rather than the
open-only one - see `_product_metrics_for_customer`'s inline comments for
exactly which rows are open-only vs. all-statuses (a per-row product
decision, not a blanket rule).

`featureScore` (0-100) has no formal SLA to measure against, so it's a
staleness proxy instead: `1 - (feature requests open longer than
FEATURE_STALE_DAYS with no resolution) / total *open* feature requests`,
defaulting to 100 when a partner has none open. Both scores are shown
separately (never folded into one number) so it's clear which side of the
backlog is driving a partner's standing; raw volume/count breakdowns are
reported alongside for context, each paired with a `*Url` field
(`_multi_issue_url`) so the frontend can make every count clickable,
opening the exact matching issues as an ad-hoc Linear list view - "total"
counts/links are open-only (matching the score denominators above), while
"new this month" counts/links intentionally include every status (a bug
opened and already closed within the same month still counts as new
incoming work). Weights/definitions live as named constants below so
they're easy to retune later.

## Support score (Intercom + Claude, incremental - one small batch/day)
Scoring full conversation transcripts via an LLM on every refresh would be
slow and needlessly re-scores the same history repeatedly. Instead, once
roughly every `_BATCH_MIN_INTERVAL_SECONDS` (~20h, so it lines up with the
normal once-a-day cache refresh), `_run_daily_scoring_batch` pulls
conversations closed in roughly the last 24h, resolves each to a partner
via `partner_identity.partner_name`, and scores the support team's side of
each with Claude (professionalism / helpfulness / how "canned" the replies
read) - see `_claude_score_conversation`. Every scored conversation is
appended *once* to a small accumulating log
(`cache.read_raw`/`write_raw`, same pattern as
`support_report.py`'s trend history) and never rescored - the log only
ever grows. `compute_support_scores` reads that log and averages every
partner's entries within a trailing window (`SUPPORT_SCORE_WINDOW_DAYS`)
into one composite score.

Net effect: a partner's Support score starts as "no data yet" and fills in
day by day as the daily batch runs - there's no backfill of history from
before this tab existed, by explicit design (this was scoped as
incremental-only, not a one-time bulk score of everything).

## Vitally health score (Vitally only, no computation - recomputed on
## every refresh)
Vitally (a customer-success platform, separate from both Linear and
Intercom) already computes its own per-account `healthScore` - in this
workspace it's a direct 0/5/10 encoding of a manually-set Red/Yellow/Green
"pulse" trait imported from a CSV upload (confirmed against live data:
Red -> 0, Yellow -> 5, Green -> 10 with no exceptions), so it's shown as-is
rather than recomputed - this tab doesn't try to second-guess a human
judgment call that already exists elsewhere. `None` when the partner has
no matching Vitally account (`vitally_client.py`) or `VITALLY_ACCESS_TOKEN`
isn't configured (`vitallyConfigured` in the report - graceful degradation,
same pattern as `claudeConfigured` below).

## Partners this tab shows
See `partner_identity.build_partner_registry` - Intercom companies and
Linear customers that couldn't be cross-referenced are still shown (with
whichever score is available), not dropped.
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from . import cache
from .intercom_client import IntercomClient
from .linear_client import LinearClient
from .partner_identity import build_company_map, build_partner_registry, partner_name
from .quality import BUG_LABEL, _month_bounds
from .support_report import INTERCOM_INBOX_PREFIX
from .vitally_client import VitallyClient
from .vitally_client import is_configured as vitally_configured

PARTNER_INSIGHTS_CACHE_KEY = "dashboard-partner-insights"
# Bump whenever this module's output shape or underlying metric logic
# changes - see `milestones_report.py:MILESTONES_REPORT_CACHE_VERSION` for
# why (the cache backend has no schema of its own).
PARTNER_INSIGHTS_CACHE_VERSION = 5

# Separate raw key (accumulating log, not aged/versioned like the main
# report - see `cache.read_raw`) for Claude-scored conversations. Never
# rewritten for old entries, only appended to - see module docstring.
PARTNER_INSIGHTS_SUPPORT_LOG_KEY = "partner-insights-support-scores"
# Generous cap on total logged conversations across every partner combined
# (mirrors `support_report.SUPPORT_REPORT_HISTORY_MAX_POINTS`'s reasoning) -
# oldest entries drop off once exceeded.
PARTNER_INSIGHTS_SUPPORT_LOG_MAX_ENTRIES = 20000
# Gates the daily Claude batch to roughly once/day regardless of how often
# the outer 24h cache happens to get invalidated (a cache-version bump, or
# repeated manual "Update" clicks) - see module docstring.
_BATCH_MIN_INTERVAL_SECONDS = 20 * 60 * 60
# How far back the *support* score looks once conversations start
# accumulating in the log - see module docstring.
SUPPORT_SCORE_WINDOW_DAYS = 30
# A still-open feature request older than this with no resolution counts
# against `featureScore` - see module docstring.
FEATURE_STALE_DAYS = 90

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-latest"


# ---------------------------------------------------------------------------
# Product score (Linear)
# ---------------------------------------------------------------------------

_CUSTOMER_LINKED_ISSUES_QUERY = """
query PartnerInsightsIssues($first: Int!, $after: String) {
  issues(first: $first, after: $after, filter: { needs: { some: {} } }) {
    nodes {
      id
      identifier
      title
      url
      createdAt
      completedAt
      canceledAt
      priority
      slaBreachesAt
      state { type }
      labels { nodes { name } }
      needs { nodes { customer { id } } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Linear's workflow state `type` is a fixed 6-value string (unlike state
# *names*, which are freely renamed/added per-workspace - see the "commit
# and deploy" conversation's live sample: {backlog: Backlog, unstarted:
# Todo, started: In Review, triage: Triage, completed: Done/Merged}) - an
# explicit allowlist of the 4 non-terminal ones, rather than "not
# completed/canceled", so a hypothetical future 7th type defaults to
# *excluded* rather than silently counted as open.
_OPEN_STATE_TYPES = {"backlog", "unstarted", "started", "triage"}


def _is_open_state(issue: Dict[str, Any]) -> bool:
    return ((issue.get("state") or {}).get("type")) in _OPEN_STATE_TYPES

_LINEAR_ISSUE_URL_RE = re.compile(r"^(https://linear\.app/[^/]+)/")


def _linear_workspace_base_url(issues: List[Dict[str, Any]]) -> Optional[str]:
    """`https://linear.app/<workspace>`, sniffed from the first issue's own
    `url` rather than a separate `organization` query/env var - every
    customer-linked issue already carries one, so there's always a sample
    to sniff from whenever there's anything to link to."""
    for issue in issues:
        match = _LINEAR_ISSUE_URL_RE.match(issue.get("url") or "")
        if match:
            return match.group(1)
    return None


def _multi_issue_url(workspace_base_url: Optional[str], issues: List[Dict[str, Any]]) -> Optional[str]:
    """A single Linear URL that opens exactly `issues` as an ad-hoc list
    view - see https://linear.app/docs/custom-views ("share or revisit a
    one-off set of issues" via `/issues/ID-1,ID-2,...`). `None` (not
    clickable) when there's nothing to show or the workspace couldn't be
    sniffed."""
    identifiers = [i["identifier"] for i in issues if i.get("identifier")]
    if not identifiers or not workspace_base_url:
        return None
    return f"{workspace_base_url}/issues/{','.join(identifiers)}"


def _fetch_customer_linked_issues(client: LinearClient) -> List[Dict[str, Any]]:
    """Every Linear issue with at least one `CustomerNeed` attached - one
    bulk paginated query rather than one query per partner (see module
    docstring)."""
    return client.paginate(_CUSTOMER_LINKED_ISSUES_QUERY, variables={}, path=["issues"], page_size=100)


def _group_issues_by_customer(issues: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_customer: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues:
        customer_ids = {
            need["customer"]["id"]
            for need in ((issue.get("needs") or {}).get("nodes") or [])
            if need.get("customer") and need["customer"].get("id")
        }
        for customer_id in customer_ids:
            by_customer.setdefault(customer_id, []).append(issue)
    return by_customer


def _is_bug(issue: Dict[str, Any]) -> bool:
    return any(label.get("name") == BUG_LABEL for label in (issue.get("labels") or {}).get("nodes") or [])


def _in_range(iso_value: Optional[str], start: str, end: str) -> bool:
    return bool(iso_value) and start <= iso_value < end


def _sla_bucket(issue: Dict[str, Any], now_iso: str) -> Optional[str]:
    """"breached" | "failed" | "met" | None (not SLA-eligible - no
    `slaBreachesAt` at all, i.e. not Urgent/High priority, matching
    `quality.py`'s docstring on which bugs carry an SLA in this
    workspace)."""
    sla_breaches_at = issue.get("slaBreachesAt")
    if not sla_breaches_at:
        return None
    closed_at = issue.get("completedAt") or issue.get("canceledAt")
    if closed_at is None:
        return "breached" if now_iso > sla_breaches_at else "pending"
    return "failed" if closed_at > sla_breaches_at else "met"


def _is_stale_feature(feature: Dict[str, Any], stale_cutoff_iso: str) -> bool:
    """A feature request/other ask counts against `featureScore` once it's
    been open (never completed or canceled) longer than
    `FEATURE_STALE_DAYS` - see module docstring."""
    if feature.get("completedAt") or feature.get("canceledAt"):
        return False
    created = feature.get("createdAt")
    return bool(created) and created < stale_cutoff_iso


def _product_metrics_for_customer(
    issues: List[Dict[str, Any]],
    now_iso: str,
    stale_cutoff_iso: str,
    month_start: str,
    month_end: str,
    workspace_base_url: Optional[str],
) -> Dict[str, Any]:
    bugs = [i for i in issues if _is_bug(i)]
    features = [i for i in issues if not _is_bug(i)]
    # "Total" counts (and their score denominators) are scoped to
    # currently-open work only (Backlog/Todo/In Progress/In Review/Triage) -
    # per-row exceptions from that below are deliberate, not oversights, see
    # the per-field comments (this mirrors an explicit product decision, not
    # a general "all counts are open-only" rule).
    open_bugs = [b for b in bugs if _is_open_state(b)]
    open_features = [f for f in features if _is_open_state(f)]

    # SLA bucketing itself still runs over *all* bugs regardless of state -
    # "failed" (missed SLA, already closed) is only reachable for closed
    # bugs by construction, and "failed this month" is intentionally kept
    # exactly as before (closed-ticket based), so it still needs the full
    # (not open-only) bucket set.
    buckets = [_sla_bucket(b, now_iso) for b in bugs]
    currently_out_of_sla_issues = [b for b, bucket in zip(bugs, buckets) if bucket == "breached"]
    failed_sla_this_month_issues = [
        bug
        for bug, bucket in zip(bugs, buckets)
        if bucket == "failed" and _in_range(bug.get("completedAt") or bug.get("canceledAt"), month_start, month_end)
    ]
    # "SLA-eligible bugs" is scoped to currently-open bugs only (a bug that
    # already closed cleanly within SLA - "met" - or missed it and closed
    # long ago isn't part of today's open workload); this also directly
    # narrows the Bug score's denominator, not just the displayed count.
    # (An open bug's bucket, if any, can only be "breached" or "pending" -
    # never "failed"/"met", which require a `closed_at` - so this is just
    # "does it carry an SLA at all".)
    sla_eligible_issues = [b for b in open_bugs if b.get("slaBreachesAt")]
    sla_eligible_bugs = len(sla_eligible_issues)
    bugs_currently_out_of_sla = len(currently_out_of_sla_issues)
    bugs_failed_sla_this_month = len(failed_sla_this_month_issues)
    sla_incidents = bugs_currently_out_of_sla + bugs_failed_sla_this_month
    # No SLA-eligible bugs at all -> nothing to be unresponsive to yet;
    # treat as a clean slate (100) rather than penalizing/rewarding based
    # on an empty sample - see module docstring.
    bug_responsiveness_rate = 1.0 if sla_eligible_bugs == 0 else max(0.0, 1 - sla_incidents / sla_eligible_bugs)

    # "New this month" intentionally stays scoped to *all* statuses (a bug
    # opened and already closed again within the same month still counts as
    # new work that came in) - only the "total" rows above are open-only.
    new_bugs_this_month_issues = [b for b in bugs if _in_range(b.get("createdAt"), month_start, month_end)]
    new_features_this_month_issues = [f for f in features if _in_range(f.get("createdAt"), month_start, month_end)]

    # Staleness is inherently open-only already (a completed/canceled
    # feature can never be "stale"), so this is naturally consistent with
    # `open_features` without needing to intersect explicitly.
    stale_feature_issues = [f for f in features if _is_stale_feature(f, stale_cutoff_iso)]
    stale_feature_requests = len(stale_feature_issues)
    # Same "empty sample -> clean slate" reasoning as bugs above. Denominator
    # is the open-only feature count, matching "total feature requests" below.
    feature_freshness_rate = (
        1.0 if not open_features else max(0.0, 1 - stale_feature_requests / len(open_features))
    )

    def link(bucket_issues: List[Dict[str, Any]]) -> Optional[str]:
        return _multi_issue_url(workspace_base_url, bucket_issues)

    return {
        "totalFeatureRequests": len(open_features),
        "totalFeatureRequestsUrl": link(open_features),
        "newFeatureRequestsThisMonth": len(new_features_this_month_issues),
        "newFeatureRequestsThisMonthUrl": link(new_features_this_month_issues),
        "staleFeatureRequests": stale_feature_requests,
        "staleFeatureRequestsUrl": link(stale_feature_issues),
        "featureScore": round(feature_freshness_rate * 100),
        "totalBugs": len(open_bugs),
        "totalBugsUrl": link(open_bugs),
        "newBugsThisMonth": len(new_bugs_this_month_issues),
        "newBugsThisMonthUrl": link(new_bugs_this_month_issues),
        "bugsCurrentlyOutOfSla": bugs_currently_out_of_sla,
        "bugsCurrentlyOutOfSlaUrl": link(currently_out_of_sla_issues),
        "bugsFailedSlaThisMonth": bugs_failed_sla_this_month,
        "bugsFailedSlaThisMonthUrl": link(failed_sla_this_month_issues),
        "slaEligibleBugs": sla_eligible_bugs,
        "slaEligibleBugsUrl": link(sla_eligible_issues),
        "bugScore": round(bug_responsiveness_rate * 100),
    }


def compute_product_scores(
    registry: List[Dict[str, Any]],
    linear_client: Optional[LinearClient] = None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """`partnerId -> product metrics dict` (with both `bugScore` and
    `featureScore`), or `None` for a partner with no linked Linear customer
    (shown as "not linked" rather than misleadingly perfect scores - see
    module docstring)."""
    linear_client = linear_client or LinearClient()
    issues = _fetch_customer_linked_issues(linear_client)
    by_customer = _group_issues_by_customer(issues)
    workspace_base_url = _linear_workspace_base_url(issues)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    stale_cutoff_iso = (now - timedelta(days=FEATURE_STALE_DAYS)).isoformat()
    month_start, month_end = _month_bounds()

    scores: Dict[str, Optional[Dict[str, Any]]] = {}
    for partner in registry:
        customer_id = partner.get("linearCustomerId")
        if not customer_id:
            scores[partner["partnerId"]] = None
            continue
        scores[partner["partnerId"]] = _product_metrics_for_customer(
            by_customer.get(customer_id, []), now_iso, stale_cutoff_iso, month_start, month_end, workspace_base_url
        )
    return scores


# ---------------------------------------------------------------------------
# Support score (Intercom conversations, scored by Claude)
# ---------------------------------------------------------------------------


def _anthropic_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set - see .env.example. Needed for Partner Insights' "
            "support-side conversation scoring."
        )
    return key


def _anthropic_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)


def _strip_html(value: Optional[str]) -> str:
    """Same as `support_report.py:_strip_html` - duplicated locally rather
    than imported since it's a generic 2-line helper, not partner-identity
    logic."""
    if not value:
        return ""
    return re.sub(r"<[^>]+>", " ", value).strip()


def _conversation_transcript(full_conversation: Dict[str, Any]) -> str:
    """A plain-text `[Support]`/`[Customer]` transcript for Claude to grade
    - built from the conversation's opening message plus every
    customer-facing part (internal notes excluded, since those were never
    seen by the customer and shouldn't count toward "how we responded")."""
    lines: List[str] = []

    source = full_conversation.get("source") or {}
    opening_body = _strip_html(source.get("body"))
    if opening_body:
        author_type = (source.get("author") or {}).get("type", "user")
        speaker = "Support" if author_type in ("admin", "bot") else "Customer"
        lines.append(f"[{speaker}] {opening_body}")

    parts = ((full_conversation.get("conversation_parts") or {}).get("conversation_parts")) or []
    for part in parts:
        if part.get("part_type") == "note":
            continue
        body = _strip_html(part.get("body"))
        if not body:
            continue
        author_type = (part.get("author") or {}).get("type", "user")
        speaker = "Support" if author_type in ("admin", "bot") else "Customer"
        lines.append(f"[{speaker}] {body}")

    return "\n".join(lines)


_SCORE_PROMPT_TEMPLATE = """You are grading how well OUR SUPPORT TEAM handled a customer support \
conversation. The transcript below is tagged [Support] for our team's messages and [Customer] for \
the customer's messages - grade ONLY the [Support] side.

Score on these three dimensions, each 0-100:
- "professionalism": tone, courtesy, clarity, and care in how our team wrote (not whether the \
underlying issue was fixable).
- "helpfulness": did our team's replies actually engage with and address the customer's specific \
problem, with a concrete answer or next step (as opposed to deflecting or ignoring the ask)?
- "cannedResponsePenalty": how much our team's replies read like generic, copy-pasted boilerplate \
that doesn't engage with this specific customer's specific issue. 0 = fully personalized and \
specific to this conversation, 100 = entirely generic/unhelpful stock phrasing.

Respond with ONLY a single JSON object and nothing else, no markdown fences, in this exact shape:
{{"professionalism": <integer 0-100>, "helpfulness": <integer 0-100>, "cannedResponsePenalty": <integer 0-100>, "rationale": "<one short sentence>"}}

Transcript:
{transcript}
"""


def _extract_json_object(text: str) -> str:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


def _clamp_score(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def _claude_score_conversation(transcript: str) -> Optional[Dict[str, Any]]:
    """One Claude call scoring `transcript` - `None` on any failure (a bad
    response, a timeout, malformed JSON) so one flaky conversation never
    takes down the whole daily batch (see `_run_daily_scoring_batch`)."""
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
                "max_tokens": 300,
                "messages": [
                    {"role": "user", "content": _SCORE_PROMPT_TEMPLATE.format(transcript=transcript[:12000])}
                ],
            },
            timeout=30,
        )
        response.raise_for_status()
        text = response.json()["content"][0]["text"]
        parsed = json.loads(_extract_json_object(text))
        return {
            "professionalism": _clamp_score(parsed.get("professionalism")),
            "helpfulness": _clamp_score(parsed.get("helpfulness")),
            "cannedResponsePenalty": _clamp_score(parsed.get("cannedResponsePenalty")),
            "rationale": str(parsed.get("rationale") or "")[:500],
        }
    except Exception as exc:  # noqa: BLE001 - one bad conversation shouldn't break the batch
        print(f"[partner_insights] Claude scoring failed for a conversation: {exc}")
        return None


def _epoch_to_iso(value: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def _run_daily_scoring_batch(
    intercom_client: IntercomClient,
    registry: List[Dict[str, Any]],
    since: float,
) -> List[Dict[str, Any]]:
    """Scores every closed conversation attributable to a registered
    partner since `since`, once each - see module docstring. Returns the
    new log entries to append (does not itself touch the log - see
    `_append_support_log`)."""
    closed = list(
        intercom_client.search_conversations(
            {"field": "statistics.first_close_at", "operator": ">", "value": int(since)}
        )
    )
    if not closed:
        return []

    company_map = build_company_map(intercom_client)
    partner_id_by_name = {p["name"]: p["partnerId"] for p in registry}

    def _score_one(conversation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        name = partner_name(conversation, company_map)
        partner_id = partner_id_by_name.get(name)
        if not partner_id:
            return None  # unresolvable/unregistered partner - mirrors support_report's "(unknown)"

        full = intercom_client.get_conversation(conversation["id"])
        transcript = _conversation_transcript(full)
        if "[Support]" not in transcript:
            return None  # nothing our team actually said yet - nothing to grade

        scores = _claude_score_conversation(transcript)
        if scores is None:
            return None

        return {
            "conversationId": conversation["id"],
            "url": f"https://app.intercom.com/a/inbox/{INTERCOM_INBOX_PREFIX}/inbox/shared/all/conversation/{conversation['id']}",
            "partnerId": partner_id,
            "scoredAt": datetime.now(timezone.utc).isoformat(),
            "closedAt": _epoch_to_iso((conversation.get("statistics") or {}).get("first_close_at")),
            **scores,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_score_one, closed))
    return [r for r in results if r is not None]


def _get_support_log() -> Dict[str, Any]:
    return cache.read_raw(PARTNER_INSIGHTS_SUPPORT_LOG_KEY) or {"lastRunAt": None, "entries": []}


def _append_support_log(new_entries: List[Dict[str, Any]], run_at: float) -> None:
    """Best-effort append - a storage hiccup here should never fail the
    report itself (mirrors `support_report.py:_record_history`)."""
    try:
        existing = _get_support_log()
        entries = (existing.get("entries") or []) + new_entries
        entries = entries[-PARTNER_INSIGHTS_SUPPORT_LOG_MAX_ENTRIES:]
        cache.write_raw(PARTNER_INSIGHTS_SUPPORT_LOG_KEY, {"lastRunAt": run_at, "entries": entries})
    except Exception as exc:  # noqa: BLE001
        print(f"[partner_insights] failed to record support scoring batch: {exc}")


def _should_run_batch(force: bool) -> bool:
    if not _anthropic_configured():
        return False  # graceful degradation - see module docstring / _anthropic_configured
    if force:
        return True
    last_run_at = _get_support_log().get("lastRunAt")
    return not last_run_at or (time.time() - last_run_at) > _BATCH_MIN_INTERVAL_SECONDS


def compute_support_scores(
    log: Dict[str, Any],
    window_days: int = SUPPORT_SCORE_WINDOW_DAYS,
) -> Dict[str, Dict[str, Any]]:
    """`partnerId -> support metrics dict` (aggregated over the trailing
    `window_days`), for every partner with at least one scored conversation
    in that window - partners with none are simply absent from the result
    (the caller/frontend shows "no data yet" for those)."""
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    by_partner: Dict[str, List[Dict[str, Any]]] = {}
    for entry in log.get("entries") or []:
        if entry.get("scoredAt", "") < cutoff_iso:
            continue
        by_partner.setdefault(entry["partnerId"], []).append(entry)

    scores: Dict[str, Dict[str, Any]] = {}
    for partner_id, entries in by_partner.items():
        entries.sort(key=lambda e: e.get("scoredAt", ""), reverse=True)
        professionalism = sum(e["professionalism"] for e in entries) / len(entries)
        helpfulness = sum(e["helpfulness"] for e in entries) / len(entries)
        canned_penalty = sum(e["cannedResponsePenalty"] for e in entries) / len(entries)
        composite = (professionalism + helpfulness + (100 - canned_penalty)) / 3
        scores[partner_id] = {
            "conversationsScored": len(entries),
            "professionalism": round(professionalism),
            "helpfulness": round(helpfulness),
            "cannedResponsePenalty": round(canned_penalty),
            "supportScore": round(composite),
            "windowDays": window_days,
            "conversations": entries,
        }
    return scores


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_partner_insights_report(force: bool = False) -> Dict[str, Any]:
    intercom_client = IntercomClient()
    linear_client = LinearClient()
    # `None` (not an empty client) when unconfigured - `build_partner_registry`
    # treats that as "skip Vitally matching entirely" rather than erroring,
    # same graceful-degradation shape as Claude scoring below.
    vitally_client = VitallyClient() if vitally_configured() else None

    registry = build_partner_registry(intercom_client, linear_client, vitally_client)
    product_scores = compute_product_scores(registry, linear_client=linear_client)

    if _should_run_batch(force):
        since = time.time() - 24 * 60 * 60
        new_entries = _run_daily_scoring_batch(intercom_client, registry, since)
        _append_support_log(new_entries, run_at=time.time())

    support_scores = compute_support_scores(_get_support_log())

    partners = [
        {
            **partner,
            "product": product_scores.get(partner["partnerId"]),
            "support": support_scores.get(partner["partnerId"]),
        }
        for partner in registry
    ]

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "claudeConfigured": _anthropic_configured(),
        "vitallyConfigured": vitally_configured(),
        "supportScoreWindowDays": SUPPORT_SCORE_WINDOW_DAYS,
        "partners": partners,
    }
