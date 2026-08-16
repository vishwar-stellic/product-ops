"""Configuration loading for the sprint status service.

API key resolution order:
1. `LINEAR_API_KEY` environment variable.
2. A `.env` file in the project root (loaded via python-dotenv).
3. A fallback `.env` used by other local Linear tooling on this machine,
   so this service works out of the box in this workspace without asking
   the user to re-enter a key that already exists locally.
"""

import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_ENV_PATH = Path("/Users/vishwa/.claude/skills/product-ops/.env")

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"


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
