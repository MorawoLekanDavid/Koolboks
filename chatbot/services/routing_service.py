import json
from datetime import datetime, timedelta

from chatbot.config import log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Agent, ConversationOwner, Lead, Message
from chatbot.services.presence_service import is_online

# Only the frontline support role takes a turn in the rotation — telesales
# is outbound-focused (broadcast/leads-outreach), and team_lead/bi_analyst
# are oversight/reporting roles, not queue members.
ROBIN_ROLES = ("customer_success_agent",)
ROBIN_INDEX_KEY = "koolbuy:routing:robin_index"
ROUTING_CONFIG_KEY = "koolbuy:routing:config"
ACTIVE_CHAT_WINDOW_MINUTES = 10

DEFAULT_CONFIG = {
    "round_robin_enabled": True,  # was already live before this toggle existed
    "sticky_enabled": False,      # new algorithm — starts off until explicitly enabled
    "capacity_enabled": False,    # same
    "max_chats": 5,
}


async def get_routing_config() -> dict:
    if not redis_client.client:
        return dict(DEFAULT_CONFIG)
    raw = await redis_client.client.get(ROUTING_CONFIG_KEY)
    if not raw:
        return dict(DEFAULT_CONFIG)
    try:
        cfg = json.loads(raw)
    except Exception:
        return dict(DEFAULT_CONFIG)
    return {**DEFAULT_CONFIG, **cfg}


async def set_routing_config(patch: dict) -> dict:
    cfg = await get_routing_config()
    cfg.update(patch)
    if redis_client.client:
        await redis_client.client.set(ROUTING_CONFIG_KEY, json.dumps(cfg))
    return cfg


# Backward-compat wrappers for the on/off toggle shipped before sticky/capacity existed.
async def is_routing_enabled() -> bool:
    return (await get_routing_config())["round_robin_enabled"]


async def set_routing_enabled(enabled: bool) -> None:
    await set_routing_config({"round_robin_enabled": enabled})


async def _active_chat_count(db, agent_name: str) -> int:
    cutoff = datetime.utcnow() - timedelta(minutes=ACTIVE_CHAT_WINDOW_MINUTES)
    return (
        db.query(Message.phone)
        .filter(Message.name == agent_name, Message.created_at >= cutoff)
        .distinct()
        .count()
    )


async def _pick_sticky_owner(db, phone: str) -> Agent | None:
    """Returning-customer check: if this phone was created as a lead/contact
    by a specific agent and that agent is currently online, route back to
    them instead of round-robining to a stranger."""
    lead = db.query(Lead).filter(Lead.phone == phone).first()
    if not lead or not lead.created_by:
        return None
    creator = (
        db.query(Agent)
        .filter((Agent.name == lead.created_by) | (Agent.email == lead.created_by))
        .first()
    )
    if not creator:
        return None
    if await is_online(creator.id):
        return creator
    return None


async def auto_assign_conversation(phone: str) -> None:
    """Assigns a brand-new conversation to an agent, so inbound chats don't
    sit unassigned until someone manually claims them. No-ops if this phone
    already has an owner — runs once per conversation, not on every inbound
    message. Never touches the bot/handoff state; this only fills in "who
    owns this," same as manually picking from the Assign to... dropdown
    would.

    Pipeline: Sticky Ownership (if enabled) -> Capacity-filtered Round Robin
    (if round robin enabled) -> unassigned if nothing matches."""
    cfg = await get_routing_config()
    if not (cfg["round_robin_enabled"] or cfg["sticky_enabled"]):
        return

    db = get_db()
    try:
        existing = db.query(ConversationOwner).filter(ConversationOwner.phone == phone).first()
        if existing:
            return

        picked: Agent | None = None

        if cfg["sticky_enabled"]:
            picked = await _pick_sticky_owner(db, phone)
            if picked:
                log.info(f"[Sticky] Routing {phone} back to {picked.name}")

        if not picked and cfg["round_robin_enabled"]:
            agents = db.query(Agent).filter(Agent.role.in_(ROBIN_ROLES)).order_by(Agent.id).all()
            if agents:
                pool = agents
                if cfg["capacity_enabled"]:
                    under_capacity = []
                    for a in agents:
                        if not await is_online(a.id):
                            continue
                        count = await _active_chat_count(db, a.name)
                        if count < cfg["max_chats"]:
                            under_capacity.append(a)
                    # If nobody online is under capacity (or nobody's online at
                    # all), fall back to the full pool rather than stockpiling
                    # unassigned chats — better distributed-but-imperfect than
                    # silently unassigned.
                    pool = under_capacity or agents

                idx = 0
                if redis_client.client:
                    idx = await redis_client.client.incr(ROBIN_INDEX_KEY)
                picked = pool[idx % len(pool)]
                log.info(f"[Round-robin] Assigned {phone} to {picked.name}")

        if not picked:
            return

        db.add(ConversationOwner(phone=phone, owner_name=picked.name, owner_email=picked.email))
        try:
            db.commit()
        except Exception:
            # Another concurrent webhook delivery for the same new phone won
            # the race and already inserted a row (phone is unique) — fine,
            # the conversation is owned either way.
            db.rollback()
    except Exception as e:
        log.warning(f"Auto-assignment failed for {phone}: {e}")
    finally:
        db.close()
