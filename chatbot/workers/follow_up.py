import asyncio
from datetime import datetime, timedelta

from sqlalchemy import func, select

from chatbot.config import (
    FOLLOW_UP_ENABLED,
    FOLLOW_UP_HOURS,
    FOLLOW_UP_MESSAGE,
    FOLLOW_UP_RECHECK_DAYS,
    GROQ_MODEL,
    WHATSAPP_API_TOKEN,
    log,
)
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Lead, Message
from chatbot.services.groq_service import groq_client
from chatbot.services.whatsapp_service import send_whatsapp_message


async def run_follow_ups():
    """Send a personalised follow-up WhatsApp message to conversations silent for
    FOLLOW_UP_HOURS that never gave their phone number."""
    if not redis_client.client or not WHATSAPP_API_TOKEN:
        return
    cutoff = datetime.utcnow() - timedelta(hours=FOLLOW_UP_HOURS)
    db = get_db()
    try:
        rows = db.execute(
            select(Message.phone, func.max(Message.created_at).label("last_msg"))
            .where(Message.direction == "inbound")
            .group_by(Message.phone)
            .having(func.max(Message.created_at) < cutoff)
        ).all()

        sent = 0
        for r in rows:
            phone = r.phone
            session_id = f"wa_{phone}"
            if await redis_client.client.get(f"koolbuy:followup:{phone}"):
                continue
            if await redis_client.client.get(f"koolbuy:handoff:{session_id}"):
                continue
            lead = db.query(Lead).filter(Lead.phone == phone).first()
            if lead:
                continue

            # Build a short transcript from DB for personalisation
            msgs = db.execute(
                select(Message).where(Message.phone == phone)
                .order_by(Message.created_at.asc())
            ).scalars().all()
            lines = []
            for m in msgs[-12:]:
                role = "Customer" if m.direction == "inbound" else "KoolBot"
                lines.append(f"{role}: {m.content[:200]}")
            transcript = "\n".join(lines) or "No prior messages."

            # Ask Groq to write a personalised, polite sales follow-up
            prompt = (
                "You are a friendly, professional sales representative for Koolbuy, "
                "a Nigerian company that sells cooling and solar-powered products "
                "(freezers, refrigerators, ice makers, solar kits).\n\n"
                "A potential customer started a WhatsApp conversation but went silent "
                f"without giving their phone number. Below is the conversation so far:\n\n"
                f"{transcript}\n\n"
                "Write a SHORT follow-up WhatsApp message (2-4 sentences). Rules:\n"
                "- Sound warm and human, not like a bot\n"
                "- Use the customer's name if you know it\n"
                "- Reference what they were interested in if relevant\n"
                "- Do NOT be pushy or desperate\n"
                "- End with a gentle open question or offer to help\n"
                "- No greetings like 'Dear Customer' — keep it casual and natural\n\n"
                "Write ONLY the message text, nothing else:"
            )
            try:
                completion = await groq_client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=180,
                    temperature=0.75,
                )
                follow_up_text = (completion.choices[0].message.content or "").strip()
            except Exception as e:
                log.warning(f"Groq follow-up generation failed for {phone}: {e}")
                follow_up_text = FOLLOW_UP_MESSAGE  # fallback to default

            await send_whatsapp_message(phone, follow_up_text)
            await redis_client.client.set(
                f"koolbuy:followup:{phone}", "1",
                ex=86400 * FOLLOW_UP_RECHECK_DAYS
            )
            log.info(f"Follow-up sent to {phone}: {follow_up_text[:60]}...")
            sent += 1

        if sent:
            log.info(f"Follow-up run complete: {sent} message(s) sent")
    except Exception as e:
        log.error(f"Follow-up run error: {e}")
    finally:
        db.close()


async def follow_up_worker():
    """Hourly background task that triggers follow-up messages."""
    log.info(f"Follow-up worker started (enabled={FOLLOW_UP_ENABLED}, hours={FOLLOW_UP_HOURS})")
    while True:
        try:
            await asyncio.sleep(3600)
            if FOLLOW_UP_ENABLED:
                await run_follow_ups()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Follow-up worker error: {e}")
