import re
from datetime import datetime, timedelta
from typing import Optional

from chatbot.config import ESCALATION_ALERT_TEMPLATE, ESCALATION_SLA_HOURS, log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Agent, ConversationOwner, Escalation, HandoffEvent, Message
from chatbot.services.whatsapp_service import send_whatsapp_template

# Fixed vocabularies, enforced here rather than as a DB-level enum -- same
# convention as ConversationScore.issues elsewhere in this codebase. Kept
# deliberately small per the brief ("don't over-engineer the categorisation").
CATEGORIES = (
    "general_enquiry", "sales", "payment", "credit", "technical",
    "aftersales", "delivery", "installation", "complaint", "fraud",
    "safety", "other",
)
PRIORITIES = ("low", "medium", "high")
TRIGGERS = ("customer_request", "bot_unresolved", "sensitive", "unviewable_media")

# A sensitive/urgent trigger is always HIGH regardless of what the model
# picked -- these categories are exactly the ones the brief calls out as
# needing immediate attention (payment dispute, fraud, safety, legal-adjacent
# complaints), so priority isn't left to the model's judgement for these.
_ALWAYS_HIGH_CATEGORIES = {"payment", "fraud", "safety"}

# The model emits a single line, e.g.:
#   ESCALATE: trigger=sensitive category=payment priority=high summary=Customer says payment isn't reflecting.
# Mirrors the existing PRODUCTS: tag convention in chat_service.py -- same
# single-call, same-turn pattern, no separate classifier call needed. `trigger`
# is which of the three brief-defined reasons this is (customer_request |
# bot_unresolved | sensitive) -- kept separate from `category` (what it's
# about) so "why did Itura need a human" and "what was it about" can each be
# reported on independently later.
_ESCALATE_TAG_RE = re.compile(
    r'ESCALATE:\s*trigger=(?P<trigger>\w+)\s+category=(?P<category>\w+)\s+priority=(?P<priority>\w+)\s+summary=(?P<summary>.+?)\s*$',
    re.IGNORECASE | re.MULTILINE,
)


def parse_escalation_tag(raw: str) -> Optional[dict]:
    """Returns {"trigger", "category", "priority", "summary"} if the model's
    raw output contains an ESCALATE: tag, else None. Unknown category/
    priority/trigger values fall back to safe defaults rather than rejecting
    the whole escalation -- a slightly-wrong field shouldn't mean the
    customer's request for help silently goes nowhere."""
    m = _ESCALATE_TAG_RE.search(raw)
    if not m:
        return None
    trigger = m.group("trigger").lower()
    if trigger not in TRIGGERS:
        trigger = "customer_request"
    category = m.group("category").lower()
    if category not in CATEGORIES:
        category = "other"
    priority = m.group("priority").lower()
    if priority not in PRIORITIES:
        priority = "medium"
    if category in _ALWAYS_HIGH_CATEGORIES:
        priority = "high"
    summary = m.group("summary").strip()[:1000]
    return {"trigger": trigger, "category": category, "priority": priority, "summary": summary}


def strip_escalation_tag(raw: str) -> str:
    """Removes the ESCALATE: line from the text before it's shown to the
    customer -- same treatment PRODUCTS: already gets."""
    return _ESCALATE_TAG_RE.sub("", raw).strip()


async def set_handoff(session_id: str, phone: str, label: str, ttl: int = 86400) -> None:
    """Silences the bot for this conversation the same proven way a manual
    agent takeover already does (same Redis key, same HandoffEvent log) --
    shared by every trigger (media, the new tag-based ones) so there's one
    real implementation of "hand this off" instead of several drifting
    copies. Moved here from bot_response.py's former _set_media_handoff so
    escalation_service can call it without bot_response needing to import
    escalation_service and vice versa."""
    if redis_client.client:
        try:
            await redis_client.client.set(f"koolbuy:handoff:{session_id}", label, ex=ttl)
        except Exception as e:
            log.warning(f"Failed to set handoff key for {session_id}: {e}")

    db = get_db()
    try:
        db.add(HandoffEvent(phone=phone, agent_name=label, event_type="takeover"))
        db.commit()
    except Exception as e:
        log.warning(f"Failed to log handoff event for {phone}: {e}")
    finally:
        db.close()


