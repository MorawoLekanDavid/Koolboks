import asyncio

from fastapi import BackgroundTasks

from chatbot.config import BOT_RESPONSE_DELAY, log
from chatbot.core import redis_client
from chatbot.services.chat_service import ChatRequest, chat_handler
from chatbot.services.whatsapp_service import save_message_db, send_whatsapp_message


async def delayed_bot_response(session_id: str, wa_from: str, name: str, text: str):
    """Wait for the agent takeover window, then respond if no agent claimed the session."""
    if BOT_RESPONSE_DELAY > 0:
        await asyncio.sleep(BOT_RESPONSE_DELAY)

    # Re-check handoff — agent may have taken over during the delay
    handoff_key = f"koolbuy:handoff:{session_id}"
    in_handoff = await redis_client.client.get(handoff_key) if redis_client.client else None
    if in_handoff:
        log.info(f"[delay] {session_id} claimed by agent during window — bot silent")
        return

    # Generate bot response
    bg = BackgroundTasks()
    chat_req = ChatRequest(session_id=session_id, message=text, user_name=name)
    try:
        chat_resp = await chat_handler(chat_req, bg)
    except Exception as e:
        log.error(f"[delay] chat_handler failed for {session_id}: {e}")
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
        save_message_db(session_id, wa_from, "KoolBot", "outbound", chat_resp.response, wamid=wamid)
        # One [image]...[/image] row per product actually sent to WhatsApp —
        # without this, bot-sent pictures were invisible in the admin
        # transcript (only the agent-sent-image path recorded that marker),
        # making it look like the bot never sends pictures at all even
        # though delivery to the customer was working the whole time.
        if first.original_image_url:
            save_message_db(session_id, wa_from, "KoolBot", "outbound", f"[image]{first.original_image_url}[/image]")

        # Every recommended product gets its own image + caption, not just the first —
        # a customer comparing several options should see all of them, not just one.
        for product in chat_resp.products[1:]:
            await send_whatsapp_message(
                wa_from, "", product.original_image_url, image_caption=_blurb(product)
            )
            if product.original_image_url:
                save_message_db(session_id, wa_from, "KoolBot", "outbound", f"[image]{product.original_image_url}[/image]")
    else:
        wamid = await send_whatsapp_message(wa_from, chat_resp.response)
        save_message_db(session_id, wa_from, "KoolBot", "outbound", chat_resp.response, wamid=wamid)

    # Run background tasks queued by chat_handler (save_lead, update_lead_address, etc.)
    for task in bg.tasks:
        try:
            if asyncio.iscoroutinefunction(task.func):
                await task.func(*task.args, **task.kwargs)
            else:
                task.func(*task.args, **task.kwargs)
        except Exception as e:
            log.warning(f"[delay] bg task {task.func.__name__} failed: {e}")
