"""Minimal Slack Web API client - just enough to DM one person a plain
text message (`chat.postMessage`). Used by `escalation_report.py` to
alert on newly-flagged Live Fire/Smoldering escalations - see that
module's "Slack alerting" docstring section for when/why it's called.

Auth: a Bot Token (`SLACK_BOT_TOKEN`, starts with "xoxb-") from a Slack
App with the `chat:write` scope, installed to your workspace - create one
at https://api.slack.com/apps -> "Create New App" -> add `chat:write`
under "OAuth & Permissions" -> "Install to Workspace" -> copy the "Bot
User OAuth Token" it gives you and set it here.

No channel/invite setup needed: `chat.postMessage` accepts a Slack user
ID directly as `channel` and Slack opens/reuses the DM with that user
automatically (confirmed against Slack's own API docs - no separate
`conversations.open` call required). Set `SLACK_ALERT_USER_ID` to the
target user's Slack member ID (their Slack profile -> "..." more menu ->
"Copy member ID", looks like "U0123ABCD4").
"""

import os
from typing import Optional

import requests

SLACK_API_BASE = "https://slack.com/api"


def is_configured() -> bool:
    return bool(os.environ.get("SLACK_BOT_TOKEN")) and bool(os.environ.get("SLACK_ALERT_USER_ID"))


def send_dm(text: str, user_id: Optional[str] = None) -> None:
    """Send `text` (Slack mrkdwn) as a DM - raises `RuntimeError` on any
    failure (missing config, HTTP error, Slack API-level error) rather
    than swallowing it, so the caller decides whether/how to log it (see
    `escalation_report.py`'s use of this - wrapped in a try/except so one
    Slack hiccup never breaks the escalation refresh itself)."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    target = user_id or os.environ.get("SLACK_ALERT_USER_ID")
    if not token or not target:
        raise RuntimeError("SLACK_BOT_TOKEN/SLACK_ALERT_USER_ID not set - see .env.example")
    response = requests.post(
        f"{SLACK_API_BASE}/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": target, "text": text},
        timeout=15,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Slack API error: {payload.get('error') or response.text[:300]}")
