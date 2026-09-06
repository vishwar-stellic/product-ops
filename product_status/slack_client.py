"""Minimal Slack Web API client - just enough to post one plain text
message (`chat.postMessage`) to a single fixed destination. Used by
`escalation_report.py` to alert on newly-flagged Live Fire/Smoldering
escalations - see that module's "Slack alerting" docstring section for
when/why it's called.

Auth: a Bot Token (`SLACK_BOT_TOKEN`, starts with "xoxb-") from a Slack
App with the `chat:write` scope, installed to your workspace - create one
at https://api.slack.com/apps -> "Create New App" -> add `chat:write`
under "OAuth & Permissions" -> "Install to Workspace" -> copy the "Bot
User OAuth Token" it gives you and set it here.

Destination (`SLACK_ALERT_TARGET`): Slack's `chat.postMessage` treats a
channel ID and a user ID identically via the same `channel` parameter, so
this one client/env var covers both cases with no code branching:
  - DM a person: set it to their Slack member ID (their profile -> "..."
    more menu -> "Copy member ID", looks like "U0123ABCD4"). Slack opens/
    reuses the DM with that user automatically - no invite or separate
    `conversations.open` call needed.
  - Post to a channel: set it to that channel's ID (open the channel in
    Slack -> channel name -> "..." at the bottom -> "Copy link", the ID is
    the last path segment, looks like "C0123ABCD4"). Unlike a DM, the bot
    user must actually be a member of the channel first - invite it with
    `/invite @YourBotName` in that channel (or check `chat:write.public`
    under the app's scopes if you'd rather not invite it to every public
    channel individually).
"""

import os

import requests

SLACK_API_BASE = "https://slack.com/api"


def is_configured() -> bool:
    return bool(os.environ.get("SLACK_BOT_TOKEN")) and bool(os.environ.get("SLACK_ALERT_TARGET"))


def send_message(text: str) -> None:
    """Post `text` (Slack mrkdwn) to `SLACK_ALERT_TARGET` - raises
    `RuntimeError` on any failure (missing config, HTTP error, Slack
    API-level error) rather than swallowing it, so the caller decides
    whether/how to log it (see `escalation_report.py`'s use of this -
    wrapped in a try/except so one Slack hiccup never breaks the
    escalation refresh itself)."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    target = os.environ.get("SLACK_ALERT_TARGET")
    if not token or not target:
        raise RuntimeError("SLACK_BOT_TOKEN/SLACK_ALERT_TARGET not set - see .env.example")
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
