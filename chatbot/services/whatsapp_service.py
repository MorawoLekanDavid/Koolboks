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


# Whisper biases its spelling toward words it's given as context -- without
# this, brand names and local place names are exactly what a general-purpose
# model gets wrong most often (mishearing "Koolboks" as something phonetically
# close, autocorrecting a Nigerian city to a more common English word, etc).
# This isn't a transcript of anything, just a vocabulary hint.
_WHISPER_DOMAIN_PROMPT = (
    "Koolboks, Itura, solar freezer, inverter, pedestal battery, kilowatt, "
    "Naira, down payment, monthly installment, Lagos, Abuja, Kaduna, Kano, "
    "Port Harcourt, Ibadan, Enugu."
)


async def transcribe_whatsapp_audio(media_id: str) -> Optional[str]:
    """Download a voice note / audio message and transcribe it via Groq's
    Whisper. This is genuine understanding, not an acknowledgment — the
    transcript is fed into the bot's normal reasoning exactly like typed
    text, so it can actually respond to what was said.

    Uses the full whisper-large-v3 model rather than the turbo variant --
    turbo trades some accuracy for roughly 8x the speed, which isn't a
    trade worth making here: a voice note is already going through a
    multi-second download + transcribe + Groq-chat-completion + WhatsApp-send
    pipeline before the customer sees a reply, so a slightly slower but more
    accurate transcription doesn't change the felt latency, while a
    misheard word (an address, a model name, a price) can send the whole
    reply in the wrong direction."""
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
            model="whisper-large-v3",
            prompt=_WHISPER_DOMAIN_PROMPT,
        )
        text = (transcription.text or "").strip()
        return text or None
    except Exception as e:
        log.warning(f"Whisper transcription failed for {media_id}: {e}")
        return None


async def send_whatsapp_message(
    to: str, body: str, image_url: str = None, image_caption: str = None, reply_to_wamid: str = None
) -> tuple[Optional[str], Optional[str]]:
    """Returns (text_wamid, image_wamid) -- always a pair, either half None if
    that part wasn't sent or the send failed. Callers that only sent text can
    keep unpacking just the first element; callers sending a product image
    need the second one too: a customer replying to a specific product photo
    quotes the IMAGE message, not the caption text, so a caller that only
    tracks the text's wamid (the previous behavior here) can never resolve
    that reply back to which product it was about.

    `reply_to_wamid`, when given, makes WhatsApp show the sent message as a
    quoted reply to that earlier message -- attached to the image send when
    one is included (that's the bubble a human would actually be pointing
    at), otherwise to the text send."""
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        log.warning("WhatsApp credentials not configured")
        return None, None
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
    text_wamid = None
    image_wamid = None
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            if image_url:
                img_payload = {"messaging_product": "whatsapp", "to": to, "type": "image",
                                "image": {"link": image_url}}
                if image_caption:
                    img_payload["image"]["caption"] = image_caption
                if reply_to_wamid:
                    img_payload["context"] = {"message_id": reply_to_wamid}
                img_resp = await client.post(url, json=img_payload, headers=headers)
                if img_resp.is_success:
                    log.info(f"Product image sent to {to}")
                    try:
                        image_wamid = img_resp.json()["messages"][0]["id"]
                    except Exception:
                        pass
                else:
                    log.warning(f"Product image send failed ({img_resp.status_code}): {img_resp.text}")
            resp = None
            if body:
                text_payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
                if reply_to_wamid and not image_url:
                    text_payload["context"] = {"message_id": reply_to_wamid}
                resp = await client.post(url, json=text_payload, headers=headers)
        if resp is not None:
            log.info(f"WhatsApp message sent to {to}: status={resp.status_code}")
            try:
                text_wamid = resp.json()["messages"][0]["id"]
            except Exception:
                pass
    except Exception as e:
        log.error(f"Failed to send WhatsApp message: {e}")
    return text_wamid, image_wamid


# WhatsApp's media upload only accepts these -- audio in particular is far
# narrower than it looks (no webm, no plain mp3-as-anything-else): only a
# handful of containers/codecs are actually accepted, everything else has to
# be transcoded first. See ensure_whatsapp_audio() below.
_WHATSAPP_AUDIO_MIME_OK = {"audio/aac", "audio/mp4", "audio/mpeg", "audio/amr", "audio/ogg"}
# Per-type caps WhatsApp itself enforces -- checked before upload so a file
# that's too large fails fast with a clear reason instead of a cryptic 400
# from Graph partway through.
WHATSAPP_MEDIA_MAX_BYTES = {
    "image": 5 * 1024 * 1024,
    "video": 16 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "document": 100 * 1024 * 1024,
}


