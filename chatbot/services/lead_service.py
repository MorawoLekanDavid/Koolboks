import json
import re
from datetime import datetime

import httpx
from sqlalchemy import and_

from chatbot.config import GROQ_MODEL, IDLE_THRESHOLD, ZAPIER_WEBHOOK, log
from chatbot.database import get_db
from chatbot.models import Lead
from chatbot.services.groq_service import groq_client
from chatbot.utils.phone import normalize_phone
from chatbot.utils.text import clean_name


def calc_active_duration(history: list) -> str:
    """
    Sum only the gaps between consecutive messages that are under IDLE_THRESHOLD.
    Gaps longer than IDLE_THRESHOLD (default 5 min) are treated as idle time
    and excluded — so pausing the chat for hours doesn't inflate the duration.
    Returns a human-readable string like '4m 32s'.
    """
    timestamps = []
    for msg in history:
        ts = msg.get("ts")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(ts))
            except Exception:
                pass

    if len(timestamps) < 2:
        return ""

    active_seconds = 0
    for i in range(1, len(timestamps)):
        gap = (timestamps[i] - timestamps[i - 1]).total_seconds()
        if gap <= IDLE_THRESHOLD:
            active_seconds += gap

    if active_seconds < 60:
        return f"{int(active_seconds)}s"
    mins = int(active_seconds // 60)
    secs = int(active_seconds % 60)
    return f"{mins}m {secs}s"


async def save_lead(user_name: str, phone: str, history: list, session_id: str = None):
    """Extract rich lead data from conversation and save to database"""
    # The phone the customer typed in chat (saved as Lead.phone) can differ from
    # the WhatsApp number the conversation is happening on — keep the latter too
    # so the admin dashboard can open the right chat thread.
    wa_phone = session_id[3:] if session_id and session_id.startswith("wa_") else None
    lines = []
    for msg in history:
        role = "Customer" if msg.get("role") == "user" else "KoolBot"
        content = re.sub(r'\[[^\]]*\]', '', msg.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    transcript = "\n".join(lines[-40:])

    # Calculate active duration before the Groq call
    duration = calc_active_duration(history)

    data = {}
    try:
        prompt = (
            "Read this sales conversation and extract the following fields. "
            "Reply ONLY with valid JSON, no markdown, no extra text:\n"
            '{"name": "", "business": "", "product_interest": "", "amount": "", '
            '"payment_plan": "", "pain_point": "", "power_type": "", "address": ""}\n\n'
            "Rules:\n"
            "- name: customer's actual name ONLY — strip ALL greetings like "
            "'hello', 'hi', 'I am', 'I\\'m', 'my name is' — return ONLY the clean name\n"
            "- business: what they sell or their business type\n"
            "- product_interest: exact product they agreed on or showed most interest in\n"
            "- amount: price of that product as mentioned in the chat\n"
            "- payment_plan: 'outright' if full payment, installment details, or 'flex 70/30'\n"
            "- pain_point: their main challenge e.g. spoilage, power cuts, fuel cost\n"
            "- power_type: 'grid' if NEPA/generator, 'off-grid' if no power, 'solar' if solar\n"
            "- address: delivery location if mentioned, else leave empty\n\n"
            f"CONVERSATION:\n{transcript}"
        )
        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0,
        )
        raw = (completion.choices[0].message.content or "").strip()
        raw = re.sub(r'^```[a-z]*\n?', '', raw).strip()
        raw = re.sub(r'\n?```$', '', raw).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as json_err:
            log.warning(f"Lead JSON parse failed: {json_err} | raw={raw[:200]}")
            data = {}
        log.info(f"Lead extracted: {data}")
    except Exception as e:
        log.warning(f"Lead extraction failed: {e}")

    try:
        db = get_db()
        clean = clean_name(data.get("name") or user_name)
        norm_phone = normalize_phone(phone)
        existing = db.query(Lead).filter(Lead.phone == norm_phone).first()
        if existing:
            if data.get("name"):        existing.name             = clean
            if data.get("business"):    existing.business         = data["business"]
            if data.get("product_interest"): existing.product_interest = data["product_interest"]
            if data.get("amount"):      existing.amount           = data["amount"]
            if data.get("payment_plan"): existing.payment_plan    = data["payment_plan"]
            if data.get("pain_point"):  existing.pain_point       = data["pain_point"]
            if data.get("power_type"):  existing.power_type       = data["power_type"]
            if data.get("address"):     existing.address          = data["address"]
            if wa_phone:                 existing.whatsapp_phone   = wa_phone
            existing.active_duration = duration
            existing.updated_at = datetime.utcnow()
            log.info(f"Lead updated: {clean} | {norm_phone} | duration={duration}")
        else:
            lead = Lead(
                name=clean,
                phone=norm_phone,
                whatsapp_phone=wa_phone,
                business=data.get("business", ""),
                product_interest=data.get("product_interest", ""),
                amount=data.get("amount", ""),
                payment_plan=data.get("payment_plan", ""),
                pain_point=data.get("pain_point", ""),
                power_type=data.get("power_type", ""),
                address=data.get("address", ""),
                active_duration=duration,
            )
            db.add(lead)
            log.info(f"Lead saved: {clean} | {phone} | duration={duration}")
        db.commit()
        db.close()
    except Exception as e:
        log.error(f"Failed to save lead to DB: {e}")

    # ── Push to CRM via Zapier webhook ─────────────────────────────────────────
    if ZAPIER_WEBHOOK:
        try:
            payload = {
                "name":             clean_name(data.get("name") or user_name),
                "phone":            phone,
                "business":         data.get("business", ""),
                "product_interest": data.get("product_interest", ""),
                "amount":           data.get("amount", ""),
                "payment_plan":     data.get("payment_plan", ""),
                "pain_point":       data.get("pain_point", ""),
                "power_type":       data.get("power_type", ""),
                "address":          data.get("address", ""),
                "active_duration":  duration,
                "source":           "koolbuy_chatbot",
            }
            async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
                resp = await client.post(ZAPIER_WEBHOOK, json=payload)
            log.info(f"Zapier webhook sent: status={resp.status_code}")
        except Exception as e:
            log.warning(f"Zapier webhook failed: {e}")


async def update_lead_address(phone: str, address: str):
    """Update delivery address for a lead in database"""
    if not phone or not address:
        return
    try:
        db = get_db()
        lead = db.query(Lead).filter(
            and_(
                Lead.phone == normalize_phone(phone),
                (Lead.address == None) | (Lead.address == "")
            )
        ).order_by(Lead.created_at.desc()).first()

        if lead:
            lead.address = address.strip()
            db.commit()
            log.info(f"Lead address updated: {phone} -> {address}")
        db.close()
    except Exception as e:
        log.warning(f"Failed to update lead address: {e}")
