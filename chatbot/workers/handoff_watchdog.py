import asyncio
from datetime import datetime

from fastapi import BackgroundTasks
from sqlalchemy import and_, func, select

from chatbot.config import HANDOFF_AUTO_RESET_HOURS, log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Message
from chatbot.services.chat_service import ChatRequest, generate_chat_response
from chatbot.workers.bot_response import deliver_reply


async def _stale_unanswered_name(phone: str):
    """None unless this handoff is both stale (no agent reply in
    HANDOFF_AUTO_RESET_HOURS) AND the conversation's last message is still
    inbound — i.e. the customer said something and is genuinely waiting,
    not just quiet after already being answered. Returns the customer's
    name (for a personalised reply) when both hold."""
    db = get_db()
    try:
        last_out = db.execute(
            select(func.max(Message.created_at))
            .where(and_(Message.phone == phone, Message.direction == "outbound"))
        ).scalar()
        if last_out is not None and \
           (datetime.utcnow() - last_out).total_seconds() <= HANDOFF_AUTO_RESET_HOURS * 3600:
            return None  # agent is still within the window

        last_msg = db.execute(
            select(Message).where(Message.phone == phone)
            .order_by(Message.created_at.desc()).limit(1)
        ).scalars().first()
        if not last_msg or last_msg.direction != "inbound":
            return None  # nothing pending — either silent both ways, or agent already replied
        return last_msg.name or "Customer"
    finally:
        db.close()


async def run_handoff_watchdog():
    """Catches handoff conversations a human agent claimed and then abandoned.

    The reactive stale-handoff check in webhook.py (search HANDOFF_AUTO_RESET_HOURS
    there) only re-evaluates staleness when the CUSTOMER sends another message —
    a real bot response then, since the whole point is to resume the normal flow
    for whatever they just said. If they don't send anything further (they already
    asked something and are simply waiting for a reply that never comes), that
    check never runs again and the conversation sits stuck in handoff forever,
    bot silenced, with nobody watching it. This scans active handoffs on a timer
    instead of waiting on a customer message that may never arrive."""
    if not redis_client.client:
        return
    handled = 0
    async for key in redis_client.client.scan_iter(match="koolbuy:handoff:wa_*"):
        session_id = key.split(":", 2)[-1]
        phone = session_id[len("wa_"):]
        try:
            name = await _stale_unanswered_name(phone)
            if name is None:
                continue

            await redis_client.client.delete(key)
            log.info(f"[handoff-watchdog] auto-reset stale handoff for {session_id} "
                     f"(no agent reply in {HANDOFF_AUTO_RESET_HOURS}h, customer still waiting)")

            # The unanswered message is almost certainly the last entry in Redis
            # history too (webhook.py appends every inbound message to history
            # even while in handoff, just without generating a reply for it) —
            # pop it back off before regenerating, since generate_chat_response()
            # always re-appends whatever it's given as the current turn; leaving
            # the original entry in place would duplicate it in history.
            history = await redis_client.get_history(session_id)
            if history and history[-1].get("role") == "user":
                pending_text = history[-1]["content"]
                await redis_client.save_history(session_id, history[:-1])
            else:
                # Redis history didn't have it (TTL'd out, Redis restart, etc.) —
                # fall back to Postgres so there's still something to reply to.
                db = get_db()
                try:
                    last_msg = db.execute(
                        select(Message).where(Message.phone == phone)
                        .order_by(Message.created_at.desc()).limit(1)
                    ).scalars().first()
                    pending_text = last_msg.content if last_msg else None
                finally:
                    db.close()
            if not pending_text:
                continue

            bg = BackgroundTasks()
            chat_req = ChatRequest(session_id=session_id, message=pending_text, user_name=name)
            chat_resp, persist = await generate_chat_response(chat_req, bg)
            await deliver_reply(session_id, phone, chat_resp, persist, bg)
            handled += 1
        except Exception as e:
            log.error(f"[handoff-watchdog] failed for {session_id}: {e}")

    if handled:
        log.info(f"[handoff-watchdog] resumed {handled} abandoned handoff conversation(s)")


async def handoff_watchdog_worker():
    """Periodic background task pairing the reactive stale-handoff check in
    webhook.py — catches the case that one can't: an agent went silent and the
    customer never sent a follow-up message to trigger it."""
    log.info(f"Handoff watchdog started (reset_hours={HANDOFF_AUTO_RESET_HOURS})")
    while True:
        try:
            await asyncio.sleep(1800)
            await run_handoff_watchdog()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Handoff watchdog error: {e}")
