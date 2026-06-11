import asyncio
import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy import and_, func, select

from chatbot.config import (
    REENGAGEMENT_TEMPLATE,
    REENGAGEMENT_TEMPLATE_LANG,
    WHATSAPP_API_TOKEN,
    WHATSAPP_API_URL,
    WHATSAPP_PHONE_NUMBER_ID,
    log,
)
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Lead, Message
from chatbot.services.whatsapp_service import save_message_db


async def reengagement_worker():
    """Hourly worker: sends a WhatsApp template to drop-offs whose 24h window has closed."""
    log.info("Re-engagement worker started")
    while True:
        await asyncio.sleep(3600)
        if not redis_client.client or not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
            continue
        # Read config from Redis (set via dashboard) or fall back to env vars
        cfg_raw = await redis_client.client.get("koolbuy:reengagement_config")
        cfg = json.loads(cfg_raw) if cfg_raw else {}
        tmpl_name = cfg.get("name") or REENGAGEMENT_TEMPLATE
        tmpl_lang = cfg.get("lang") or REENGAGEMENT_TEMPLATE_LANG
        enabled = cfg.get("enabled", True) if cfg_raw else bool(REENGAGEMENT_TEMPLATE)
        if not tmpl_name or not enabled:
            continue
        try:
            cutoff_hi = datetime.utcnow() - timedelta(hours=24)
            cutoff_lo = datetime.utcnow() - timedelta(hours=72)
            db = get_db()
            try:
                rows = db.execute(
                    select(Message.phone,
                           func.max(Message.created_at).label("last_msg"),
                           func.min(Message.name).label("cust_name"))
                    .where(Message.direction == "inbound")
                    .group_by(Message.phone)
                    .having(and_(func.max(Message.created_at) <= cutoff_hi,
                                 func.max(Message.created_at) >= cutoff_lo))
                ).all()
                lead_phones = {r.phone for r in db.query(Lead.phone).all()}
            finally:
                db.close()

            sent = 0
            for row in rows:
                phone = row.phone
                if phone in lead_phones:
                    continue
                if await redis_client.client.get(f"koolbuy:reengaged:{phone}"):
                    continue
                session_id = f"wa_{phone}"
                if await redis_client.client.get(f"koolbuy:handoff:{session_id}"):
                    continue
                wa_to = phone.lstrip('+')
                cust_name = row.cust_name or "there"
                payload = {
                    "messaging_product": "whatsapp",
                    "to": wa_to,
                    "type": "template",
                    "template": {
                        "name": tmpl_name,
                        "language": {"code": tmpl_lang},
                        "components": [{"type": "body", "parameters": [{"type": "text", "text": cust_name}]}]
                    }
                }
                try:
                    async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
                        r = await client.post(
                            f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                            json=payload,
                            headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
                        )
                    if r.is_success:
                        await redis_client.client.set(f"koolbuy:reengaged:{phone}", "1", ex=7 * 86400)
                        save_message_db(session_id, phone, "KoolBot", "outbound", f"[Auto re-engagement: {tmpl_name}]")
                        sent += 1
                        log.info(f"Re-engagement template sent to {phone}")
                    else:
                        log.warning(f"Re-engagement failed for {phone}: {r.text}")
                except Exception as e:
                    log.warning(f"Re-engagement send error for {phone}: {e}")
            if sent:
                log.info(f"Re-engagement worker: {sent} templates sent")
        except Exception as e:
            log.error(f"Re-engagement worker error: {e}")
