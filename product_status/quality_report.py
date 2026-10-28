"""History of the EPD Report's Quality table, per squad - the data behind the
dashboard's "Quality Report" tab. Every point carries all of the Quality
table's rows:

    - Out of SLA (A)                open at the snapshot, SLA breach time passed
    - Failed SLA this month (B)     closed this (Pacific) month, after the SLA breach time
    - SLA Quality (A+B)             the two above, together
    - Active High Bugs              open at the snapshot, High priority
    - Incoming High+ this month     created this (Pacific) month, Urgent or High

Two kinds of points:

  weekly  - the last 12 Wednesdays, each taken at the END of that Wednesday
            (Pacific; today's is as of now). The "this month" rows are
            month-to-date (Pacific month) as of that day.
  monthly - the last 6 months, each valued at month end (Pacific months; the current month is month-to-date, as of
            now) - i.e. the value for that whole month.

("Number of bugs because of missing tests" has no data source - it's filled in
by hand in the EPD Report - so it isn't a column here.)

How a past point is reconstructed: the live Quality table leans on Linear's
current `slaStatus` filter, which can't be asked "as of last Wednesday".
Instead this pulls every Bug-labeled issue that could matter in the window
ONCE per squad, with its created/completed/canceled timestamps and
`slaBreachesAt`, and replays each metric's definition at each snapshot time
client-side. Caveat: an issue's *current* priority and SLA breach time are used
for every snapshot (Linear's API doesn't expose the history cheaply), so an
issue whose priority was changed later is counted as if it always had that
priority.

Nothing here is persisted - `server.py` keeps the result in process memory
only (see `QUALITY_REPORT_MEMORY_TTL_SECONDS`).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .linear_client import LinearClient
from .quality import BUG_LABEL, CURRENTLY_OUT_OF_SLA_EXCLUDED_LABELS

QUALITY_REPORT_WEEKS = 12
QUALITY_REPORT_MONTHS = 6

_PACIFIC = ZoneInfo("America/Los_Angeles")

# Series, in the same order as the EPD Report's Quality table.
QUALITY_REPORT_METRICS: List[Dict[str, str]] = [
    {"key": "currentlyOutOfSla", "label": "Out of SLA (A)"},
    {"key": "failedSlaThisMonth", "label": "Failed SLA this month (B)"},
    {"key": "slaQualityTotal", "label": "SLA Quality (A+B)"},
    {"key": "currentlyActiveHighBugs", "label": "Active High Bugs"},
    {"key": "incomingHighUrgentThisMonth", "label": "Incoming High+ this month"},
]

_ISSUES_QUERY = """
query QualityReportIssues($first: Int!, $after: String, $filter: IssueFilter!) {
  issues(first: $first, after: $after, filter: $filter) {
    nodes {
      id
      priority
      createdAt
      completedAt
      canceledAt
      slaBreachesAt
      labels(first: 20) { nodes { name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _month_start(moment: datetime) -> datetime:
    """Start of `moment`'s calendar month in Pacific time, as a UTC datetime."""
    pacific = moment.astimezone(_PACIFIC)
    return pacific.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def weekly_snapshots(now: datetime, weeks: int = QUALITY_REPORT_WEEKS) -> List[Dict[str, Any]]:
    """`weeks` Wednesday snapshots, oldest first. Each is taken at the END of
    that Wednesday (Pacific); the most recent Wednesday, if it's today, is
    taken as of `now` instead."""
    now_pt = now.astimezone(_PACIFIC)
    last_wed = (now_pt - timedelta(days=(now_pt.weekday() - 2) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    snapshots: List[Dict[str, Any]] = []
    for back in range(weeks - 1, -1, -1):
        day = last_wed - timedelta(weeks=back)
        day_end = (day + timedelta(days=1)).astimezone(timezone.utc)
        snapshots.append(
            {
                "label": day.strftime("%b %-d"),
                "asOf": min(day_end - timedelta(seconds=1), now).isoformat(),
                "partial": day_end > now,
            }
        )
    return snapshots


def monthly_snapshots(now: datetime, months: int = QUALITY_REPORT_MONTHS) -> List[Dict[str, Any]]:
    """`months` Pacific month-end snapshots, oldest first; the current month is
    taken month-to-date, as of `now`."""
    current = now.astimezone(_PACIFIC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    snapshots: List[Dict[str, Any]] = []
    for back in range(months - 1, -1, -1):
        year, month = current.year, current.month - back
        while month < 1:
            month += 12
            year -= 1
        start = datetime(year, month, 1, tzinfo=_PACIFIC)
        next_start = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=_PACIFIC).astimezone(timezone.utc)
        snapshots.append(
            {
                "label": start.strftime("%b %Y"),
                "dayLabel": (next_start - timedelta(seconds=1)).astimezone(_PACIFIC).strftime("%b %-d"),
                "asOf": min(next_start - timedelta(seconds=1), now).isoformat(),
                "partial": next_start > now,
            }
        )
    return snapshots


def _fetch_bug_issues(client: LinearClient, team: Dict[str, Any], window_start: datetime) -> List[Dict[str, Any]]:
    """Bug-labeled issues of one team that were open at any point since
    `window_start`, or closed since then (everything the snapshots can see)."""
    since = window_start.isoformat()
    return client.paginate(
        _ISSUES_QUERY,
        variables={
            "filter": {
                "team": {"id": {"eq": team["id"]}},
                "labels": {"some": {"name": {"eq": BUG_LABEL}}},
                "or": [
                    {"completedAt": {"null": True}, "canceledAt": {"null": True}},
                    {"completedAt": {"gte": since}},
                    {"canceledAt": {"gte": since}},
                ],
            }
        },
        path=["issues"],
        page_size=100,
    )


def _prepare(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "priority": raw.get("priority"),
        "created": _parse(raw.get("createdAt")),
        "completed": _parse(raw.get("completedAt")),
        "canceled": _parse(raw.get("canceledAt")),
        "sla": _parse(raw.get("slaBreachesAt")),
        "labels": {n["name"] for n in (raw.get("labels") or {}).get("nodes", [])},
    }


def _open_at(issue: Dict[str, Any], moment: datetime) -> bool:
    created = issue["created"]
    if created is None or created > moment:
        return False
    for closed in (issue["completed"], issue["canceled"]):
        if closed is not None and closed <= moment:
            return False
    return True


def metrics_as_of(issues: List[Dict[str, Any]], moment: datetime, excluded_labels: List[str]) -> Dict[str, int]:
    """Every Quality metric for one squad's prepared issues at `moment`."""
    month_start = _month_start(moment)
    out_of_sla = 0
    failed = 0
    active_high = 0
    incoming = 0
    for issue in issues:
        sla = issue["sla"]
        if _open_at(issue, moment):
            if sla is not None and sla < moment and not (issue["labels"] & set(excluded_labels)):
                out_of_sla += 1
            if issue["priority"] == 2:
                active_high += 1
        # Closed this month (up to `moment`) after the SLA deadline had passed.
        if sla is not None and any(
            closed is not None and month_start <= closed <= moment and closed > sla
            for closed in (issue["completed"], issue["canceled"])
        ):
            failed += 1
        created = issue["created"]
        if created is not None and month_start <= created <= moment and issue["priority"] in (1, 2):
            incoming += 1
    return {
        "slaQualityTotal": out_of_sla + failed,
        "currentlyOutOfSla": out_of_sla,
        "failedSlaThisMonth": failed,
        "currentlyActiveHighBugs": active_high,
        "incomingHighUrgentThisMonth": incoming,
    }


def _points(issues, snapshots, excluded):
    return [
        {**snap, "values": metrics_as_of(issues, datetime.fromisoformat(snap["asOf"]), excluded)}
        for snap in snapshots
    ]


def build_quality_report(
    client: Optional[LinearClient] = None,
    teams: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    weeks: int = QUALITY_REPORT_WEEKS,
    months: int = QUALITY_REPORT_MONTHS,
) -> Dict[str, Any]:
    """Wednesday + monthly Quality history for every squad in `teams` (plus a
    Total that adds the squads together). See the module docstring."""
    if not teams:
        raise ValueError("teams is required")
    client = client or LinearClient()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    weekly = weekly_snapshots(now, weeks)
    monthly = monthly_snapshots(now, months)
    # Month-to-date metrics at the earliest snapshot need closes since that month began.
    window_start = min(
        _month_start(datetime.fromisoformat(weekly[0]["asOf"])),
        _month_start(datetime.fromisoformat(monthly[0]["asOf"])),
    )

    def build_squad(team: Dict[str, Any]) -> Dict[str, Any]:
        issues = [_prepare(raw) for raw in _fetch_bug_issues(client, team, window_start)]
        excluded = CURRENTLY_OUT_OF_SLA_EXCLUDED_LABELS.get(team["key"], [])
        return {
            "key": team["key"],
            "name": team["name"],
            "weekly": _points(issues, weekly, excluded),
            "monthly": _points(issues, monthly, excluded),
            "issueCount": len(issues),
        }

    with ThreadPoolExecutor(max_workers=3) as pool:
        squads = list(pool.map(build_squad, teams))

    def summed(field: str, snapshots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {**snap, "values": {m["key"]: sum(sq[field][i]["values"][m["key"]] for sq in squads) for m in QUALITY_REPORT_METRICS}}
            for i, snap in enumerate(snapshots)
        ]

    return {
        "generatedAt": now.isoformat(),
        "weeks": weeks,
        "months": months,
        "metrics": QUALITY_REPORT_METRICS,
        "squads": squads,
        "total": {
            "key": "TOTAL",
            "name": "Total",
            "weekly": summed("weekly", weekly),
            "monthly": summed("monthly", monthly),
        },
    }
