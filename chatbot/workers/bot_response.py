import asyncio
from urllib.parse import unquote

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

    reply_text = chat_resp.response
    image_url = None
    if chat_resp.products:
        product = chat_resp.products[0]
        reply_text += f"\n\n🛒 *{product.name}*\n💰 N{float(product.price):,.0f}"
        if product.image_url and "img-proxy?url=" in product.image_url:
            image_url = unquote(product.image_url.split("img-proxy?url=")[1])

    save_message_db(session_id, wa_from, "KoolBot", "outbound", reply_text)
    await send_whatsapp_message(wa_from, reply_text, image_url)

    # Run background tasks queued by chat_handler (save_lead, update_lead_address, etc.)
    for task in bg.tasks:
        try:
            if asyncio.iscoroutinefunction(task.func):
                await task.func(*task.args, **task.kwargs)
            else:
                task.func(*task.args, **task.kwargs)
        except Exception as e:
            log.warning(f"[delay] bg task {task.func.__name__} failed: {e}")
