"""Vercel's native FastAPI service entrypoint."""
from platform_v2.deployment import build_app

app = build_app()
