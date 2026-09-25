import asyncio
from datetime import datetime, timedelta

from fastapi import BackgroundTasks
from sqlalchemy import and_, func, select

from chatbot.config import HANDOFF_AUTO_RESET_HOURS, log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Message
from chatbot.services.bot_control import is_bot_globally_disabled
from chatbot.services.chat_service import ChatRequest, generate_chat_response
from chatbot.workers.bot_response import deliver_reply


# Upper bound on how far back this looks — without one, every conversation
# that ever went quiet (weeks or months ago, long since closed) would be a
# "candidate" forever, and a phone whose last message happened to be inbound
# ("ok thanks 🙏") would get an unprompted bot reply out of nowhere, on a
# conversation the business has long moved on from. This is for recently
# abandoned conversations, not an archive replay.
MAX_STALENESS_HOURS = 72


async def _candidate_phones() -> list:
    """Every phone whose most recent message of ANY kind falls in the window
    (HANDOFF_AUTO_RESET_HOURS ago, MAX_STALENESS_HOURS ago] — the pool
    _stale_unanswered_name() below then filters down to the ones that are
    actually still unanswered. Deliberately NOT scoped to currently-active
    `koolbuy:handoff:*` Redis keys (see the docstring on run_handoff_watchdog
    for why that was the bug)."""
    now = datetime.utcnow()
    floor = now - timedelta(hours=HANDOFF_AUTO_RESET_HOURS)
    ceiling = now - timedelta(hours=MAX_STALENESS_HOURS)
    db = get_db()
    try:
        rows = db.execute(
            select(Message.phone)
            .group_by(Message.phone)
            .having(and_(func.max(Message.created_at) < floor,
                         func.max(Message.created_at) >= ceiling))
        ).all()
        return [r.phone for r in rows]
    finally:
        db.close()


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
    """Catches conversations abandoned after a customer's message went
    unanswered for HANDOFF_AUTO_RESET_HOURS+ — most commonly a human agent who
    claimed the conversation and then went silent, but not exclusively that.

    Originally this scanned active `koolbuy:handoff:wa_*` Redis keys, mirroring
    the reactive stale-handoff check in webhook.py. That missed real cases: the
    handoff key itself carries its own 24h TTL (see MEDIA_HANDOFF_TTL / the
    toggle_handoff endpoint) and expires on its own well before anyone reviews
    it — once it's gone, that scan can never find the conversation again, even
    though the customer is exactly as unanswered as before. Confirmed live: a
    customer's "600L" sat unanswered for 40+ hours, spanning right across the
    key's own expiry, and the very first version of this watchdog never once
    saw it. This version instead asks Postgres directly for every phone whose
    latest message (any direction) is older than the threshold — independent
    of whether a handoff key currently exists, already expired, or was never
    set at all (e.g. the bot itself silently failed to reply the first time).
    Still doesn't send anything unless the conversation's actual last message
    is inbound (see _stale_unanswered_name) — a customer who was already
    answered and simply hasn't replied since is left alone."""
    if not redis_client.client:
        return
    if await is_bot_globally_disabled():
        return  # a super admin paused the bot everywhere -- this isn't its call to override
    handled = 0
    for phone in await _candidate_phones():
        session_id = f"wa_{phone}"
        try:
            name = await _stale_unanswered_name(phone)
            if name is None:
                continue

            handoff_key = f"koolbuy:handoff:{session_id}"
            await redis_client.client.delete(handoff_key)
            log.info(f"[handoff-watchdog] resuming abandoned conversation for {session_id} "
                     f"(no reply in {HANDOFF_AUTO_RESET_HOURS}h, customer still waiting)")

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

            # Redis history doesn't carry WhatsApp's quoted-reply relationship,
            # only Postgres does — look it up directly so a customer who quoted
            # a specific product photo still gets resolved correctly here too,
            # not just on the immediate/non-watchdog reply path.
            db = get_db()
            try:
                last_msg = db.execute(
                    select(Message).where(Message.phone == phone)
                    .order_by(Message.created_at.desc()).limit(1)
                ).scalars().first()
                reply_to_wamid = last_msg.reply_to_wamid if last_msg else None
            finally:
                db.close()

            bg = BackgroundTasks()
            chat_req = ChatRequest(session_id=session_id, message=pending_text, user_name=name,
                                    reply_to_wamid=reply_to_wamid)
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
