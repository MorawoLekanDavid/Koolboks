from chatbot.core import redis_client

VALID_STATUSES = ("online", "away", "offline")
STATUS_KEY_PREFIX = "koolbuy:agent_status:"


def _key(agent_id: int) -> str:
    return f"{STATUS_KEY_PREFIX}{agent_id}"


async def set_status(agent_id: int, status: str) -> None:
    if status not in VALID_STATUSES:
        status = "offline"
    if redis_client.client:
        await redis_client.client.set(_key(agent_id), status)


async def get_status(agent_id: int) -> str:
    """No status ever set (agent has never toggled it) defaults to offline —
    routing should never treat a never-logged-in-to-presence agent as
    available."""
    if not redis_client.client:
        return "offline"
    val = await redis_client.client.get(_key(agent_id))
    return val if val in VALID_STATUSES else "offline"


async def get_statuses(agent_ids: list[int]) -> dict[int, str]:
    if not agent_ids or not redis_client.client:
        return {aid: "offline" for aid in agent_ids}
    keys = [_key(aid) for aid in agent_ids]
    vals = await redis_client.client.mget(*keys)
    return {aid: (v if v in VALID_STATUSES else "offline") for aid, v in zip(agent_ids, vals)}


async def is_online(agent_id: int) -> bool:
    return await get_status(agent_id) == "online"
