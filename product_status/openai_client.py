"""Thin wrapper around OpenAI's Chat Completions API - the one LLM used by
both `partner_insights.py` (Support score conversation grading) and
`escalation_report.py` (email escalation triage). Previously both were
Claude/Anthropic; this app now uses OpenAI exclusively for both call
sites (see git history for the Claude versions if that's ever needed
again).

Base URL defaults to OpenAI's regional "US data residency" endpoint
(`https://us.api.openai.com/v1`) rather than the global `api.openai.com` -
same Bearer-token auth and Chat Completions request/response shape as the
global endpoint, just a different host, so requests are processed/stored
in the US per OpenAI's per-request regional routing (see
https://developers.openai.com/api/docs/guides/your-data). If your OpenAI
account isn't eligible for the "us" prefix, requests 401 with
`incorrect_hostname` - set `OPENAI_BASE_URL` to the default
`https://api.openai.com/v1` in that case.

Default model is `gpt-5-mini` - OpenAI's "faster, cost-efficient... for
well-defined tasks and precise prompts" tier, matching what both call
sites actually are (structured extraction/classification, not open-ended
generation). GPT-5-family models reject the legacy `max_tokens` param
(400 Bad Request) - `max_completion_tokens` is required instead, and that
budget is shared with the model's own internal reasoning tokens, not just
the visible output - see `chat_completion`'s `reasoning_effort="low"`
default, which keeps that reasoning overhead small for tasks this
well-defined.
"""

import os
from typing import Optional

import requests

DEFAULT_OPENAI_BASE_URL = "https://us.api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set - see .env.example. Needed for Partner Insights' "
            "Support scoring and Escalation triage."
        )
    return key


def _base_url() -> str:
    return os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)


def chat_completion(
    prompt: str,
    max_completion_tokens: int = 4000,
    reasoning_effort: Optional[str] = "low",
    timeout: int = 60,
) -> str:
    """One user-turn Chat Completions call, returning the assistant's raw
    text content. Raises on any failure (bad response, timeout, HTTP
    error) - callers are expected to catch broadly and degrade gracefully
    (see `partner_insights.py:_score_conversation` /
    `escalation_report.py:_update_escalations`, both of which already do
    this)."""
    payload = {
        "model": _model(),
        "max_completion_tokens": max_completion_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    response = requests.post(
        f"{_base_url()}/chat/completions",
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if not content:
        # A GPT-5-family response that spent its whole token budget on
        # internal reasoning with nothing left for visible output - see
        # module docstring. Surfacing this distinctly (rather than
        # returning "") makes it easy to tell apart from "the model
        # legitimately said nothing" in logs.
        finish_reason = choice.get("finish_reason")
        raise RuntimeError(f"OpenAI returned no content (finish_reason={finish_reason}) - {body}"[:500])
    return content
