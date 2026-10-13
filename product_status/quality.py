"""SLA / quality metrics per team: currently-breached bugs, bugs that failed
their SLA this month, currently-open High-priority bugs, and incoming
high/urgent bugs this month.

A "bug" is any issue carrying the workspace-level "Bug" label. Only Urgent
and High priority bugs carry an SLA in this workspace. Rather than
re-deriving breach state from `slaBreachesAt` client-side, this uses
Linear's own computed `slaStatus` filter, which already accounts for
priority/SLA eligibility:

  - Breached  -> still open, past its SLA deadline
                 ("Currently Out of SLA")
  - Failed    -> was closed (completed or canceled) after its SLA deadline
                 ("Failed SLA" - counted here only if closed this month)
  - Completed -> was closed before its SLA deadline (met SLA; not counted)

For Progress, each count is additionally split by the "Compute?" label group
(see `COMPUTE_BREAKDOWN_TEAMS`).

"This month" is a UTC calendar month, consistent with how the rest of this
package treats timestamps.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .linear_client import LinearClient

BUG_LABEL = "Bug"

# Per-team labels excluded from "Currently Out of SLA" only. Progress asked
# for "gurobi-solves"-labeled bugs to not count toward that row - they're
# tracked/worked separately and skew the SLA number.
CURRENTLY_OUT_OF_SLA_EXCLUDED_LABELS: Dict[str, List[str]] = {
    "PROG": ["gurobi-solves"],
}

# "SLA Quality Total" and "Incoming Bugs with High or Urgent priority this
# month" are each scored against a per-team limit: higher-volume squads get
# more headroom. Teams not listed use `DEFAULT_QUALITY_THRESHOLD`.
HIGH_VOLUME_QUALITY_TEAMS = {"PROG", "PLAN", "INT"}
HIGH_VOLUME_QUALITY_THRESHOLD = 10
DEFAULT_QUALITY_THRESHOLD = 5


# Squads whose quality rows are also split by the "Compute?" label group.
# Progress tags bugs with the child labels "Compute" / "Non-compute" (nested
# under the "Compute?" group label); a bug with neither is "unlabeled".
COMPUTE_BREAKDOWN_TEAMS = {"PROG"}
COMPUTE_LABEL = "Compute"
NON_COMPUTE_LABEL = "Non-compute"

# bucket key -> issue-label collection filter selecting exactly that bucket.
_COMPUTE_BUCKET_FILTERS: Dict[str, Dict[str, Any]] = {
    "compute": {"some": {"name": {"eq": COMPUTE_LABEL}}},
    "nonCompute": {"some": {"name": {"eq": NON_COMPUTE_LABEL}}},
    # "every label is neither of the two" = carries neither label.
    "unlabeled": {"every": {"name": {"nin": [COMPUTE_LABEL, NON_COMPUTE_LABEL]}}},
}


def _quality_threshold_for_team(team_key: str) -> int:
    return HIGH_VOLUME_QUALITY_THRESHOLD if team_key in HIGH_VOLUME_QUALITY_TEAMS else DEFAULT_QUALITY_THRESHOLD

_COUNT_ISSUES_QUERY = """
query CountIssues($first: Int!, $after: String, $filter: IssueFilter!) {
  issues(first: $first, after: $after, filter: $filter) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _count_issues(client: LinearClient, issue_filter: Dict[str, Any]) -> int:
    nodes = client.paginate(
        _COUNT_ISSUES_QUERY,
        variables={"filter": issue_filter},
        path=["issues"],
        page_size=100,
    )
    return len(nodes)


def _count_by_compute_bucket(client: LinearClient, issue_filter: Dict[str, Any]) -> Dict[str, int]:
    """`issue_filter`'s count split into Compute / Non-compute / unlabeled.

    Each bucket is its own Linear query (rather than deriving "unlabeled" by
    subtraction) so the three always match what the labels say, even if a bug
    ever carries both. Combined with `and` because `issue_filter` may already
    use the `labels` key."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            bucket: pool.submit(_count_issues, client, {"and": [issue_filter, {"labels": label_filter}]})
            for bucket, label_filter in _COMPUTE_BUCKET_FILTERS.items()
        }
        return {bucket: future.result() for bucket, future in futures.items()}


def _sum_buckets(*breakdowns: Dict[str, int]) -> Dict[str, int]:
    return {bucket: sum(b[bucket] for b in breakdowns) for bucket in _COMPUTE_BUCKET_FILTERS}


def _month_bounds(now: Optional[datetime] = None) -> Tuple[str, str]:
    """(start, end) ISO instants for the current UTC calendar month."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0, day=1)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()


def build_quality_summary(
    client: Optional[LinearClient] = None,
    team: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """SLA/bug counts for one team, for the "Quality" section of the dashboard."""
    if team is None:
        raise ValueError("team is required")
    client = client or LinearClient()

    month_start, month_end = _month_bounds()
    team_filter = {"id": {"eq": team["id"]}}
    bug_label_filter = {"some": {"name": {"eq": BUG_LABEL}}}

    excluded_labels = CURRENTLY_OUT_OF_SLA_EXCLUDED_LABELS.get(team["key"], [])
    out_of_sla_labels_filter = dict(bug_label_filter)
    if excluded_labels:
        # "every" here means "every label on the issue is not one of the
        # excluded names" - i.e. the issue doesn't carry any of them -
        # combined (ANDed) with "some" requiring the Bug label above.
        out_of_sla_labels_filter["every"] = {"name": {"nin": excluded_labels}}

    out_of_sla_filter = {
        "team": team_filter,
        "labels": out_of_sla_labels_filter,
        "slaStatus": {"eq": "Breached"},
    }
    failed_sla_filter = {
        "team": team_filter,
        "labels": bug_label_filter,
        "slaStatus": {"eq": "Failed"},
        "or": [
            {"completedAt": {"gte": month_start, "lt": month_end}},
            {"canceledAt": {"gte": month_start, "lt": month_end}},
        ],
    }
    incoming_filter = {
        "team": team_filter,
        "labels": bug_label_filter,
        "priority": {"in": [1, 2]},  # 1 = Urgent, 2 = High
        "createdAt": {"gte": month_start, "lt": month_end},
    }
    active_high_filter = {
        "team": team_filter,
        "labels": bug_label_filter,
        "priority": {"eq": 2},  # 2 = High
        "completedAt": {"null": True},
        "canceledAt": {"null": True},
    }

    currently_out_of_sla = _count_issues(client, out_of_sla_filter)
    failed_sla_this_month = _count_issues(client, failed_sla_filter)
    incoming_high_urgent_this_month = _count_issues(client, incoming_filter)
    currently_active_high_bugs = _count_issues(client, active_high_filter)

    compute_breakdown: Optional[Dict[str, Dict[str, int]]] = None
    if team["key"] in COMPUTE_BREAKDOWN_TEAMS:
        out_of_sla_split = _count_by_compute_bucket(client, out_of_sla_filter)
        failed_split = _count_by_compute_bucket(client, failed_sla_filter)
        compute_breakdown = {
            "slaQualityTotal": _sum_buckets(out_of_sla_split, failed_split),
            "currentlyOutOfSla": out_of_sla_split,
            "failedSlaThisMonth": failed_split,
            "currentlyActiveHighBugs": _count_by_compute_bucket(client, active_high_filter),
            "incomingHighUrgentThisMonth": _count_by_compute_bucket(client, incoming_filter),
        }

    sla_quality_total = currently_out_of_sla + failed_sla_this_month
    threshold = _quality_threshold_for_team(team["key"])

    return {
        "monthStart": month_start,
        "currentlyOutOfSla": currently_out_of_sla,
        "failedSlaThisMonth": failed_sla_this_month,
        "slaQualityTotal": sla_quality_total,
        "currentlyActiveHighBugs": currently_active_high_bugs,
        "incomingHighUrgentThisMonth": incoming_high_urgent_this_month,
        "threshold": threshold,
        "slaQualityWithinThreshold": sla_quality_total <= threshold,
        "incomingHighUrgentWithinThreshold": incoming_high_urgent_this_month <= threshold,
        # Only for squads in COMPUTE_BREAKDOWN_TEAMS (Progress): each row's
        # count split into compute / nonCompute / unlabeled.
        "computeBreakdown": compute_breakdown,
    }
