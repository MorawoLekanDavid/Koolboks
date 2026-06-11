import json

from fastapi import Depends, HTTPException, Query

from chatbot.config import ADMIN_KEY
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Agent


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
    if redis_client.client:
        try:
            raw = await redis_client.client.get(f"koolbuy:agent_session:{key}")
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    raise HTTPException(status_code=403, detail="Unauthorized")


async def require_super_admin(ctx: dict = Depends(get_admin_ctx)) -> dict:
    if ctx.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return ctx


async def require_admin(ctx: dict = Depends(get_admin_ctx)) -> dict:
    if ctx.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return ctx
