from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from chatbot.dependencies import get_admin_ctx, require_super_admin
from chatbot.services.bot_control import get_bot_status, set_bot_globally_disabled
from chatbot.workers.handoff_watchdog import resume_all_pending_after_reenable

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
async def set_bot_status(
    body: BotSwitchRequest, background_tasks: BackgroundTasks,
    ctx: dict = Depends(require_super_admin),
):
    by = ctx.get("name") or ctx.get("email") or "Super Admin"
    # Captured BEFORE the switch flips -- set_bot_globally_disabled(False, ...)
    # clears this from Redis, so the "since it went off" timestamp has to be
    # read first or the re-enable sweep below has no window to scan.
    was = await get_bot_status()
    result = await set_bot_globally_disabled(body.disabled, by, body.reason)
    if was["disabled"] and not body.disabled:
        # Turning it back on doesn't just change future behavior -- anything
        # that arrived while it was off is sitting exactly as unanswered as
        # a stale handoff would be, so this resumes all of it immediately
        # instead of waiting on the next customer message or the slower
        # periodic watchdog. Runs as a background task so the toggle itself
        # responds instantly even if there's a lot to catch up on.
        background_tasks.add_task(resume_all_pending_after_reenable, was["since"], by)
    return result
