import asyncio
import json

from fastapi import BackgroundTasks

from chatbot.config import BOT_NAME, BOT_RESPONSE_DELAY, log
from chatbot.core import redis_client
from chatbot.services.chat_service import ChatRequest, generate_chat_response
from chatbot.services.escalation_service import create_escalation
from chatbot.services.whatsapp_service import save_message_db, send_whatsapp_message, transcribe_whatsapp_audio


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


async def delayed_bot_response(session_id: str, wa_from: str, name: str, text: str, my_seq: str = None,
                                reply_to_wamid: str = None):
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
    msgs_key = f"koolbuy:pending_msgs:{session_id}"
    if redis_client.client and my_seq is not None:
        try:
            pending = await redis_client.client.lrange(msgs_key, 0, -1)
            if pending:
                parts = []
                for raw in pending:
                    entry = _parse_pending_entry(raw)
                    if entry.get("escalate"):
                        escalate = True
                    # Last one in the burst wins if more than one message in this
                    # combined turn quoted something — the most recent quote is
                    # the one still relevant by the time we're replying to all of them.
                    if entry.get("reply_to_wamid"):
                        reply_to_wamid = entry["reply_to_wamid"]
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

            # Re-check right before generating: transcription above can take
            # real time, during which a customer's follow-up may have already
            # arrived and started its own task. pending_msgs is deliberately
            # NOT cleared yet (see below) — if superseded, that other task
            # reads this same buffer (this message included) and combines
            # everything correctly, so just bail out here.
            current_seq = await redis_client.client.get(f"koolbuy:pending_seq:{session_id}")
            if current_seq is not None and current_seq != my_seq:
                log.info(f"[delay] {session_id} superseded while preparing the reply — skipping")
                return
        except Exception as e:
            log.warning(f"[delay] pending-msgs read failed for {session_id}: {e}")

    # Generate bot response — NOT yet committed to history or sent. The LLM
    # call inside is the single slowest step here (often 1-3s+), and a
    # customer's follow-up can easily land during it; generate_chat_response()
    # hands back a reply plus a separate persist() step specifically so this
    # worker can re-check for supersession one more time before either of
    # those happens, instead of a stale reply getting saved to history as if
    # it had actually been sent.
    bg = BackgroundTasks()
    chat_req = ChatRequest(session_id=session_id, message=text, user_name=name, reply_to_wamid=reply_to_wamid)
    try:
        chat_resp, persist = await generate_chat_response(chat_req, bg)
    except Exception as e:
        log.error(f"[delay] reply generation failed for {session_id}: {e}")
        # Without this, the customer sees total silence — no error, no retry
        # prompt, nothing — whenever the AI backend has a hiccup.
        try:
            fallback_text = "Sorry, I had trouble processing that — could you try sending your message again?"
            wamid, _ = await send_whatsapp_message(wa_from, fallback_text)
            save_message_db(session_id, wa_from, BOT_NAME, "outbound", fallback_text, wamid=wamid)
        except Exception as e2:
            log.error(f"[delay] fallback message also failed for {session_id}: {e2}")
        if escalate:
            await create_escalation(
                session_id=session_id, phone=wa_from, customer_name=name,
                trigger="unviewable_media", category="general_enquiry", priority="medium",
                summary="Customer sent a photo, video, or document the bot can't view.",
            )
        return

    # This is the check that closes the gap the pre-generation checks above
    # can't: a customer's follow-up arriving WHILE the LLM was generating.
    # Still superseded? Discard everything — don't persist (the reply never
    # gets written into history as if it had been sent) and don't clear
    # pending_msgs (the newer task reads this message from that same buffer
    # and combines it with its own, replying to both at once).
    if redis_client.client and my_seq is not None:
        try:
            current_seq = await redis_client.client.get(f"koolbuy:pending_seq:{session_id}")
            if current_seq is not None and current_seq != my_seq:
                log.info(f"[delay] {session_id} superseded during generation — discarding reply, "
                         f"not persisting or sending")
                return
        except Exception as e:
            log.warning(f"[delay] post-generation pending-seq check failed for {session_id}: {e}")

    # Past this point we're committed: clear the buffer (this task now owns
    # replying to everything in it) and commit the reply to history before
    # anything gets sent, so a crash mid-send can't leave history out of sync
    # with what the customer actually sees.
    if redis_client.client and my_seq is not None:
        try:
            await redis_client.client.delete(msgs_key)
        except Exception as e:
            log.warning(f"[delay] pending-msgs clear failed for {session_id}: {e}")
    await deliver_reply(session_id, wa_from, chat_resp, persist, bg, escalate)


