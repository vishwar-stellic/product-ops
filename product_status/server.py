"""HTTP service exposing sprint status on demand.

Run with:
    uvicorn product_status.server:app --reload --port 8008

Endpoints:
    GET  /health
    GET  /sprints                 -> current + previous sprint, all teams
    GET  /sprints/current         -> current sprint only, all teams
    GET  /sprints/previous        -> previous sprint only, all teams
    GET  /sprints?team=PROG,PLAN  -> restrict to specific teams (key or name)
    GET  /projects/summit         -> project summaries (with milestones) for
                                      projects tagged "For Summit"
    GET  /projects/summit?label=X -> same, for a different project label
    GET  /api/dashboard              -> sprints + summit projects for every
                                         squad; each squad is served from its
                                         own on-disk cache, refreshed at most
                                         once per 24h
    POST /api/dashboard/refresh/{key} -> force a fresh pull for one squad
                                          (team key, e.g. "PROG"), bypassing
                                          the 24h cache
    POST /api/dashboard/publish-notion -> publish the (currently cached)
                                          dashboard to Notion as a new
                                          "Product Ops <date>" sub-page
    GET  /api/notion/status       -> whether Notion is connected (OAuth) and
                                      to which workspace
    POST /api/notion/disconnect   -> forget the stored Notion OAuth token
    GET  /notion/oauth/start      -> redirects to Notion's OAuth consent
                                      screen ("Connect to Notion" button)
    GET  /notion/oauth/callback   -> OAuth redirect target; exchanges the
                                      code for a token and redirects to "/"
    GET  /                        -> the dashboard web UI (static/index.html)
"""

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import cache, notion_oauth
from .cycles import fetch_teams
from .dashboard import (
    SQUAD_CACHE_VERSION,
    TEAMS_CACHE_KEY,
    build_squad_data,
    fetch_dashboard_teams,
    squad_cache_key,
)
from .linear_client import LinearClient, LinearGraphQLError
from .notion_client import NotionError
from .notion_report import publish_dashboard_to_notion
from .projects import DEFAULT_SUMMIT_LABEL, build_summit_projects_report
from .report import build_current_sprint, build_full_report, build_previous_sprint

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Linear Product Status Service", version="0.1.0")

# Linear data doesn't change fast enough to need a fresh fetch on every hit;
# a short cache keeps the service responsive and avoids burning API quota
# when multiple callers ask "what's the sprint status" within the same window.
CACHE_TTL_SECONDS = int(os.environ.get("SPRINT_STATUS_CACHE_TTL", "120"))
_cache: Dict[Tuple[str, Any], Tuple[float, Any]] = {}


def _cached(key: Tuple[str, Any], fn: Callable[[], Any]) -> Any:
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def _team_list(team: Optional[str]) -> Optional[List[str]]:
    if not team:
        return None
    return [t.strip() for t in team.split(",") if t.strip()]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/sprints")
def sprints(
    team: Optional[str] = Query(default=None, description="Comma-separated team keys/names"),
    fresh: bool = Query(default=False, description="Bypass the cache and refetch from Linear"),
):
    key = ("full", team)
    try:
        if fresh:
            _cache.pop(key, None)
        return _cached(key, lambda: build_full_report(client=LinearClient(), team_filter=_team_list(team)))
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/sprints/current")
def sprints_current(
    team: Optional[str] = Query(default=None),
    fresh: bool = Query(default=False),
):
    key = ("current", team)

    def _fetch():
        client = LinearClient()
        teams = fetch_teams(client, team_filter=_team_list(team))
        return {
            "teams": [
                {
                    "team": {"id": t["id"], "key": t["key"], "name": t["name"]},
                    "currentSprint": build_current_sprint(client, t),
                }
                for t in teams
            ]
        }

    try:
        if fresh:
            _cache.pop(key, None)
        return _cached(key, _fetch)
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


DASHBOARD_MAX_AGE_SECONDS = int(os.environ.get("DASHBOARD_CACHE_MAX_AGE_SECONDS", str(24 * 60 * 60)))


def _get_dashboard_teams(force: bool = False) -> List[Dict[str, Any]]:
    entry = cache.get_or_refresh(
        TEAMS_CACHE_KEY,
        lambda: fetch_dashboard_teams(LinearClient()),
        force=force,
        max_age_seconds=DASHBOARD_MAX_AGE_SECONDS,
    )
    return entry["data"]


def _get_squad(team: Dict[str, Any], force: bool) -> Dict[str, Any]:
    entry = cache.get_or_refresh(
        squad_cache_key(team["key"]),
        lambda: build_squad_data(client=LinearClient(), team=team),
        force=force,
        max_age_seconds=DASHBOARD_MAX_AGE_SECONDS,
        version=SQUAD_CACHE_VERSION,
    )
    return {"fetchedAt": entry["fetchedAt"], **entry["data"]}


