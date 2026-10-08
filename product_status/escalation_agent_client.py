"""Read-only client for the escalation agent (`escalation-agent/`, deployed as
its own Vercel project).

The agent is the only thing that triages partner email now: it reads Vitally on
a schedule, keeps each partner's tracked escalation items, and posts the Slack
alerts. This dashboard just *reads* what it saved, so the Partner Insights tab
shows the same items the Slack alerts were raised from.

Setup: set `ESCALATION_AGENT_URL` (the agent's production URL, no trailing
slash) here. Auth reuses `CRON_SECRET`, which both projects already share (the
agent uses it to read the partner registry from this app). Leave
`ESCALATION_AGENT_URL` unset and the escalation columns show "not configured".
"""

import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests

# One whole-roster read is ~100 blob reads on the agent; Partner Insights page
# loads in the same minute (and the per-partner Update button) share one result.
CACHE_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 60

_lock = threading.Lock()
_cached: Dict[tuple, Any] = {}  # partner-id set -> (fetched at, states); at most a few entries


def is_configured() -> bool:
    return bool(os.environ.get("ESCALATION_AGENT_URL") and os.environ.get("CRON_SECRET"))


def fetch_partner_states(partner_ids: List[str], force: bool = False) -> Dict[str, Optional[Dict[str, Any]]]:
    """partnerId -> {items, checkedAt, lastMessageAt, recentEmails}, or None for a
    partner the agent has never processed. Raises RuntimeError when the agent
    can't be reached - callers decide how to degrade."""
    if not is_configured():
        return {}
    key = tuple(sorted(set(partner_ids)))
    with _lock:
        hit = _cached.get(key)
        if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]

    base = os.environ["ESCALATION_AGENT_URL"].rstrip("/")
    try:
        response = requests.post(
            f"{base}/escalation/state",
            json={"partnerIds": list(key)},
            headers={"Authorization": f"Bearer {os.environ['CRON_SECRET']}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Escalation agent unreachable: {exc}") from exc
    if not response.ok:
        raise RuntimeError(f"Escalation agent error {response.status_code}: {response.text[:200]}")
    states = (response.json() or {}).get("partners") or {}
    with _lock:
        if len(_cached) >= 8:
            _cached.clear()
        _cached[key] = (time.time(), states)
    return states