def _blurb(p):
    """The caption WhatsApp attaches directly under a product photo -- this
    IS the "image card" the website shows, just delivered through WhatsApp's
    native image+caption bubble instead of a separate HTML card. Previously
    only carried name + price, so the actual description lived in a second,
    disconnected text bubble instead of under the photo it described."""
    lines = [f"🛒 *{p.name}*", f"💰 N{float(p.price):,.0f}"]
    if p.description:
        desc = p.description.strip()
        # WhatsApp's image caption cap is 1024 characters total, INCLUDING the
        # name/price lines above -- 850 leaves headroom even for the longest
        # product names in the catalogue (several run 80-90+ characters).
        # Note: as of writing, every description actually stored is already
        # <=500 characters (truncated further upstream, before it ever
        # reaches this database) -- this cap is a safety net for whenever
        # fuller descriptions get entered, not what's currently cutting
        # anything short.
        if len(desc) > 850:
            cut = desc[:850]
            # Prefer ending on a full sentence over an abrupt mid-word cut --
            # look for the last sentence-ending punctuation in the back part
            # of the window before falling back to a clean word break.
            last_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
            if last_end > 850 * 0.6:
                desc = cut[:last_end + 1]
            else:
                desc = cut.rsplit(" ", 1)[0] + "…"
        lines.append(desc)
    return "\n".join(lines)


async def deliver_reply(session_id: str, wa_from: str, chat_resp, persist, bg: BackgroundTasks, escalate: bool = False):
    """Persist + send a reply already generated by generate_chat_response(), then
    run its queued background tasks. Split out of delayed_bot_response() so the
    handoff watchdog (chatbot/workers/handoff_watchdog.py) — which generates a
    reply for a customer's already-received, still-unanswered message instead of
    a live debounce burst — can reuse the exact same send/persist/escalate
    behavior instead of a second, drifting copy of it."""
    await persist()

    if chat_resp.products:
        first = chat_resp.products[0]
        text_wamid, image_wamid = await send_whatsapp_message(
            wa_from, chat_resp.response,
            first.original_image_url,  # raw S3/CDN URL — WhatsApp fetches directly
            image_caption=_blurb(first),
        )
        save_message_db(session_id, wa_from, BOT_NAME, "outbound", chat_resp.response, wamid=text_wamid)
        # One [image]...[/image] row per product actually sent to WhatsApp, each
        # tagged with the IMAGE's own wamid (not the caption text's) -- that's
        # what a customer's WhatsApp "reply" actually quotes when they tap a
        # specific product photo, so it's what has to be on this row for a later
        # quoted reply to resolve back to this exact product. Without this row
        # at all, bot-sent pictures were invisible in the admin transcript too
        # (only the agent-sent-image path recorded that marker).
        if first.original_image_url:
            save_message_db(session_id, wa_from, BOT_NAME, "outbound",
                             f"[image]{first.original_image_url}[/image]", wamid=image_wamid)

        # Every recommended product gets its own image + caption, not just the first —
        # a customer comparing several options should see all of them, not just one.
        for product in chat_resp.products[1:]:
            _, product_image_wamid = await send_whatsapp_message(
                wa_from, "", product.original_image_url, image_caption=_blurb(product)
            )
            if product.original_image_url:
                save_message_db(session_id, wa_from, BOT_NAME, "outbound",
                                 f"[image]{product.original_image_url}[/image]", wamid=product_image_wamid)
    else:
        wamid, _ = await send_whatsapp_message(wa_from, chat_resp.response)
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
    # Two independent sources: `escalate` is the pre-existing unviewable-media
    # trigger (decided in delayed_bot_response before the LLM ever ran);
    # chat_resp.escalation is the model's own ESCALATE: tag from this turn
    # (see chat_service.py) -- both go through the same create_escalation so
    # every handoff reason gets identical tracking and notification, not just
    # the newer ones.
    if escalate:
        await create_escalation(
            session_id=session_id, phone=wa_from,
            trigger="unviewable_media", category="general_enquiry", priority="medium",
            summary="Customer sent a photo, video, or document the bot can't view.",
        )
    elif chat_resp.escalation:
        e = chat_resp.escalation
        await create_escalation(
            session_id=session_id, phone=wa_from,
            trigger=e["trigger"], category=e["category"], priority=e["priority"],
            summary=e["summary"],
        )
