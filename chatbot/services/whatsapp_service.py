from typing import Optional

import httpx

from chatbot.config import (
    WHATSAPP_API_TOKEN,
    WHATSAPP_API_URL,
    WHATSAPP_PHONE_NUMBER_ID,
    log,
)
from chatbot.database import get_db
from chatbot.models import Message
from chatbot.utils.phone import normalize_phone


async def mark_whatsapp_read(message_id: str):
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        return
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    try:
        async with httpx.AsyncClient(timeout=5.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            await client.post(url, json=payload, headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"})
    except Exception as e:
        log.warning(f"Failed to mark message as read: {e}")


async def send_whatsapp_message(to: str, body: str, image_url: str = None) -> Optional[str]:
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        log.warning("WhatsApp credentials not configured")
        return None
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
    wamid = None
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            if image_url:
                img_payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": image_url}}
                img_resp = await client.post(url, json=img_payload, headers=headers)
                if img_resp.is_success:
                    log.info(f"Product image sent to {to}")
                else:
                    log.warning(f"Product image send failed ({img_resp.status_code}): {img_resp.text}")
            text_payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
            resp = await client.post(url, json=text_payload, headers=headers)
        log.info(f"WhatsApp message sent to {to}: status={resp.status_code}")
        try:
            wamid = resp.json()["messages"][0]["id"]
        except Exception:
            pass
    except Exception as e:
        log.error(f"Failed to send WhatsApp message: {e}")
    return wamid


def save_message_db(session_id: str, phone: str, name: str, direction: str, content: str, wamid: str = None):
    try:
        db = get_db()
        db.add(Message(session_id=session_id, phone=normalize_phone(phone), name=name, direction=direction, content=content, wamid=wamid))
        db.commit()
        db.close()
    except Exception as e:
        log.error(f"Failed to save message: {e}")
