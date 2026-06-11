from fastapi import APIRouter, Depends

from chatbot.config import (
    FOLLOW_UP_ENABLED,
    FOLLOW_UP_HOURS,
    FOLLOW_UP_MESSAGE,
    FOLLOW_UP_RECHECK_DAYS,
)
from chatbot.dependencies import require_admin
from chatbot.workers.follow_up import run_follow_ups

router = APIRouter(prefix="/admin/follow-up", tags=["follow-up"])


@router.get("/config")
async def get_follow_up_config(ctx: dict = Depends(require_admin)):
    return {
        "enabled": FOLLOW_UP_ENABLED,
        "hours": FOLLOW_UP_HOURS,
        "recheck_days": FOLLOW_UP_RECHECK_DAYS,
        "message": FOLLOW_UP_MESSAGE,
    }


@router.get("/trigger")
async def trigger_follow_ups(ctx: dict = Depends(require_admin)):
    """Manually trigger a follow-up run (useful for testing)."""
    await run_follow_ups()
    return {"status": "done"}
