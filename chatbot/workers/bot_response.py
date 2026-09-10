import asyncio
import json

from fastapi import BackgroundTasks
from fastapi.concurrency import run_in_threadpool

from chatbot.config import BOT_NAME, BOT_RESPONSE_DELAY, log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import HandoffEvent
from chatbot.services.chat_service import ChatRequest, chat_handler
from chatbot.services.whatsapp_service import save_message_db, send_whatsapp_message, transcribe_whatsapp_audio

# Matches the 24h TTL an agent-initiated takeover already uses
# (conversations.py's toggle_handoff) — same mechanism, different trigger.
MEDIA_HANDOFF_TTL = 86400
MEDIA_HANDOFF_LABEL = "Awaiting agent (media)"


def _parse_pending_entry(raw: str) -> dict:
    """Pending-buffer entries are JSON ({"text", "escalate", "audio_media_id"}).
    Falls back to treating `raw` as plain text for entries written by an older
    version of this code — buffers can briefly outlive a deploy."""
    try:
        entry = json.loads(raw)
        if isinstance(entry, dict):
            return entry
    except (json.JSONDecodeError, TypeError):
        pass
    return {"text": raw, "escalate": False, "audio_media_id": None}


async def _set_media_handoff(session_id: str, wa_from: str):
    """Hand a conversation to a human because the customer sent something the
    bot can't meaningfully respond to (an image, video, or document) — reuses
    the exact mechanism a manual agent takeover uses, just system-triggered,
    so it shows up the same way in the admin dashboard and auto-resets the
    same way (HANDOFF_AUTO_RESET_HOURS) if nobody picks it up."""
    if redis_client.client:
        try:
            await redis_client.client.set(
                f"koolbuy:handoff:{session_id}", MEDIA_HANDOFF_LABEL, ex=MEDIA_HANDOFF_TTL
            )
        except Exception as e:
            log.warning(f"[delay] failed to set media handoff for {session_id}: {e}")

    def _log():
        db = get_db()
        try:
            db.add(HandoffEvent(phone=wa_from, agent_name=MEDIA_HANDOFF_LABEL, event_type="takeover"))
            db.commit()
        except Exception as e:
            log.warning(f"[delay] failed to log media handoff event for {wa_from}: {e}")
        finally:
            db.close()

    await run_in_threadpool(_log)
    log.info(f"[delay] {session_id} escalated to a human agent (unviewable media)")