def whatsapp_media_type_for(content_type: str) -> str:
    """Maps a browser-supplied MIME type to one of WhatsApp's four message
    types. Anything that isn't clearly image/video/audio is sent as a
    document -- WhatsApp has no generic "file" type, document is the
    catch-all (PDFs, Office files, zips, ...)."""
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("audio/"):
        return "audio"
    return "document"


async def ensure_whatsapp_audio(data: bytes, content_type: str) -> tuple[bytes, str]:
    """Browsers can only record voice notes as audio/webm;codecs=opus (Chrome)
    or audio/ogg;codecs=opus (Firefox) via MediaRecorder -- WhatsApp's Cloud
    API accepts the latter but silently rejects the former. Rather than limit
    voice notes to Firefox-recorded audio, this transcodes anything not
    already in an accepted container to ogg/opus with ffmpeg (installed in
    the image specifically for this) before upload. Returns the original
    bytes/type unchanged if no transcode is needed or ffmpeg fails -- the
    upload call downstream still surfaces a clear error either way."""
    base_type = content_type.split(";")[0].strip().lower()
    if base_type in _WHATSAPP_AUDIO_MIME_OK:
        return data, content_type
    import asyncio as _asyncio
    import tempfile
    import os as _os
    try:
        with tempfile.NamedTemporaryFile(suffix=".in", delete=False) as src:
            src.write(data)
            src_path = src.name
        dst_path = src_path + ".ogg"
        try:
            proc = await _asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", src_path, "-c:a", "libopus", "-b:a", "32k", dst_path,
                stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE,
            )
            _, stderr = await _asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0 or not _os.path.exists(dst_path):
                log.warning(f"Voice note transcode failed: {stderr.decode(errors='ignore')[:500]}")
                return data, content_type
            with open(dst_path, "rb") as f:
                return f.read(), "audio/ogg"
        finally:
            for p in (src_path, dst_path):
                try:
                    _os.remove(p)
                except OSError:
                    pass
    except Exception as e:
        log.warning(f"Voice note transcode error: {e}")
        return data, content_type


async def upload_whatsapp_media(data: bytes, content_type: str, filename: str) -> Optional[str]:
    """Uploads raw bytes to WhatsApp's own media store and returns the media
    ID to reference in a send call. This is the only way to send a file the
    agent uploaded from their own device -- unlike product images, there's no
    public URL for it to hand WhatsApp instead."""
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        log.warning("WhatsApp credentials not configured")
        return None
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            files = {"file": (filename, data, content_type)}
            form = {"messaging_product": "whatsapp"}
            resp = await client.post(url, data=form, files=files, headers=headers)
        if resp.is_success:
            return resp.json().get("id")
        log.warning(f"WhatsApp media upload failed ({resp.status_code}): {resp.text}")
        return None
    except Exception as e:
        log.error(f"WhatsApp media upload error: {e}")
        return None


async def send_whatsapp_media_message(
    to: str, media_type: str, media_id: str, caption: str = None,
    filename: str = None, reply_to_wamid: str = None,
) -> Optional[str]:
    """Sends a message referencing media already uploaded via
    upload_whatsapp_media(). Returns the sent message's wamid, or None on
    failure. Captions only render for image/video/document -- WhatsApp drops
    them silently on audio, so callers shouldn't bother passing one there."""
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        log.warning("WhatsApp credentials not configured")
        return None
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
    media_obj = {"id": media_id}
    if caption and media_type in ("image", "video", "document"):
        media_obj["caption"] = caption
    if media_type == "document" and filename:
        media_obj["filename"] = filename
    payload = {"messaging_product": "whatsapp", "to": to, "type": media_type, media_type: media_obj}
    if reply_to_wamid:
        payload["context"] = {"message_id": reply_to_wamid}
    try:
        async with httpx.AsyncClient(timeout=15.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.is_success:
            try:
                return resp.json()["messages"][0]["id"]
            except Exception:
                return None
        log.warning(f"WhatsApp {media_type} send failed ({resp.status_code}): {resp.text}")
        return None
    except Exception as e:
        log.error(f"WhatsApp {media_type} send error: {e}")
        return None


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


def save_message_db(session_id: str, phone: str, name: str, direction: str, content: str, wamid: str = None,
                     delivery_status: str = "sent", reply_to_wamid: str = None):
    try:
        db = get_db()
        norm = normalize_phone(phone)
        db.add(Message(session_id=session_id, phone=norm, name=name, direction=direction, content=content,
                        wamid=wamid, delivery_status=delivery_status, reply_to_wamid=reply_to_wamid))
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
