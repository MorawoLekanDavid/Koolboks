"""Cloud startup and durable queue wakeups; no credentials in browser code."""
import logging
import time
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .api import create_app
from .bootstrap import provision_initial_admin
from .db import Config
from .models import Audit, Workspace

def queue_health(sessions, action, category):
    """Persist bounded, non-sensitive diagnostics when platform logs are unavailable."""
    with sessions() as db:
        for (workspace_id,) in db.query(Workspace.id).all():
            recent = db.query(Audit).filter(Audit.workspace_id == workspace_id,
                Audit.actor_id == "system", Audit.action == action,
                Audit.entity_id == category, Audit.created_at > time.time() - 300).first()
            if not recent:
                db.add(Audit(workspace_id=workspace_id, actor_id="system", action=action, entity_id=category))
        db.commit()

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
                if await wake_if_needed(app.state.sessions):
                    queue_health(app.state.sessions, "queue.wake.accepted", "itura-jobs")
            except Exception as error:
                logging.getLogger(__name__).warning("Queue wake failed; committed jobs remain in the durable database")
                try:
                    queue_health(app.state.sessions, "queue.wake.failed", type(error).__name__[:80])
                except SQLAlchemyError:
                    logging.getLogger(__name__).warning("Queue health status could not be persisted")
                if request.url.path.startswith("/webhooks/"):
                    return JSONResponse({"detail": "Message retained; retry delivery."}, status_code=503)
        return response
    return app
