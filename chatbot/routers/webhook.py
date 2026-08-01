import asyncio
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import and_, func, select

from chatbot.config import HANDOFF_AUTO_RESET_HOURS, LEAD_TTL, WHATSAPP_VERIFY_TOKEN, log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import BroadcastRecipient, Message
from chatbot.services.lead_service import save_lead
from chatbot.services.whatsapp_service import mark_whatsapp_read, save_message_db
from chatbot.utils.phone import extract_valid_phone, normalize_phone
from chatbot.workers.bot_response import delayed_bot_response

router = APIRouter(tags=["webhook"])


def _update_broadcast_delivery(wamid: str, status: str):
    """Update delivery_status on a broadcast recipient when Meta fires a status event."""
    db = get_db()
    try:
        recipient = db.query(BroadcastRecipient).filter(BroadcastRecipient.wamid == wamid).first()
        if recipient:
            # Only upgrade status rank; never downgrade (read > delivered > sent)
            rank = {"sent": 0, "delivered": 1, "read": 2, "failed": -1}
            if rank.get(status, 0) > rank.get(recipient.delivery_status, 0) or status == "failed":
                recipient.delivery_status = status
                db.commit()
    except Exception as e:
        log.warning(f"Broadcast delivery update failed for wamid {wamid}: {e}")
    finally:
        db.close()


def _mark_broadcast_responded(phone: str):
    """Mark the most recent broadcast recipient for this phone as responded."""
    db = get_db()
    try:
        recipient = (
            db.query(BroadcastRecipient)
            .filter(BroadcastRecipient.phone == phone, BroadcastRecipient.responded == False)
            .order_by(BroadcastRecipient.created_at.desc())
            .first()
        )
        if recipient:
            recipient.responded = True
            db.commit()
    except Exception as e:
        log.warning(f"Broadcast responded update failed for phone {phone}: {e}")
    finally:
        db.close()


@router.get("/webhook")
async def whatsapp_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        log.info("WhatsApp webhook verified successfully")
        return Response(content=hub_challenge, media_type="text/plain")
    log.warning("WhatsApp webhook verification failed")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    log.info(f"WhatsApp webhook received: {payload}")
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # Handle delivery status events (delivered / read / failed)
                for status_evt in value.get("statuses", []):
                    wamid = status_evt.get("id")
                    status_type = status_evt.get("status")
                    if wamid and status_type in ("delivered", "read", "failed"):
                        background_tasks.add_task(_update_broadcast_delivery, wamid, status_type)

                messages = value.get("messages", [])
                for msg in messages:
                    if msg.get("type") != "text":
                        continue
                    wa_from = normalize_phone(msg["from"])
                    text = msg["text"]["body"]
                    contacts = value.get("contacts", [{}])
                    name = contacts[0].get("profile", {}).get("name", "Customer") if contacts else "Customer"
                    session_id = f"wa_{wa_from}"
                    msg_id = msg.get("id")
                    log.info(f"WhatsApp message from {wa_from} ({name}): {text}")

                    # Mark message as read immediately
                    if msg_id:
                        background_tasks.add_task(mark_whatsapp_read, msg_id)

                    # Save inbound message to DB
                    background_tasks.add_task(save_message_db, session_id, wa_from, name, "inbound", text)

                    # Mark broadcast campaign as responded if this phone was a recipient
                    background_tasks.add_task(_mark_broadcast_responded, wa_from)

                    # Check if agent has taken over this session
                    handoff_key = f"koolbuy:handoff:{session_id}"
                    in_handoff = await redis_client.client.get(handoff_key) if redis_client.client else None
                    if in_handoff:
                        # Auto-reset stale handoffs — if no agent has replied in 8+ hours,
                        # the conversation was abandoned. Let the bot resume.
                        stale = False
                        try:
                            _db = get_db()
                            last_out = _db.execute(
                                select(func.max(Message.created_at))
                                .where(and_(Message.phone == wa_from,
                                            Message.direction == "outbound"))
                            ).scalar()
                            _db.close()
                            if last_out is None or \
                               (datetime.utcnow() - last_out).total_seconds() > HANDOFF_AUTO_RESET_HOURS * 3600:
                                stale = True
                        except Exception as _e:
                            log.warning(f"Handoff stale-check failed: {_e}")
                        if stale:
                            await redis_client.client.delete(handoff_key)
                            in_handoff = None
                            log.info(f"Auto-reset stale handoff for {session_id} (no agent reply in {HANDOFF_AUTO_RESET_HOURS}h)")
                        else:
                            log.info(f"Session {session_id} is in handoff mode — bot silent")
                            history = await redis_client.get_history(session_id)
                            history.append({"role": "user", "content": text,
                                            "ts": datetime.now().isoformat()})
                            await redis_client.save_history(session_id, history)

                            # Bot stays silent, but a lead is still a lead — capture
                            # a phone number even when a human agent is doing the
                            # asking, instead of only ever qualifying via the bot.
                            try:
                                phone_redis = await redis_client.client.get(
                                    f"koolbuy:phone:{session_id}") if redis_client.client else None
                                already_captured = bool(phone_redis) or any(
                                    "[VALID phone captured" in m.get("content", "") for m in history
                                )
                                if not already_captured:
                                    phone = extract_valid_phone(text)
                                    if phone:
                                        background_tasks.add_task(save_lead, name, phone, history, session_id)
                                        if redis_client.client:
                                            await redis_client.client.set(
                                                f"koolbuy:phone:{session_id}", phone, ex=LEAD_TTL)
                            except Exception as _e:
                                log.warning(f"Handoff lead-capture check failed: {_e}")

                            continue

                    # Check session state and reset completed sessions
                    if redis_client.client:
                        history_key = f"koolbuy:chat:{session_id}"
                        raw_history = await redis_client.client.get(history_key)
                        history_text = raw_history if raw_history else ""
                        is_complete = "[VALID phone captured" in history_text and "[DELIVERY confirmed" in history_text

                        if is_complete:
                            await redis_client.client.delete(history_key)
                            await redis_client.client.delete(f"koolbuy:phone:{session_id}")
                            await redis_client.client.delete(f"koolbuy:delivery:{session_id}")
                            log.info(f"Session {session_id} reset for new conversation")

                    # Fire delayed response — gives agents BOT_RESPONSE_DELAY seconds to take over
                    asyncio.create_task(delayed_bot_response(session_id, wa_from, name, text))
    except Exception as e:
        log.error(f"WhatsApp webhook processing error: {e}")
    return Response(content="OK", status_code=200)
