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
                                      projects tagged "Star Project"
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
                                          "EPD Report <date>" sub-page
                                          (skip_sprint_data always defaults
                                          to true - there's no dashboard
                                          control for it, sprint data is
                                          never published; pass
                                          ?skip_sprint_data=false directly
                                          against this endpoint to include
                                          it anyway. ?only_star_projects=true
                                          to omit the quarter-based "Other
                                          projects" group, default false.
                                          ?demo_run=true to only publish the
                                          "Progress" squad, default false.
                                          ?parent_page_url=... to publish
                                          under a different Notion page than
                                          notion_report.DEFAULT_PARENT_PAGE_URL)
    POST /api/dashboard/publish-sprint-report -> publish the (currently
                                          cached) dashboard's sprint data to
                                          Notion as a new "Sprint Report
                                          <date>" sub-page. ?parent_page_url=...
                                          same as above.
    GET  /api/milestones-report       -> every current-quarter project's
                                          milestones on one timeline, plus
                                          anyone flagged as overloaded (see
                                          milestones_report.py) - cached
                                          like /api/dashboard
    POST /api/milestones-report/refresh -> force a fresh pull, bypassing
                                          the 24h cache
    GET  /api/support-report          -> the Support SLA "5 metrics" per
                                          squad, live from Intercom (see
                                          support_report.py) - cached like
                                          /api/dashboard
    POST /api/support-report/refresh  -> force a fresh pull from Intercom,
                                          bypassing the 24h cache (this one
                                          is slow - a couple minutes, see
                                          support_report.py's docstring)
    GET  /api/support-report/history  -> accumulated trend-chart history for
                                          the support report's top table -
                                          one point recorded per actual
                                          refresh (not per page load), see
                                          support_report.py's docstring
    GET  /api/notion/status       -> whether Notion is connected (OAuth) and
                                      to which workspace, plus
                                      defaultParentPageUrl
    POST /api/notion/disconnect   -> forget the stored Notion OAuth token
    GET  /notion/oauth/start      -> redirects to Notion's OAuth consent
                                      screen ("Connect to Notion" button)
    GET  /notion/oauth/callback   -> OAuth redirect target; exchanges the
                                      code for a token and redirects to "/"
    GET  /                        -> the dashboard web UI (static/index.html)

    -- Google sign-in (see auth.py; only enforced when GOOGLE_OAUTH_CLIENT_ID/
       GOOGLE_OAUTH_CLIENT_SECRET are set - see the auth middleware below) --
    GET  /auth/login              -> "Sign in with Google" page
    GET  /auth/google/start       -> redirects to Google's consent screen
    GET  /auth/google/callback    -> OAuth redirect target; verifies the
                                      account's email domain and sets the
                                      signed session cookie
    GET  /auth/logout             -> clears the session cookie
    GET  /api/me                  -> the signed-in user's email/name/picture
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, cache, notion_oauth
from .cycles import fetch_teams
from .dashboard import (
    PROJECTS_REPORT_CACHE_KEY,
    SQUAD_CACHE_VERSION,
    TEAMS_CACHE_KEY,
    build_squad_data,
    fetch_dashboard_teams,
    squad_cache_key,
)
from .linear_client import LinearClient, LinearGraphQLError
from .milestones_report import (
    MILESTONES_REPORT_CACHE_KEY,
    MILESTONES_REPORT_CACHE_VERSION,
    build_milestones_report,
)
from .notion_client import NotionError, extract_page_id
from .notion_report import (
    DEFAULT_PARENT_PAGE_URL,
    publish_dashboard_to_notion,
    publish_sprint_report_to_notion,
)
from .projects import DEFAULT_SUMMIT_LABEL, build_dashboard_projects_report, build_summit_projects_report
from .report import build_current_sprint, build_full_report, build_previous_sprint
from .support_report import (
    SUPPORT_REPORT_CACHE_KEY,
    SUPPORT_REPORT_CACHE_VERSION,
    build_support_report,
    get_support_report_history,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Linear Product Status Service", version="0.1.0")

# Paths reachable without a signed-in session: the login flow itself, plus
# a couple of endpoints that should stay reachable regardless (uptime
# checks, and the static assets the login page itself needs to render).
_AUTH_PUBLIC_PATHS = {
    "/health",
    "/auth/login",
    "/auth/google/start",
    "/auth/google/callback",
    "/auth/logout",
    "/style.css",
    "/app.js",
    "/favicon.svg",
}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Gate every route behind a signed-in @<ALLOWED_EMAIL_DOMAIN> Google
    account - except `_AUTH_PUBLIC_PATHS` above (see module docstring).

    If Google sign-in isn't configured (`auth.is_configured()` is False -
    e.g. `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` aren't set),
    this is a no-op so local dev keeps working without requiring every
    contributor to set up Google OAuth credentials first."""
    if not auth.is_configured() or request.url.path in _AUTH_PUBLIC_PATHS:
        return await call_next(request)

    user = auth.verify_session_cookie(request.cookies.get(auth.SESSION_COOKIE_NAME))
    if user is None:
        if request.url.path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"detail": "Not authenticated - sign in at /auth/login"}, status_code=401)
        return RedirectResponse(url="/auth/login")

    request.state.user = user
    return await call_next(request)


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


_LOGIN_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Sign in - Product Operations</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<link rel="stylesheet" href="/style.css" />
<style>
  body {{ display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .login-card {{
    text-align: center;
    padding: 40px 48px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    max-width: 360px;
  }}
  .login-card .brand-mark {{ font-size: 28px; color: var(--accent); }}
  .login-card h1 {{ font-size: 18px; margin: 8px 0 4px; }}
  .login-card p {{ color: var(--text-faint); font-size: 13.5px; margin: 0 0 20px; }}
  .login-card p.error {{ color: #e05555; margin-bottom: 16px; }}
  .google-btn {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 10px 20px;
    border-radius: 6px;
    background: var(--accent);
    color: #fff;
    text-decoration: none;
    font-weight: 500;
    font-size: 14px;
  }}
  .google-btn:hover {{ opacity: 0.9; }}
</style>
</head>
<body>
  <div class="login-card">
    <div class="brand-mark">◆</div>
    <h1>Product Operations</h1>
    <p>Sign in with your @{domain} Google account to continue.</p>
    {error_html}
    <a class="google-btn" href="/auth/google/start">Sign in with Google</a>
  </div>
</body>
</html>"""


@app.get("/auth/login", response_class=HTMLResponse)
def auth_login(error: Optional[str] = Query(default=None)):
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    return _LOGIN_PAGE_TEMPLATE.format(domain=escape(auth.allowed_domain()), error_html=error_html)


@app.get("/auth/google/start")
def auth_google_start():
    if not auth.is_configured():
        raise HTTPException(
            status_code=500,
            detail="Google sign-in isn't configured (GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET missing)",
        )
    state = auth.create_state()
    return RedirectResponse(url=auth.authorization_url(state))


@app.get("/auth/google/callback")
def auth_google_callback(
    request: Request,
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    if error:
        return RedirectResponse(url=f"/auth/login?error={quote(error)}")
    if not state or not auth.consume_state(state):
        return RedirectResponse(url=f"/auth/login?error={quote('Login expired - please try again.')}")
    if not code:
        return RedirectResponse(url=f"/auth/login?error={quote('Google did not return a login code.')}")

    try:
        profile = auth.exchange_code(code)
    except auth.AuthError as exc:
        return RedirectResponse(url=f"/auth/login?error={quote(str(exc))}")

    response = RedirectResponse(url="/")
    response.set_cookie(
        auth.SESSION_COOKIE_NAME,
        auth.create_session_cookie(profile),
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return response


@app.get("/auth/logout")
def auth_logout():
    response = RedirectResponse(url="/auth/login")
    response.delete_cookie(auth.SESSION_COOKIE_NAME)
    return response


@app.get("/api/me")
def api_me(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        # Auth isn't configured (see `require_login`) - no signed-in user to report.
        return {"authenticated": False}
    return {"authenticated": True, "email": user.get("email"), "name": user.get("name"), "picture": user.get("picture")}


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


def _get_projects_report(force: bool) -> Dict[str, Any]:
    """The workspace-wide projects report, shared across every squad.

    Every squad needs the same data here (see `dashboard.PROJECTS_REPORT_CACHE_KEY`),
    so it's cached and refreshed independently of any one squad rather than
    refetched fresh inside each squad's own build - that used to mean up to
    7x redundant ~5s Linear pulls whenever multiple squads rebuilt at once.
    """
    entry = cache.get_or_refresh(
        PROJECTS_REPORT_CACHE_KEY,
        lambda: build_dashboard_projects_report(client=LinearClient()),
        force=force,
        max_age_seconds=DASHBOARD_MAX_AGE_SECONDS,
    )
    return entry["data"]


def _get_squad(team: Dict[str, Any], force: bool, projects_report: Dict[str, Any]) -> Dict[str, Any]:
    entry = cache.get_or_refresh(
        squad_cache_key(team["key"]),
        lambda: build_squad_data(client=LinearClient(), team=team, projects_report=projects_report),
        force=force,
        max_age_seconds=DASHBOARD_MAX_AGE_SECONDS,
        version=SQUAD_CACHE_VERSION,
    )
    return {"fetchedAt": entry["fetchedAt"], **entry["data"]}


def _get_squads(teams: List[Dict[str, Any]], force: bool) -> List[Dict[str, Any]]:
    """Fetch every squad concurrently rather than one at a time.

    Each squad is independent (own cache entry, own Linear calls), so
    sequentially awaiting them in a single request adds up fast - a cold
    cache (first hit after a 24h expiry, or a SQUAD_CACHE_VERSION bump that
    invalidates every entry at once) means every squad falls through to a
    live multi-query Linear pull, and enough of those back-to-back can blow
    past the platform's function timeout, surfacing to the browser as a
    generic "Failed to fetch". Running them in a thread pool instead bounds
    the worst case to roughly the slowest single squad's fetch time.

    The shared projects report is fetched once up front (see
    `_get_projects_report`) rather than once per squad. `max_workers` is
    kept modest (rather than one thread per squad) because each squad's own
    build fans out into a few concurrent Linear calls too (see
    `build_squad_data`) - capping both levels keeps the total number of
    simultaneous requests to Linear from spiking too high at once.
    """
    if not teams:
        return []
    projects_report = _get_projects_report(force=force)
    with ThreadPoolExecutor(max_workers=min(len(teams), 4)) as pool:
        return list(pool.map(lambda t: _get_squad(t, force, projects_report), teams))


@app.get("/api/dashboard")
def dashboard():
    """Sprints + summit projects for every squad. Each squad is served from
    its own on-disk cache unless it's missing or older than 24h, in which
    case just that squad is refetched from Linear and re-cached."""
    try:
        teams = _get_dashboard_teams()
        return {"summitLabel": DEFAULT_SUMMIT_LABEL, "squads": _get_squads(teams, force=False)}
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/dashboard/refresh/{team_key}")
def dashboard_refresh_squad(team_key: str):
    """Force a fresh pull from Linear for one squad only, regardless of
    cache age. `team_key` is the Linear team key, e.g. "PROG". Also forces
    a fresh pull of the shared projects report (see `_get_projects_report`)
    so this squad's project data is genuinely current too - as a side
    effect this warms that shared cache for every other squad as well."""
    try:
        teams = _get_dashboard_teams()
        team = next((t for t in teams if t["key"].lower() == team_key.lower()), None)
        if team is None:
            raise HTTPException(status_code=404, detail=f'Unknown squad "{team_key}"')
        projects_report = _get_projects_report(force=True)
        return _get_squad(team, force=True, projects_report=projects_report)
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
        return {"summitLabel": DEFAULT_SUMMIT_LABEL, "squads": _get_squads(teams, force=True)}
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/dashboard/publish-notion")
def dashboard_publish_notion(
    skip_sprint_data: bool = Query(
        default=True,
        description="Omit Previous Sprint and reduce Current Sprint to just a heading + commentary callout",
    ),
    only_star_projects: bool = Query(
        default=False,
        description='Only include projects labeled "Star Project" - omits the "Other Projects" group',
    ),
    demo_run: bool = Query(
        default=False,
        description='Only publish the "Progress" squad - for trying out the export without writing every squad',
    ),
    parent_page_url: Optional[str] = Query(
        default=None,
        description="Notion page (URL or bare ID) to publish under - defaults to DEFAULT_PARENT_PAGE_URL",
    ),
):
    """Publish the currently cached dashboard to Notion as a new
    "EPD Report <date>" sub-page (see `notion_report.py`). Uses whatever
    is already cached per squad rather than forcing a fresh Linear pull -
    hit each squad's Update button first if you want the export to reflect
    the very latest data."""
    try:
        teams = _get_dashboard_teams()
        if demo_run:
            teams = [t for t in teams if t["key"].upper() == "PROG"]
        dashboard_data = {"summitLabel": DEFAULT_SUMMIT_LABEL, "squads": _get_squads(teams, force=False)}
        parent_page_id = extract_page_id(parent_page_url) if parent_page_url else None
        return publish_dashboard_to_notion(
            dashboard_data,
            parent_page_id=parent_page_id,
            skip_sprint_data=skip_sprint_data,
            only_star_projects=only_star_projects,
        )
    except NotionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/dashboard/publish-sprint-report")
def dashboard_publish_sprint_report(
    parent_page_url: Optional[str] = Query(
        default=None,
        description="Notion page (URL or bare ID) to publish under - defaults to DEFAULT_PARENT_PAGE_URL",
    ),
):
    """Publish the currently cached dashboard's sprint data to Notion as a
    new "Sprint Report <date>" sub-page (see
    `notion_report.py:publish_sprint_report_to_notion`) - one heading per
    squad with Current Sprint / Previous Sprint tables underneath. Uses
    whatever is already cached per squad rather than forcing a fresh Linear
    pull - hit each squad's Update button first if you want the export to
    reflect the very latest data."""
    try:
        teams = _get_dashboard_teams()
        dashboard_data = {"summitLabel": DEFAULT_SUMMIT_LABEL, "squads": _get_squads(teams, force=False)}
        parent_page_id = extract_page_id(parent_page_url) if parent_page_url else None
        return publish_sprint_report_to_notion(dashboard_data, parent_page_id=parent_page_id)
    except NotionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _get_milestones_report(force: bool) -> Dict[str, Any]:
    entry = cache.get_or_refresh(
        MILESTONES_REPORT_CACHE_KEY,
        lambda: build_milestones_report(client=LinearClient()),
        force=force,
        max_age_seconds=DASHBOARD_MAX_AGE_SECONDS,
        version=MILESTONES_REPORT_CACHE_VERSION,
    )
    return {"fetchedAt": entry["fetchedAt"], **entry["data"]}


@app.get("/api/milestones-report")
def milestones_report():
    """Every current-quarter project's milestones on one timeline, plus
    anyone flagged as overloaded (see `milestones_report.py`). Cached the
    same way as `/api/dashboard` - refetched at most once per 24h unless
    forced via the POST endpoint below."""
    try:
        return _get_milestones_report(force=False)
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/milestones-report/refresh")
def milestones_report_refresh():
    """Force a fresh pull from Linear for the milestones report, regardless of cache age."""
    try:
        return _get_milestones_report(force=True)
    except LinearGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _get_support_report(force: bool) -> Dict[str, Any]:
    entry = cache.get_or_refresh(
        SUPPORT_REPORT_CACHE_KEY,
        lambda: build_support_report(),
        force=force,
        max_age_seconds=DASHBOARD_MAX_AGE_SECONDS,
        version=SUPPORT_REPORT_CACHE_VERSION,
    )
    return {"fetchedAt": entry["fetchedAt"], **entry["data"]}


@app.get("/api/support-report")
def support_report():
    """The Support SLA "5 metrics" per squad, live from Intercom (see
    support_report.py). Cached the same way as /api/dashboard - refetched
    at most once per 24h unless forced via the POST endpoint below."""
    try:
        return _get_support_report(force=False)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/support-report/refresh")
def support_report_refresh():
    """Force a fresh pull from Intercom for the support report, regardless
    of cache age. Slow (a couple of minutes) - see support_report.py."""
    try:
        return _get_support_report(force=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/support-report/history")
def support_report_history():
    """Accumulated trend-chart history for the support report's top table -
    see support_report.py's docstring ("Trend history")."""
    return get_support_report_history()


@app.get("/api/notion/status")
def notion_status():
    """Whether Notion publishing is available right now, and how
    (`method`: "api_key" if NOTION_API_KEY is set - takes priority - or
    "oauth" if a connection is stored, plus which workspace). Lets the
    dashboard show "Connect to Notion" vs "Publish to Notion" without
    attempting a publish first. Also reports `defaultParentPageUrl` (see
    `notion_report.DEFAULT_PARENT_PAGE_URL`) so the dashboard's "Publishes
    to" bars have a default to display/reset to before the user overrides
    it (each tab's override is stored client-side, not here)."""
    return {**notion_oauth.status(), "defaultParentPageUrl": DEFAULT_PARENT_PAGE_URL}


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
