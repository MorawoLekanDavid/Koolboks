from typing import Optional

import httpx

from chatbot.config import (
    WHATSAPP_API_TOKEN,
    WHATSAPP_API_URL,
    WHATSAPP_PHONE_NUMBER_ID,
    log,
)
from chatbot.database import get_db
from chatbot.models import Lead, Message
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


async def fetch_whatsapp_media(media_id: str) -> Optional[tuple]:
    """Resolve a WhatsApp media ID to its actual bytes. Meta never hands out a
    stable, browsable URL for inbound media — only an ID that resolves to a
    short-lived, auth-required download link — so this always does a live
    two-step fetch (resolve the link, then download it) rather than caching a
    URL that would go stale within minutes. Returns (content_type, bytes) or
    None on any failure — callers decide how to degrade."""
    if not WHATSAPP_API_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=15.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            meta_resp = await client.get(f"{WHATSAPP_API_URL}/{media_id}", headers=headers)
            if meta_resp.status_code != 200:
                log.warning(f"WhatsApp media lookup failed for {media_id}: {meta_resp.status_code}")
                return None
            media_url = meta_resp.json().get("url")
            if not media_url:
                return None
            file_resp = await client.get(media_url, headers=headers)
            if file_resp.status_code != 200:
                log.warning(f"WhatsApp media download failed for {media_id}: {file_resp.status_code}")
                return None
            return file_resp.headers.get("content-type", "application/octet-stream"), file_resp.content
    except Exception as e:
        log.warning(f"WhatsApp media fetch error for {media_id}: {e}")
        return None


async def transcribe_whatsapp_audio(media_id: str) -> Optional[str]:
    """Download a voice note / audio message and transcribe it via Groq's
    Whisper. This is genuine understanding, not an acknowledgment — the
    transcript is fed into the bot's normal reasoning exactly like typed
    text, so it can actually respond to what was said."""
    from chatbot.services.groq_service import groq_client  # deferred: avoids a
    # module-load-order cycle, since groq_service doesn't need anything here

    fetched = await fetch_whatsapp_media(media_id)
    if not fetched:
        return None
    content_type, audio_bytes = fetched
    ext = "ogg" if "ogg" in content_type else "mp4" if "mp4" in content_type else "mp3" if "mpeg" in content_type else "bin"
    try:
        transcription = await groq_client.audio.transcriptions.create(
            file=(f"voice.{ext}", audio_bytes, content_type),
            model="whisper-large-v3-turbo",
        )
        text = (transcription.text or "").strip()
        return text or None
    except Exception as e:
        log.warning(f"Whisper transcription failed for {media_id}: {e}")
        return None


async def send_whatsapp_message(
    to: str, body: str, image_url: str = None, image_caption: str = None
) -> Optional[str]:
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        log.warning("WhatsApp credentials not configured")
        return None
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
    wamid = None
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            if image_url:
                img_payload = {"messaging_product": "whatsapp", "to": to, "type": "image",
                                "image": {"link": image_url}}
                if image_caption:
                    img_payload["image"]["caption"] = image_caption
                img_resp = await client.post(url, json=img_payload, headers=headers)
                if img_resp.is_success:
                    log.info(f"Product image sent to {to}")
                else:
                    log.warning(f"Product image send failed ({img_resp.status_code}): {img_resp.text}")
            resp = None
            if body:
                text_payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
                resp = await client.post(url, json=text_payload, headers=headers)
        if resp is not None:
            log.info(f"WhatsApp message sent to {to}: status={resp.status_code}")
            try:
                wamid = resp.json()["messages"][0]["id"]
            except Exception:
                pass
    except Exception as e:
        log.error(f"Failed to send WhatsApp message: {e}")
    return wamid


async def send_whatsapp_template(to: str, template_name: str, variables: list[str] = None, language: str = "en") -> bool:
    """Send an approved template message — the only way to reach a phone
    number that hasn't messaged us first (invites, OTPs), since Meta blocks
    free-text sends outside a 24h customer-initiated window. Returns False
    (never raises) if the template doesn't exist yet or isn't approved, so
    callers can degrade gracefully instead of crashing the whole flow."""
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        log.warning("WhatsApp credentials not configured")
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {"name": template_name, "language": {"code": language}},
    }
    if variables:
        payload["template"]["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": v} for v in variables],
        }]
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            resp = await client.post(
                f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"},
            )
        if resp.is_success:
            log.info(f"WhatsApp template '{template_name}' sent to {to}")
            return True
        log.warning(f"WhatsApp template '{template_name}' send to {to} failed ({resp.status_code}): {resp.text}")
        return False
    except Exception as e:
        log.error(f"WhatsApp template send error: {e}")
        return False


async def send_whatsapp_otp_template(to: str, template_name: str, code: str, language: str = "en") -> bool:
    """Send an AUTHENTICATION-category template. Meta renders fixed wording for
    these — no free-text body params — and represents the OTP button as a URL
    button (the code fills the {{1}} placeholder in the button's link), so
    both the BODY and BUTTON components need the same code as a text param."""
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        log.warning("WhatsApp credentials not configured")
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language, "policy": "deterministic"},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": code}]},
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": code}],
                },
            ],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            resp = await client.post(
                f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"},
            )
        if resp.is_success:
            log.info(f"WhatsApp OTP template '{template_name}' sent to {to}")
            return True
        log.warning(f"WhatsApp OTP template '{template_name}' send to {to} failed ({resp.status_code}): {resp.text}")
        return False
    except Exception as e:
        log.error(f"WhatsApp OTP template send error: {e}")
        return False


def save_message_db(session_id: str, phone: str, name: str, direction: str, content: str, wamid: str = None, delivery_status: str = "sent"):
    try:
        db = get_db()
        norm = normalize_phone(phone)
        db.add(Message(session_id=session_id, phone=norm, name=name, direction=direction, content=content, wamid=wamid, delivery_status=delivery_status))
        if direction == "inbound":
            lead = db.query(Lead).filter(Lead.phone == norm).first()
            if lead:
                # The customer's real WhatsApp display name only shows up once
                # they actually message in — never overwrite a name that's
                # already been captured (bot-extracted or agent-entered).
                if not lead.name and name and name != "Customer":
                    lead.name = name
                # A contact tab entry moves itself forward once the customer
                # replies — never backward, so a manual stage change (e.g.
                # "converted", "dead") never gets clobbered by this.
                if lead.source in ("manual", "import") and (lead.outreach_stage or "not_contacted") in ("not_contacted", "contacted"):
                    lead.outreach_stage = "responded"
        db.commit()
        db.close()
    except Exception as e:
        log.error(f"Failed to save message: {e}")
