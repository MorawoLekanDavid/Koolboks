from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chatbot.dependencies import get_admin_ctx, require_super_admin
from chatbot.services.bot_control import get_bot_status, set_bot_globally_disabled

router = APIRouter(prefix="/admin/system", tags=["system"])


@router.get("/bot-status")
async def bot_status(ctx: dict = Depends(get_admin_ctx)):
    """Any signed-in agent can see whether the bot is globally on or off --
    everyone needs to know this to understand why every chat has gone quiet
    -- but only a super admin can change it (see the POST below)."""
    return await get_bot_status()


class BotSwitchRequest(BaseModel):
    disabled: bool
    reason: Optional[str] = None


@router.post("/bot-status")
async def set_bot_status(body: BotSwitchRequest, ctx: dict = Depends(require_super_admin)):
    by = ctx.get("name") or ctx.get("email") or "Super Admin"
    return await set_bot_globally_disabled(body.disabled, by, body.reason)
