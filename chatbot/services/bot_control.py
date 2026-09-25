import json
from datetime import datetime
from typing import Optional

from chatbot.core import redis_client

# The single global on/off switch for the AI bot, checked in every place the
# bot can autonomously generate and send a message: the live webhook reply
# path, the handoff watchdog's auto-resume, the handback-resume fix, and the
# automated follow-up worker. Deliberately separate from per-conversation
# handoff (koolbuy:handoff:<session_id>) -- that's one agent claiming one
# chat; this is "nobody gets a bot reply until a super admin turns it back
# on," which is why it's gated at that role rather than the normal
# per-tab permission system every other setting uses.
_GLOBAL_DISABLE_KEY = "koolbuy:system:bot_globally_disabled"


async def get_bot_status() -> dict:
    """Fails open (bot enabled) if Redis is unreachable -- an infrastructure
    hiccup should never silently take every conversation off the bot with
    nobody aware it happened."""
    default = {"disabled": False, "by": None, "since": None, "reason": None}
    if not redis_client.client:
        return default
    try:
        raw = await redis_client.client.get(_GLOBAL_DISABLE_KEY)
    except Exception:
        return default
    if not raw:
        return default
    try:
        data = json.loads(raw)
        data["disabled"] = True
        return data
    except Exception:
        return default


async def is_bot_globally_disabled() -> bool:
    return (await get_bot_status())["disabled"]


async def set_bot_globally_disabled(disabled: bool, by: str, reason: Optional[str] = None) -> dict:
    if not redis_client.client:
        raise RuntimeError("Session store unavailable")
    if disabled:
        payload = {"by": by, "since": datetime.utcnow().isoformat(), "reason": reason}
        await redis_client.client.set(_GLOBAL_DISABLE_KEY, json.dumps(payload))
    else:
        await redis_client.client.delete(_GLOBAL_DISABLE_KEY)
    return await get_bot_status()