async def _resolve_notification_target(phone: str) -> tuple[Optional[Agent], bool]:
    """Who gets pinged: the conversation's already-assigned owner if one
    exists, else the routing system's configured fallback agent (the same
    "who catches things when nobody's clearly responsible" concept
    routing_service.py already uses for unclaimed new conversations, reused
    here rather than inventing a second "on-duty" idea). (None, False) if
    neither is set -- the escalation still gets created and stays visible
    in-app either way, it just has nobody to notify yet.

    Returns (agent, is_owner) -- is_owner tells the caller whether this is a
    REAL pre-existing owner (the ticket should be auto-assigned to them, not
    just notified) or the generic fallback catch-all (notify, but don't
    assign -- a fallback agent hasn't actually claimed anything)."""
    from chatbot.services.routing_service import get_routing_config

    db = get_db()
    try:
        owner = db.query(ConversationOwner).filter(ConversationOwner.phone == phone).first()
        if owner and owner.owner_email:
            agent = db.query(Agent).filter(Agent.email == owner.owner_email).first()
            if agent and agent.phone_number:
                return agent, True
    finally:
        db.close()

    cfg = await get_routing_config()
    fallback_id = cfg.get("fallback_agent_id")
    if not fallback_id:
        return None, False
    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.id == fallback_id).first()
        return (agent, False) if agent and agent.phone_number else (None, False)
    finally:
        db.close()


def _auto_assign_from_owner(escalation_id: int, agent: Agent) -> None:
    """Links an escalation to the conversation owner that _resolve_notification_target
    already found -- without this, an escalation on an already-owned chat sat
    in the queue as "Unassigned" forever unless someone manually hit Claim,
    even though exactly one person could ever see or reply to that
    conversation in the first place. No-ops if already assigned to someone
    (never silently steals a ticket) or already resolved."""
    db = get_db()
    try:
        esc = db.query(Escalation).filter(Escalation.id == escalation_id).first()
        if not esc or esc.status == "resolved" or esc.assigned_agent_id == agent.id:
            return
        if esc.assigned_agent_id is None:
            esc.assigned_agent_id = agent.id
            esc.assigned_at = datetime.utcnow()
            if esc.status == "open":
                esc.status = "assigned"
            db.commit()
    finally:
        db.close()


async def _notify_agent(escalation_id: int, agent: Agent, phone: str, customer_name: str,
                         category: str, priority: str, summary: str) -> None:
    """Best-effort -- a failed send updates the escalation's notification
    fields but never touches its status. The escalation itself is already
    persisted by the time this runs."""
    db = get_db()
    try:
        esc = db.query(Escalation).filter(Escalation.id == escalation_id).first()
        if not esc:
            return
        try:
            sent = await send_whatsapp_template(
                agent.phone_number, ESCALATION_ALERT_TEMPLATE,
                [customer_name or phone, category.replace("_", " ").title(),
                 priority.upper(), summary or "No summary available."],
            )
            esc.notified_at = datetime.utcnow()
            esc.notification_status = "sent" if sent else "failed"
            esc.notification_error = None if sent else (
                f"send_whatsapp_template returned False for template "
                f"'{ESCALATION_ALERT_TEMPLATE}' -- likely not yet created/approved in Meta Business Manager."
            )
        except Exception as e:
            esc.notified_at = datetime.utcnow()
            esc.notification_status = "failed"
            esc.notification_error = str(e)[:500]
            log.warning(f"Escalation notification failed for #{escalation_id}: {e}")
        db.commit()
    finally:
        db.close()


def escalation_to_dict(e: Escalation) -> dict:
    """Single source of truth for the JSON shape the admin UI consumes --
    shared by the list/update endpoints and retry_notification() below so
    there's exactly one place that knows Escalation's serialized fields."""
    return {
        "id": e.id,
        "phone": e.phone,
        "customer_name": e.customer_name,
        "category": e.category,
        "priority": e.priority,
        "trigger": e.trigger,
        "status": e.status,
        "summary": e.summary,
        "assigned_agent_id": e.assigned_agent_id,
        "assigned_agent_name": e.assigned_agent.name if e.assigned_agent else None,
        "notified_at": e.notified_at.isoformat() if e.notified_at else None,
        "notification_status": e.notification_status,
        "notification_error": e.notification_error,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "assigned_at": e.assigned_at.isoformat() if e.assigned_at else None,
        "first_response_at": e.first_response_at.isoformat() if e.first_response_at else None,
        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
        "resolution": e.resolution,
        "reopen_count": e.reopen_count,
    }


