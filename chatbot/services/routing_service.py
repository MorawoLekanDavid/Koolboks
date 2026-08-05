from chatbot.config import log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Agent, ConversationOwner

# Only the frontline support roles take a turn in the rotation — telesales/sales
# agents are outbound-focused (broadcast/leads-outreach) and default to no
# leads/contacts access, so they aren't meant to inherit inbound support chats.
ROBIN_ROLES = ("agent", "customer_success_agent")
ROBIN_INDEX_KEY = "koolbuy:routing:robin_index"


async def auto_assign_conversation(phone: str) -> None:
    """Round-robin a brand-new conversation to the next eligible agent, so
    inbound chats don't sit unassigned until someone manually claims them.
    No-ops if this phone already has an owner — runs once per conversation,
    not on every inbound message. Never touches the bot/handoff state; this
    only fills in "who owns this," same as manually picking from the
    Assign to... dropdown would."""
    db = get_db()
    try:
        existing = db.query(ConversationOwner).filter(ConversationOwner.phone == phone).first()
        if existing:
            return

        agents = (
            db.query(Agent)
            .filter(Agent.role.in_(ROBIN_ROLES))
            .order_by(Agent.id)
            .all()
        )
        if not agents:
            return

        idx = 0
        if redis_client.client:
            idx = await redis_client.client.incr(ROBIN_INDEX_KEY)
        picked = agents[idx % len(agents)]

        db.add(ConversationOwner(phone=phone, owner_name=picked.name, owner_email=picked.email))
        try:
            db.commit()
            log.info(f"Round-robin assigned {phone} to {picked.name}")
        except Exception:
            # Another concurrent webhook delivery for the same new phone won
            # the race and already inserted a row (phone is unique) — fine,
            # the conversation is owned either way.
            db.rollback()
    except Exception as e:
        log.warning(f"Round-robin assignment failed for {phone}: {e}")
    finally:
        db.close()