async def delayed_bot_response(session_id: str, wa_from: str, name: str, text: str, my_seq: str = None):
    """Wait for the agent takeover window, then respond if no agent claimed the session.

    `my_seq` is this message's debounce ticket (see webhook.py). If a newer message
    arrived for this session while we were asleep, the pending-seq counter will have
    moved past `my_seq` — that means a later task now owns replying to this whole
    burst (it will pick up everything from the pending buffer, this message
    included), so we bail out silently instead of sending a second, overlapping
    reply. `my_seq is None` means debounce wasn't available (e.g. Redis was down
    when this was scheduled) — in that case we just reply to `text` as before."""
    if BOT_RESPONSE_DELAY > 0:
        await asyncio.sleep(BOT_RESPONSE_DELAY)

    if redis_client.client and my_seq is not None:
        try:
            current_seq = await redis_client.client.get(f"koolbuy:pending_seq:{session_id}")
            if current_seq is not None and current_seq != my_seq:
                log.info(f"[delay] {session_id} superseded by a newer message — skipping, "
                         f"the latest task will reply to the whole burst")
                return
        except Exception as e:
            log.warning(f"[delay] pending-seq check failed for {session_id}: {e}")

    # Re-check handoff — agent (or a prior media escalation) may have taken over during the delay
    handoff_key = f"koolbuy:handoff:{session_id}"
    in_handoff = await redis_client.client.get(handoff_key) if redis_client.client else None
    if in_handoff:
        log.info(f"[delay] {session_id} claimed by agent during window — bot silent")
        return

    # We're the winning task — fold every message received during this burst
    # (this one included) into a single combined turn instead of replying to
    # just the last one and silently dropping earlier ones from the same burst.
    # Audio entries get resolved to a real transcript right here — exactly once,
    # by whichever task actually ends up replying, so a voice note that gets
    # superseded before anyone replies to it never costs a wasted transcription call.
    escalate = False
    if redis_client.client and my_seq is not None:
        try:
            msgs_key = f"koolbuy:pending_msgs:{session_id}"
            pending = await redis_client.client.lrange(msgs_key, 0, -1)
            if pending:
                parts = []
                for raw in pending:
                    entry = _parse_pending_entry(raw)
                    if entry.get("escalate"):
                        escalate = True
                    audio_id = entry.get("audio_media_id")
                    if audio_id:
                        transcript = await transcribe_whatsapp_audio(audio_id)
                        if transcript:
                            parts.append(f'[Voice note transcript: "{transcript}"]')
                            # Its own row so an agent can read what was said without
                            # having to press play — same convention as image captions.
                            save_message_db(session_id, wa_from, name, "inbound", f'🎤 "{transcript}"')
                        else:
                            parts.append("[Customer sent a voice note that couldn't be "
                                          "transcribed. Acknowledge warmly and ask them to "
                                          "type their message instead.]")
                    elif entry.get("text"):
                        parts.append(entry["text"])
                if parts:
                    text = "\n".join(parts)

            # Re-check right before committing to reply: transcription above
            # can take real time, during which a customer's follow-up may
            # have already arrived and started its own task. If so, leave
            # pending_msgs UNCLEARED — that task will read this same buffer
            # (this message included) and combine everything correctly — and
            # bail out here rather than generate a reply to a burst that's
            # already stale. Known remaining gap: a message arriving DURING
            # the chat_handler call below (the LLM call itself, typically
            # 1-3s) isn't caught by any check — chat_handler saves
            # conversation history internally as part of generating a reply,
            # so discarding post-call would leave a reply in history that
            # was never actually sent to the customer, corrupting later
            # turns' context. Closing that gap needs chat_handler's generate
            # and persist steps split apart, which is a real refactor, not a
            # one-line fix.
            current_seq = await redis_client.client.get(f"koolbuy:pending_seq:{session_id}")
            if current_seq is not None and current_seq != my_seq:
                log.info(f"[delay] {session_id} superseded while preparing the reply — skipping")
                return

            await redis_client.client.delete(msgs_key)
        except Exception as e:
            log.warning(f"[delay] pending-msgs read failed for {session_id}: {e}")

    # Generate bot response
    bg = BackgroundTasks()
    chat_req = ChatRequest(session_id=session_id, message=text, user_name=name)
    try:
        chat_resp = await chat_handler(chat_req, bg)
    except Exception as e:
        log.error(f"[delay] chat_handler failed for {session_id}: {e}")
        # Without this, the customer sees total silence — no error, no retry
        # prompt, nothing — whenever the AI backend has a hiccup.
        try:
            fallback_text = "Sorry, I had trouble processing that — could you try sending your message again?"
            wamid = await send_whatsapp_message(wa_from, fallback_text)
            save_message_db(session_id, wa_from, BOT_NAME, "outbound", fallback_text, wamid=wamid)
        except Exception as e2:
            log.error(f"[delay] fallback message also failed for {session_id}: {e2}")
        if escalate:
            await _set_media_handoff(session_id, wa_from)
        return

    def _blurb(p):
        return f"🛒 *{p.name}*\n💰 N{float(p.price):,.0f}"

    if chat_resp.products:
        first = chat_resp.products[0]
        wamid = await send_whatsapp_message(
            wa_from, chat_resp.response,
            first.original_image_url,  # raw S3/CDN URL — WhatsApp fetches directly
            image_caption=_blurb(first),
        )
        save_message_db(session_id, wa_from, BOT_NAME, "outbound", chat_resp.response, wamid=wamid)
        # One [image]...[/image] row per product actually sent to WhatsApp —
        # without this, bot-sent pictures were invisible in the admin
        # transcript (only the agent-sent-image path recorded that marker),
        # making it look like the bot never sends pictures at all even
        # though delivery to the customer was working the whole time.
        if first.original_image_url:
            save_message_db(session_id, wa_from, BOT_NAME, "outbound", f"[image]{first.original_image_url}[/image]")

        # Every recommended product gets its own image + caption, not just the first —
        # a customer comparing several options should see all of them, not just one.
        for product in chat_resp.products[1:]:
            await send_whatsapp_message(
                wa_from, "", product.original_image_url, image_caption=_blurb(product)
            )
            if product.original_image_url:
                save_message_db(session_id, wa_from, BOT_NAME, "outbound", f"[image]{product.original_image_url}[/image]")
    else:
        wamid = await send_whatsapp_message(wa_from, chat_resp.response)
        save_message_db(session_id, wa_from, BOT_NAME, "outbound", chat_resp.response, wamid=wamid)

    # Run background tasks queued by chat_handler (save_lead, update_lead_address, etc.)
    for task in bg.tasks:
        try:
            if asyncio.iscoroutinefunction(task.func):
                await task.func(*task.args, **task.kwargs)
            else:
                task.func(*task.args, **task.kwargs)
        except Exception as e:
            log.warning(f"[delay] bg task {task.func.__name__} failed: {e}")

    # The reply just sent was the escalation announcement — hand off to a human
    # now, after it's out, so this doesn't silence the announcement itself.
    if escalate:
        await _set_media_handoff(session_id, wa_from)
