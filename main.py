"""Deployment entrypoint (e.g. for Vercel's Python runtime).

Vercel's FastAPI framework preset only auto-detects an `app` instance in a
handful of default locations (`app.py`, `index.py`, `server.py`, `main.py`,
`wsgi.py`, or `asgi.py` at the project root or under `src/`/`app/`/`api/`) -
this project's app lives at `product_status/server.py`, which isn't one of
those, so this file just re-exports it under a recognized name.

Local development should still use `product_status.server` directly, e.g.:
    uvicorn product_status.server:app --reload --port 8008
"""

from product_status.server import app

__all__ = ["app"]