async def retry_notification(escalation_id: int) -> Optional[dict]:
    """Re-attempts the alert for an escalation that was previously skipped or
    failed -- for when an admin fixes the underlying cause after the fact
    (adds a routing fallback agent, gives the assigned agent a phone number,
    etc) rather than waiting for the next unrelated escalation to prove it
    works. Reuses the exact same resolve+send path create_escalation uses."""
    db = get_db()
    try:
        esc = db.query(Escalation).filter(Escalation.id == escalation_id).first()
        if not esc:
            return None
        phone, customer_name = esc.phone, esc.customer_name
        category, priority, summary = esc.category, esc.priority, esc.summary
    finally:
        db.close()

    agent, is_owner = await _resolve_notification_target(phone)
    if agent:
        if is_owner:
            _auto_assign_from_owner(escalation_id, agent)
        await _notify_agent(escalation_id, agent, phone, customer_name, category, priority, summary or "")
    else:
        db = get_db()
        try:
            esc = db.query(Escalation).filter(Escalation.id == escalation_id).first()
            if esc:
                esc.notification_status = "skipped"
                esc.notification_error = "No conversation owner and no routing fallback_agent_id configured."
                db.commit()
        finally:
            db.close()

    db = get_db()
    try:
        esc = db.query(Escalation).filter(Escalation.id == escalation_id).first()
        return escalation_to_dict(esc) if esc else None
    finally:
        db.close()


async def has_open_escalation(phone: str) -> Optional[int]:
    """Returns the id of an already-open (open/assigned/in_progress)
    escalation for this phone, if one exists -- used to avoid opening a
    second ticket for the same ongoing issue every time the customer sends
    another message. A conversation with a RESOLVED escalation is free to
    open a new one; an OPEN one just keeps standing."""
    db = get_db()
    try:
        existing = (
            db.query(Escalation)
            .filter(Escalation.phone == phone, Escalation.status.in_(("open", "assigned", "in_progress")))
            .first()
        )
        return existing.id if existing else None
    finally:
        db.close()


async def create_escalation(
    session_id: str, phone: str, trigger: str, customer_name: Optional[str] = None,
    category: str = "general_enquiry", priority: str = "medium",
    summary: Optional[str] = None,
) -> Optional[int]:
    """The single entry point for every escalation trigger -- the new
    tag-based ones from chat_service.py AND the pre-existing unviewable-media
    case in bot_response.py, so every reason a handoff happens gets the same
    tracking and the same attempt at notifying someone, not just the ones
    this feature was originally asked to add. Returns the new escalation's
    id, or the existing open one's id if this phone already has one (no
    duplicate ticket, no duplicate notification)."""
    if trigger not in TRIGGERS:
        trigger = "customer_request"
    if category not in CATEGORIES:
        category = "other"
    if priority not in PRIORITIES:
        priority = "medium"
    if category in _ALWAYS_HIGH_CATEGORIES:
        priority = "high"

    existing_id = await has_open_escalation(phone)
    if existing_id:
        log.info(f"Escalation already open for {phone} (#{existing_id}) -- not creating a duplicate")
        return existing_id

    db = get_db()
    try:
        if not customer_name:
            last_inbound = (
                db.query(Message)
                .filter(Message.phone == phone, Message.direction == "inbound", Message.name.isnot(None))
                .order_by(Message.created_at.desc())
                .first()
            )
            customer_name = last_inbound.name if last_inbound else None
        esc = Escalation(
            phone=phone, customer_name=customer_name, category=category,
            priority=priority, trigger=trigger, status="open", summary=summary,
        )
        db.add(esc)
        db.commit()
        db.refresh(esc)
        escalation_id = esc.id
    finally:
        db.close()

    label = f"Escalation #{escalation_id} ({category})"
    await set_handoff(session_id, phone, label)

    agent, is_owner = await _resolve_notification_target(phone)
    if agent:
        if is_owner:
            _auto_assign_from_owner(escalation_id, agent)
        await _notify_agent(escalation_id, agent, phone, customer_name, category, priority, summary or "")
    else:
        db = get_db()
        try:
            esc = db.query(Escalation).filter(Escalation.id == escalation_id).first()
            if esc:
                esc.notification_status = "skipped"
                esc.notification_error = "No conversation owner and no routing fallback_agent_id configured."
                db.commit()
        finally:
            db.close()
        log.warning(f"Escalation #{escalation_id} for {phone} has no notification target -- visible in-app only")

    log.info(f"Escalation #{escalation_id} created for {phone} (trigger={trigger}, category={category}, priority={priority})")
    return escalation_id


