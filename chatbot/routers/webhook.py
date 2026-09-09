import asyncio
import json
import re
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import and_, func, select

from chatbot.config import COMPLETE_SESSION_RESET_HOURS, HANDOFF_AUTO_RESET_HOURS, LEAD_TTL, WHATSAPP_VERIFY_TOKEN, log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import BroadcastRecipient, Message
from chatbot.services.lead_service import save_lead
from chatbot.services.routing_service import auto_assign_conversation
from chatbot.services.whatsapp_service import mark_whatsapp_read, save_message_db
from chatbot.utils.phone import extract_valid_phone, normalize_phone
from chatbot.workers.bot_response import delayed_bot_response

router = APIRouter(tags=["webhook"])

# A bare "okay"/"thanks" after a completed conversation is just an acknowledgment,
# not a new inquiry — don't wipe the session and force the script back to Step 1 for it.
FILLER_ACK_RE = re.compile(
    r'^\s*(ok(ay)?|alright(y)?|cool|nice|great|good|fine|sure|noted|got ?it|'
    r'thanks?( you)?|thank ?u|tanks?|👍+|🙏+|❤️+|😊+)\s*[!.]*\s*$',
    re.IGNORECASE,
)


def _update_message_delivery(wamid: str, status: str, error_detail: str = None, event_ts: str = None):
    """Update delivery_status on a broadcast recipient and/or the matching Message
    row when Meta fires a status event. A wamid may match either, both, or neither
    (regular agent/bot messages only ever have a Message row; broadcast sends have
    both). Without this, the admin conversation view always shows "sent" forever,
    even when Meta later fails to actually deliver the message to the customer."""
    db = get_db()
    try:
        rank = {"sent": 0, "delivered": 1, "read": 2, "failed": -1}
        event_dt = None
        if event_ts:
            try:
                event_dt = datetime.utcfromtimestamp(int(event_ts))
            except (TypeError, ValueError):
                event_dt = None

        recipient = db.query(BroadcastRecipient).filter(BroadcastRecipient.wamid == wamid).first()
        if recipient:
            if rank.get(status, 0) > rank.get(recipient.delivery_status, 0) or status == "failed":
                recipient.delivery_status = status

        message = db.query(Message).filter(Message.wamid == wamid).first()
        if message:
            if rank.get(status, 0) > rank.get(message.delivery_status or "sent", 0) or status == "failed":
                message.delivery_status = status
                if status == "failed":
                    message.delivery_error = error_detail
            # First-write-wins, independent of the rank-gated status field above —
            # a later out-of-order "delivered" webhook shouldn't overwrite an
            # earlier-recorded timestamp for the same transition.
            ts = event_dt or datetime.utcnow()
            if status == "delivered" and message.delivered_at is None:
                message.delivered_at = ts
            elif status == "read" and message.read_at is None:
                message.read_at = ts

        if status == "failed" and error_detail:
            log.warning(f"WhatsApp delivery failed for wamid {wamid}: {error_detail}")

        db.commit()
    except Exception as e:
        log.warning(f"Delivery status update failed for wamid {wamid}: {e}")
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
                        error_detail = None
                        if status_type == "failed":
                            errors = status_evt.get("errors", [])
                            if errors:
                                error_detail = errors[0].get("title") or errors[0].get("message")
                        background_tasks.add_task(_update_message_delivery, wamid, status_type, error_detail, status_evt.get("timestamp"))

                messages = value.get("messages", [])
                for msg in messages:
                    msg_type = msg.get("type")
                    escalate = False       # image/video/document: needs a human, not text-describable
                    audio_media_id = None  # audio: resolved to a real transcript later, not acknowledged blind
                    caption = ""

                    if msg_type == "text":
                        text = msg["text"]["body"]
                        llm_text = text
                    elif msg_type in ("image", "video", "document", "audio", "sticker"):
                        # Previously silently skipped — no reply, no acknowledgment, no
                        # record at all. `text` is the real marker saved to the DB (the
                        # admin transcript already renders [image]/[video]/[audio]/
                        # [document] tags); `llm_text` drives what the bot actually does.
                        media = msg.get(msg_type) or {}
                        caption = (media.get("caption") or "").strip()
                        media_id = media.get("id")
                        filename = media.get("filename")  # documents only
                        article = "an" if msg_type in ("image", "audio") else "a"

                        if media_id and msg_type in ("image", "sticker"):
                            text = f"[image]/admin/media-proxy/{media_id}[/image]"
                        elif media_id and msg_type == "video":
                            text = f"[video]/admin/media-proxy/{media_id}[/video]"
                        elif media_id and msg_type == "audio":
                            text = f"[audio]/admin/media-proxy/{media_id}[/audio]"
                        elif media_id and msg_type == "document":
                            label = filename or "document"
                            url = f"/admin/media-proxy/{media_id}" + (f"?filename={quote(filename)}" if filename else "")
                            text = f"[document]{url}|{label}[/document]"
                        else:
                            text = f"[Customer sent {article} {msg_type}]"

                        # What the bot actually does differs by type:
                        # - audio: genuinely transcribed (Whisper) and treated like typed
                        #   text once resolved — see delayed_bot_response
                        # - image / video / document: real content the bot can't see and
                        #   that often needs a human call (a product photo, a receipt, a
                        #   spec sheet) — escalate to an agent rather than pretend it's
                        #   fine, or ask the customer to "describe it in words," which
                        #   just reads as broken
                        # - sticker: low-stakes chit-chat, not worth pulling in a human
                        #   for — acknowledge briefly and keep going (READ THE ROOM)
                        if msg_type == "audio":
                            audio_media_id = media_id
                            llm_text = None  # filled in with the transcript once resolved
                        elif msg_type in ("image", "video", "document"):
                            escalate = True
                            llm_text = (
                                f"[Customer sent {article} {msg_type}"
                                + (f' with caption "{caption}"' if caption else "")
                                + f". You cannot view {msg_type}s. Let them know warmly, in "
                                f"one short clause, that you're looping in a teammate to "
                                f"take a look and follow up shortly — do NOT say you "
                                f"personally can't view it, just frame it as bringing in a "
                                f"specialist. Do not ask any further questions this turn.]"
                            )
                        else:  # sticker
                            llm_text = ("[Customer sent a sticker. Acknowledge briefly and "
                                        "warmly, then continue the conversation naturally.]")
                    else:
                        continue  # location, contacts, reactions, interactive replies, etc. — out of scope for now
                    wa_from = normalize_phone(msg["from"])
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
                    # A caption gets its own row, same convention already used for
                    # outbound product sends — the media marker stays a pure media tag,
                    # caption text is a separate readable line in the transcript.
                    if msg_type in ("image", "video", "document") and caption:
                        background_tasks.add_task(save_message_db, session_id, wa_from, name, "inbound", caption)

                    # Round-robin a brand-new conversation to an agent — no-ops
                    # once the phone already has an owner, so this only ever
                    # fires on that contact's first-ever inbound message.
                    background_tasks.add_task(auto_assign_conversation, wa_from)

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

                    # Check session state and reset completed sessions — but only once
                    # they've gone idle, not on the very next message. A customer who
                    # keeps chatting right after finishing (a clarification, a new
                    # objection, another question) is still mid-conversation, not
                    # starting a new inquiry — resetting on the immediate next message
                    # wiped their captured phone/delivery and re-greeted them from
                    # scratch, which is exactly what this staleness check prevents.
                    if redis_client.client:
                        history_key = f"koolbuy:chat:{session_id}"
                        raw_history = await redis_client.client.get(history_key)
                        history_text = raw_history if raw_history else ""
                        is_complete = "[VALID phone captured" in history_text and "[DELIVERY confirmed" in history_text

                        if is_complete and FILLER_ACK_RE.match(text):
                            log.info(f"Session {session_id} complete but '{text}' looks like a filler "
                                     f"acknowledgment — not restarting the script")
                        elif is_complete:
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
                                   (datetime.utcnow() - last_out).total_seconds() > COMPLETE_SESSION_RESET_HOURS * 3600:
                                    stale = True
                            except Exception as _e:
                                log.warning(f"Complete-session stale-check failed: {_e}")
                            if stale:
                                await redis_client.client.delete(history_key)
                                await redis_client.client.delete(f"koolbuy:phone:{session_id}")
                                await redis_client.client.delete(f"koolbuy:delivery:{session_id}")
                                log.info(f"Session {session_id} reset for new conversation "
                                         f"(idle {COMPLETE_SESSION_RESET_HOURS}h+ since last reply)")
                            else:
                                log.info(f"Session {session_id} complete but recent — letting the "
                                         f"conversation continue instead of resetting it")

                    # Debounce: record this message in a per-session pending buffer so
                    # that if another message arrives while we're waiting to reply, only
                    # the task scheduled by the LAST one actually generates a reply,
                    # combining everything sent during the burst into one turn. Without
                    # this, two quick messages ("I have nepa" / "But not good enough")
                    # each independently spawned their own reply, producing two
                    # overlapping bot messages back to back — confirmed happening in
                    # production. `my_seq` is minted here (not inside the delayed task)
                    # so its value reflects the exact moment this message was scheduled,
                    # not whatever the counter happens to read once the task finally runs.
                    # Each buffer entry carries its own escalate/audio flag (not just
                    # text) so a burst mixing message types — a photo then a follow-up
                    # question, say — still escalates and still answers, whichever
                    # task ends up combining and replying to the whole thing.
                    pending_entry = json.dumps({
                        "text": llm_text,
                        "escalate": escalate,
                        "audio_media_id": audio_media_id,
                    })
                    my_seq = None
                    if redis_client.client:
                        try:
                            seq_key = f"koolbuy:pending_seq:{session_id}"
                            msgs_key = f"koolbuy:pending_msgs:{session_id}"
                            my_seq = str(await redis_client.client.incr(seq_key))
                            await redis_client.client.expire(seq_key, 300)
                            await redis_client.client.rpush(msgs_key, pending_entry)
                            await redis_client.client.expire(msgs_key, 300)
                        except Exception as _e:
                            log.warning(f"Debounce buffer write failed for {session_id}: {_e}")
                            my_seq = None

                    # Fire delayed response — gives agents BOT_RESPONSE_DELAY seconds to take over.
                    # The `or` fallback only matters if Redis is unavailable (my_seq stays
                    # None): audio's llm_text is None until resolved via the pending buffer,
                    # which needs Redis — without it there's no combining, so this is what
                    # the reply falls back to instead of crashing on a None message.
                    asyncio.create_task(delayed_bot_response(
                        session_id, wa_from, name,
                        llm_text or "[Customer sent a voice note. Acknowledge warmly and ask them to type their message.]",
                        my_seq,
                    ))
    except Exception as e:
        log.error(f"WhatsApp webhook processing error: {e}")
    return Response(content="OK", status_code=200)
