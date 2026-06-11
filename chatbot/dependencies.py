import json

from fastapi import Depends, HTTPException, Query

from chatbot.config import ADMIN_KEY
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Agent

# Agent sessions use a sliding expiration: every authenticated request
# refreshes the TTL, so an active agent is never logged out mid-session.
AGENT_SESSION_TTL = 86400  # 24h


async def get_admin_ctx(key: str = Query(...)) -> dict:
    if key == ADMIN_KEY:
        # Look up super_admin account for their real name
        db = get_db()
        try:
            sa = db.query(Agent).filter(Agent.role == "super_admin").first()
            name = sa.name if sa else "Admin"
            email = sa.email if sa else ""
        finally:
            db.close()
        return {"role": "super_admin", "name": name, "email": email}

    if not redis_client.client:
        # Redis is temporarily unreachable (e.g. mid-restart) — this is an
        # infrastructure hiccup, not an invalid session, so don't force a logout.
        raise HTTPException(status_code=503, detail="Session store unavailable")

    session_key = f"koolbuy:agent_session:{key}"
    try:
        raw = await redis_client.client.get(session_key)
    except Exception:
        raise HTTPException(status_code=503, detail="Session store unavailable")

    if not raw:
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        await redis_client.client.expire(session_key, AGENT_SESSION_TTL)
    except Exception:
        pass

    return json.loads(raw)


async def require_super_admin(ctx: dict = Depends(get_admin_ctx)) -> dict:
    if ctx.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return ctx


async def require_admin(ctx: dict = Depends(get_admin_ctx)) -> dict:
    if ctx.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return ctx
