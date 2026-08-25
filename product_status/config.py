"""Configuration loading for the sprint status service.

API key resolution order:
1. `LINEAR_API_KEY` environment variable.
2. A `.env` file in the project root (loaded via python-dotenv).
3. A fallback `.env` used by other local Linear tooling on this machine,
   so this service works out of the box in this workspace without asking
   the user to re-enter a key that already exists locally.
"""

import os
import tempfile
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_ENV_PATH = Path("/Users/vishwa/.claude/skills/product-ops/.env")

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"


def _default_cache_dir() -> Path:
    # Serverless platforms (Vercel, AWS Lambda, ...) ship the deployed code
    # on a read-only filesystem and only allow writes under `/tmp` - which
    # is itself ephemeral (wiped on cold start, not shared across scaled-out
    # instances), so this is a "works for now" fallback rather than a real
    # fix for the dashboard cache / Notion OAuth token needing to persist -
    # see README "Deploying" for details. Vercel sets `VERCEL=1` in the
    # function's environment, which is what triggers this.
    if os.environ.get("VERCEL"):
        return Path(tempfile.gettempdir()) / "product-ops-cache"
    return PROJECT_ROOT / ".cache"


# Override with `PRODUCT_OPS_CACHE_DIR` to point at a persistent volume/mount
# on other hosts where the project root itself isn't writable.
CACHE_DIR = Path(os.environ.get("PRODUCT_OPS_CACHE_DIR") or _default_cache_dir())


def _load_env_files() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    if FALLBACK_ENV_PATH.exists():
        load_dotenv(FALLBACK_ENV_PATH, override=False)


_load_env_files()


def get_api_key() -> str:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RuntimeError(
            "LINEAR_API_KEY is not set. Add it to a .env file in the project "
            "root (see .env.example) or export it in your shell."
        )
    return key


def get_team_filter() -> Optional[List[str]]:
    """Optional allow-list of team keys/names from SPRINT_STATUS_TEAMS."""
    raw = os.environ.get("SPRINT_STATUS_TEAMS", "").strip()
    if not raw:
        return None
    return [part.strip().lower() for part in raw.split(",") if part.strip()]
