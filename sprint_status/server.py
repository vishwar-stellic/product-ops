"""HTTP service exposing sprint status on demand.

Run with:
    uvicorn sprint_status.server:app --reload --port 8008

Endpoints:
    GET /health
    GET /sprints                 -> current + previous sprint, all teams
    GET /sprints/current         -> current sprint only, all teams
    GET /sprints/previous        -> previous sprint only, all teams
    GET /sprints?team=PROG,PLAN  -> restrict to specific teams (key or name)
"""

import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query

from .cycles import fetch_teams
from .linear_client import LinearClient, LinearGraphQLError
from .report import build_current_sprint, build_full_report, build_previous_sprint

app = FastAPI(title="Linear Sprint Status Service", version="0.1.0")

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