@app.get("/api/dashboard")
def dashboard():
    """Sprints + summit projects for every squad. Each squad is served from
    its own on-disk cache unless it's missing or older than 24h, in which
    case just that squad is refetched from Linear and re-cached."""
    try:
        teams = _get_dashboard_teams()
        return {"summitLabel": DEFAULT_SUMMIT_LABEL, "squads": [_get_squad(t, force=False) for t in teams]}
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/dashboard/refresh/{team_key}")
def dashboard_refresh_squad(team_key: str):
    """Force a fresh pull from Linear for one squad only, regardless of
    cache age. `team_key` is the Linear team key, e.g. "PROG"."""
    try:
        teams = _get_dashboard_teams()
        team = next((t for t in teams if t["key"].lower() == team_key.lower()), None)
        if team is None:
            raise HTTPException(status_code=404, detail=f'Unknown squad "{team_key}"')
        return _get_squad(team, force=True)
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/dashboard/refresh")
def dashboard_refresh_all():
    """Force a fresh pull from Linear for every squad, regardless of cache
    age. Not used by the dashboard UI (which refreshes one squad at a
    time), but kept available for scripts/automation."""
    try:
        teams = _get_dashboard_teams(force=True)
        return {"summitLabel": DEFAULT_SUMMIT_LABEL, "squads": [_get_squad(t, force=True) for t in teams]}
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/dashboard/publish-notion")
def dashboard_publish_notion():
    """Publish the currently cached dashboard to Notion as a new
    "Product Ops <date>" sub-page (see `notion_report.py`). Uses whatever
    is already cached per squad rather than forcing a fresh Linear pull -
    hit each squad's Update button first if you want the export to reflect
    the very latest data."""
    try:
        teams = _get_dashboard_teams()
        dashboard_data = {"summitLabel": DEFAULT_SUMMIT_LABEL, "squads": [_get_squad(t, force=False) for t in teams]}
        return publish_dashboard_to_notion(dashboard_data)
    except NotionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/notion/status")
def notion_status():
    """Whether a Notion OAuth connection is stored, and for which
    workspace. Lets the dashboard show "Connect to Notion" vs "Publish to
    Notion" without attempting a publish first."""
    return notion_oauth.status()


@app.post("/api/notion/disconnect")
def notion_disconnect():
    """Forget the stored Notion OAuth token (e.g. to reconnect a different
    workspace, or after revoking access in Notion)."""
    notion_oauth.disconnect()
    return {"connected": False}


@app.get("/notion/oauth/start")
def notion_oauth_start():
    """Redirects the browser to Notion's OAuth consent screen. This has to
    be a real browser navigation (not a fetch call from the dashboard),
    since the user picks which page(s) to share on Notion's own site."""
    try:
        state = notion_oauth.create_state()
        return RedirectResponse(notion_oauth.authorization_url(state))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/notion/oauth/callback")
def notion_oauth_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    """Notion redirects here after the user approves (or denies) access.
    Exchanges the one-time `code` for an access token, then bounces back to
    the dashboard with a query param the frontend reads to show a toast."""
    if error:
        return RedirectResponse(f"/?notion_error={quote(error)}")
    if not code or not state or not notion_oauth.consume_state(state):
        return RedirectResponse("/?notion_error=Invalid+or+expired+OAuth+state")
    try:
        notion_oauth.exchange_code(code)
    except RuntimeError as exc:
        return RedirectResponse(f"/?notion_error={quote(str(exc))}")
    return RedirectResponse("/?notion_connected=1")


@app.get("/projects/summit")
def projects_summit(
    label: str = Query(default=DEFAULT_SUMMIT_LABEL, description="Project label to filter on"),
    fresh: bool = Query(default=False, description="Bypass the cache and refetch from Linear"),
):
    key = ("summit-projects", label)
    try:
        if fresh:
            _cache.pop(key, None)
        return _cached(key, lambda: build_summit_projects_report(client=LinearClient(), label_name=label))
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/sprints/previous")
def sprints_previous(
    team: Optional[str] = Query(default=None),
    fresh: bool = Query(default=False),
):
    key = ("previous", team)

    def _fetch():
        client = LinearClient()
        teams = fetch_teams(client, team_filter=_team_list(team))
        return {
            "teams": [
                {
                    "team": {"id": t["id"], "key": t["key"], "name": t["name"]},
                    "previousSprint": build_previous_sprint(client, t),
                }
                for t in teams
            ]
        }

    try:
        if fresh:
            _cache.pop(key, None)
        return _cached(key, _fetch)
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# Mounted last so it never shadows the API routes above; `html=True` serves
# static/index.html for "/" and any other unmatched path.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard-ui")