async def mark_first_response_if_needed(phone: str, agent_id: Optional[int] = None) -> None:
    """Called from the agent-reply/agent-send-media endpoints -- stamps the
    open escalation's first_response_at the first time an agent actually
    sends something to this phone, not when they merely open the chat. If
    nobody had claimed the ticket yet (the fallback-notified case, or one
    created before a ConversationOwner existed), the agent who actually
    replied IS the real first responder -- claim it for them rather than
    leaving it "Unassigned" under someone who's now visibly handling it."""
    db = get_db()
    try:
        esc = (
            db.query(Escalation)
            .filter(Escalation.phone == phone, Escalation.status.in_(("open", "assigned", "in_progress")),
                    Escalation.first_response_at.is_(None))
            .order_by(Escalation.created_at.desc())
            .first()
        )
        if esc:
            esc.first_response_at = datetime.utcnow()
            if agent_id and esc.assigned_agent_id is None:
                esc.assigned_agent_id = agent_id
                esc.assigned_at = datetime.utcnow()
            if esc.status in ("open", "assigned"):
                esc.status = "in_progress"
            db.commit()
    except Exception as e:
        log.warning(f"Failed to mark first response for {phone}: {e}")
    finally:
        db.close()


async def check_sla_breaches() -> int:
    """Backstop for an escalation whose owner has gone quiet: any escalation
    still awaiting a first response ESCALATION_SLA_HOURS after it was opened
    gets a second WhatsApp alert sent to the routing fallback agent -- the
    same "who catches things nobody's handling" contact used when an
    escalation has no owner at all, reused here rather than adding a second
    "on-duty" concept. Fires once per escalation (sla_notified_at gates it),
    and never touches assigned_agent_id -- the original owner keeps the
    ticket, this is a second set of eyes, not a reassignment. Returns how
    many alerts were sent, for the worker's log line."""
    from chatbot.services.routing_service import get_routing_config

    cutoff = datetime.utcnow() - timedelta(hours=ESCALATION_SLA_HOURS)
    db = get_db()
    try:
        breaches = (
            db.query(Escalation)
            .filter(
                Escalation.first_response_at.is_(None),
                Escalation.sla_notified_at.is_(None),
                Escalation.status != "resolved",
                Escalation.created_at <= cutoff,
            )
            .all()
        )
        breach_data = [
            (e.id, e.phone, e.customer_name, e.category, e.priority, e.summary)
            for e in breaches
        ]
    finally:
        db.close()

    if not breach_data:
        return 0

    cfg = await get_routing_config()
    fallback_id = cfg.get("fallback_agent_id")
    if not fallback_id:
        log.warning(f"{len(breach_data)} escalation(s) breached SLA but no routing "
                    f"fallback_agent_id is configured -- nobody to notify")
        return 0

    db = get_db()
    try:
        fallback_agent = db.query(Agent).filter(Agent.id == fallback_id).first()
    finally:
        db.close()
    if not fallback_agent or not fallback_agent.phone_number:
        log.warning(f"{len(breach_data)} escalation(s) breached SLA but the configured "
                    f"fallback agent (#{fallback_id}) has no phone number on file")
        return 0

    sent = 0
    for esc_id, phone, customer_name, category, priority, summary in breach_data:
        try:
            await send_whatsapp_template(
                fallback_agent.phone_number, ESCALATION_ALERT_TEMPLATE,
                [customer_name or phone, category.replace("_", " ").title(),
                 priority.upper(), summary or "No summary available."],
            )
        except Exception as e:
            log.warning(f"SLA-breach alert failed for escalation #{esc_id}: {e}")
        # Stamped regardless of send success -- same one-shot-attempt policy
        # as the original notification; a stuck "keep retrying forever" loop
        # helps nobody more than a single visible in-app warning does.
        db = get_db()
        try:
            esc = db.query(Escalation).filter(Escalation.id == esc_id).first()
            if esc:
                esc.sla_notified_at = datetime.utcnow()
                db.commit()
                sent += 1
        finally:
            db.close()
        log.info(f"[escalation-sla] #{esc_id} ({phone}) unanswered {ESCALATION_SLA_HOURS}h+ "
                 f"-- alerted fallback agent {fallback_agent.name}")

    return sent
