from chatbot.core import redis_client

# Two independent signals combine into the displayed status:
#   heartbeat alive + not away  -> online
#   heartbeat alive + away      -> away
#   heartbeat dead (any away)   -> offline
# The heartbeat key is the source of truth for "is the dashboard tab even
# open" — it's refreshed by a periodic ping from the frontend and cleared
# instantly by a sendBeacon on tab close, so it self-heals within one TTL
# window (45s) even if the tab crashes or the network drops without a clean
# close. "Away" is a pure manual override on top of that, for "I'm here but
# stepped out" without closing the tab.
HEARTBEAT_PREFIX = "koolbuy:agent_presence:"
AWAY_PREFIX = "koolbuy:agent_status:"
HEARTBEAT_TTL = 45  # seconds — outlasts a couple of missed 20s heartbeats


def _hb_key(agent_id: int) -> str:
    return f"{HEARTBEAT_PREFIX}{agent_id}"


def _away_key(agent_id: int) -> str:
    return f"{AWAY_PREFIX}{agent_id}"


async def heartbeat(agent_id: int) -> None:
    if redis_client.client:
        await redis_client.client.set(_hb_key(agent_id), "1", ex=HEARTBEAT_TTL)


async def clear_heartbeat(agent_id: int) -> None:
    """Clean tab-close beacon — go offline immediately instead of waiting
    out the TTL."""
    if redis_client.client:
        await redis_client.client.delete(_hb_key(agent_id))


async def set_away(agent_id: int, away: bool) -> None:
    if redis_client.client:
        await redis_client.client.set(_away_key(agent_id), "1" if away else "0")


async def _is_away(agent_id: int) -> bool:
    if not redis_client.client:
        return False
    val = await redis_client.client.get(_away_key(agent_id))
    return val == "1"


async def is_online(agent_id: int) -> bool:
    """Eligible for routing: dashboard tab open (heartbeat alive) and not
    manually marked Away."""
    if not redis_client.client:
        return False
    alive = await redis_client.client.exists(_hb_key(agent_id))
    if not alive:
        return False
    return not await _is_away(agent_id)


async def get_status(agent_id: int) -> str:
    """Display-only tri-state: online | away | offline."""
    if not redis_client.client:
        return "offline"
    alive = await redis_client.client.exists(_hb_key(agent_id))
    if not alive:
        return "offline"
    return "away" if await _is_away(agent_id) else "online"


async def get_statuses(agent_ids: list[int]) -> dict[int, str]:
    if not agent_ids or not redis_client.client:
        return {aid: "offline" for aid in agent_ids}
    pipe = redis_client.client.pipeline()
    for aid in agent_ids:
        pipe.exists(_hb_key(aid))
    for aid in agent_ids:
        pipe.get(_away_key(aid))
    results = await pipe.execute()
    n = len(agent_ids)
    alive_flags, away_flags = results[:n], results[n:]
    out = {}
    for aid, alive, away in zip(agent_ids, alive_flags, away_flags):
        out[aid] = "offline" if not alive else ("away" if away == "1" else "online")
    return out
