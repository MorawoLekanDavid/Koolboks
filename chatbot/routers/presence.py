from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from chatbot.dependencies import get_admin_ctx
from chatbot.services import presence_service

router = APIRouter(prefix="/admin/presence", tags=["presence"])


@router.post("/heartbeat")
async def heartbeat(ctx: dict = Depends(get_admin_ctx)):
    """Pinged every 20s while the dashboard tab is open and visible. Refreshes
    the 45s liveness TTL — missing a couple of these (tab crash, network
    drop) self-heals to offline without needing an explicit signal."""
    agent_id = ctx.get("agent_id")
    if agent_id:
        await presence_service.heartbeat(agent_id)
    return {"ok": True}


@router.post("/offline")
async def go_offline(ctx: dict = Depends(get_admin_ctx)):
    """Fired via navigator.sendBeacon() on tab close/refresh/navigation —
    goes offline immediately instead of waiting out the heartbeat TTL."""
    agent_id = ctx.get("agent_id")
    if agent_id:
        await presence_service.clear_heartbeat(agent_id)
    return {"ok": True}


class AwayIn(BaseModel):
    away: bool


@router.put("/away")
async def set_away(body: AwayIn, ctx: dict = Depends(get_admin_ctx)):
    """Manual override — 'here but stepped out' without closing the tab.
    Layered on top of the heartbeat: still requires an alive heartbeat to
    show as anything other than offline."""
    agent_id = ctx.get("agent_id")
    if not agent_id:
        raise HTTPException(400, "Super admin sessions via the master key don't have a presence status.")
    await presence_service.set_away(agent_id, body.away)
    return {"away": body.away}
