"""Cloud startup and durable queue wakeups; no credentials in browser code."""
import logging
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .api import create_app
from .bootstrap import provision_initial_admin
from .db import Config

def build_app():
    try:
        config = Config.from_env()
    except (RuntimeError, ValueError):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        @app.middleware("http")
        async def not_configured(request, _):
            if request.url.path == "/health/live":
                return JSONResponse({"status": "alive"})
            return JSONResponse({"detail": "Backend configuration is incomplete. Contact your administrator."}, status_code=503)
        return app

    app = create_app(config)
    app.router.on_startup.append(lambda: provision_initial_admin(config))

    @app.middleware("http")
    async def cloud_boundary(request, call_next):
        try:
            response = await call_next(request)
        except SQLAlchemyError:
            return JSONResponse({"detail": "Database service is unavailable. Contact your administrator."}, status_code=503)
        writes = request.method in {"POST", "PATCH"} and request.url.path.startswith(("/api/", "/web/", "/webhooks/"))
        recovery_poll = request.method == "GET" and request.url.path in {"/api/conversations", "/web/messages"}
        if response.status_code < 400 and (writes or recovery_poll):
            from .queue_worker import wake_if_needed
            try:
                await wake_if_needed(app.state.sessions)
            except Exception:
                logging.getLogger(__name__).warning("Queue wake failed; committed jobs remain in the durable database")
                if request.url.path.startswith("/webhooks/"):
                    return JSONResponse({"detail": "Message retained; retry delivery."}, status_code=503)
        return response
    return app
