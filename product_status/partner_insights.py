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

## (Removed) Support score
An earlier version of this tab also scored Intercom conversations with an
LLM (professionalism/helpfulness/"canned"-ness) into a Support score
column, via a small daily batch (`_run_daily_scoring_batch`) appending to
an accumulating log. That column was dropped in favor of the
Escalations signal below (a stronger, more actionable read on partner
health), and the daily batch was removed entirely rather than left running
unused - see git history (`_run_daily_scoring_batch`/`compute_support_scores`/
`_score_conversation`) if it's ever needed again.

## Vitally health score (Vitally only, no computation - recomputed on
## every refresh)
Vitally (a customer-success platform, separate from both Linear and
Intercom) already computes its own per-account `healthScore` - in this
workspace it's a direct 0/5/10 encoding of a manually-set Red/Yellow/Green
"pulse" trait imported from a CSV upload (confirmed against live data:
Red -> 0, Yellow -> 5, Green -> 10 with no exceptions). `partner_identity.py`
still attaches it to every registry entry (`vitallyHealthScore`), but this
tab doesn't currently render it as its own column - it's not being
recomputed here, just left in the data in case it comes back.

## Partners this tab shows
Registry comes from `partner_identity.build_partner_registry`, but this
tab then filters that down to *only* partners with a matched Vitally
account (`vitallyAccountId`) - see `build_partner_insights_report`. Every
column left in the table (Bug/Feature score, Live Fire/Smoldering) is
either Linear- or Vitally-sourced, so a partner Vitally doesn't know about
would only ever show empty cells; filtering them out keeps the table to
partners this tab can actually say something about, rather than a long
tail of "not linked"/"not in Vitally" rows.

## Escalations (Vitally emails + LLM triage, incremental, forced-refresh only)
A second, independent signal alongside Bug/Feature score - see
`escalation_report.py`'s module docstring for the full design. In short:
partner-authored, human-written emails synced into Vitally from
Gmail/Outlook (not Intercom) are triaged by OpenAI against a fixed risk
framework into LIVE_FIRE/SMOLDERING/WATCH items, cached and updated
incrementally (only new email since the last run is ever re-analyzed).
Unlike Bug/Feature score, this never runs on a passive/cache-age refresh -
only an explicit "Update" button click triggers new LLM calls, since it's
the more expensive of the two to compute.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .escalation_report import escalations_configured, refresh_partner_escalations, vitally_app_account_url
from .intercom_client import IntercomClient
from .linear_client import LinearClient
from .partner_identity import build_partner_registry
from .quality import BUG_LABEL, _month_bounds
from .vitally_client import VitallyClient
from .vitally_client import is_configured as vitally_configured

PARTNER_INSIGHTS_CACHE_KEY = "dashboard-partner-insights"
# Bump whenever this module's output shape or underlying metric logic
# changes - see `milestones_report.py:MILESTONES_REPORT_CACHE_VERSION` for
# why (the cache backend has no schema of its own).
PARTNER_INSIGHTS_CACHE_VERSION = 9

# A still-open feature request older than this with no resolution counts
# against `featureScore` - see module docstring.
FEATURE_STALE_DAYS = 90


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
# Orchestration
# ---------------------------------------------------------------------------


def build_partner_insights_report(force: bool = False) -> Dict[str, Any]:
    intercom_client = IntercomClient()
    linear_client = LinearClient()
    # `None` (not an empty client) when unconfigured - `build_partner_registry`
    # treats that as "skip Vitally matching entirely" rather than erroring.
    vitally_client = VitallyClient() if vitally_configured() else None

    registry = build_partner_registry(intercom_client, linear_client, vitally_client)
    # This tab only shows partners Vitally knows about - see module
    # docstring's "Partners this tab shows". Filtered here (not inside
    # `build_partner_registry` itself, which stays a general-purpose
    # registry) so every downstream computation below only runs for
    # partners that'll actually appear in the report.
    registry = [p for p in registry if p.get("vitallyAccountId")]
    product_scores = compute_product_scores(registry, linear_client=linear_client)

    # Escalations: the one signal here that never runs on a passive
    # cache-age refresh, only an explicit `force` - see
    # `escalation_report.py`'s module docstring and this module's own
    # docstring's "Escalations" section.
    escalation_state = (
        refresh_partner_escalations(registry, vitally_client, force=force) if vitally_client else {}
    )

    def _escalations_for(partner: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        account_id = partner.get("vitallyAccountId")
        if not account_id:
            return None
        entry = escalation_state.get(partner["partnerId"]) or {"items": [], "checkedAt": None}
        return {
            "items": entry.get("items") or [],
            "checkedAt": entry.get("checkedAt"),
            "vitallyAccountUrl": vitally_app_account_url(account_id),
            # Raw source emails from the latest triage batch - see
            # `escalation_report.py`'s module docstring's `recentEmails`
            # section. Empty until the first forced refresh after a
            # partner has new eligible email.
            "recentEmails": entry.get("recentEmails") or [],
        }

    partners = [
        {
            **partner,
            "product": product_scores.get(partner["partnerId"]),
            "escalations": _escalations_for(partner),
        }
        for partner in registry
    ]

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "vitallyConfigured": vitally_configured(),
        "escalationsConfigured": escalations_configured(),
        "partners": partners,
    }
