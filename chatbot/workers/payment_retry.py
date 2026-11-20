import asyncio
import re
from datetime import datetime, timedelta

from sqlalchemy import func

from chatbot.config import log
from chatbot.database import get_db
from chatbot.models import Message
from chatbot.services.whatsapp_service import save_message_db, send_whatsapp_message

# The exact string Meta uses for error code 131042 -- "no payment method is
# set up for your WhatsApp Business account." This is a Meta-side billing
# rejection, not an app bug: nothing here can fix the root cause, only keep
# retrying until whoever manages the Meta Business Manager billing hub does.
PAYMENT_ERROR = "Business eligibility payment issue"

RETRY_INTERVAL_SECONDS = 300  # 5 min -- fast enough that a fix on Meta's side clears the backlog quickly, without hammering their API while it's still broken
ALERT_WINDOW_HOURS = 2  # how far back the dashboard banner looks before it decides the issue is still "active"

_IMAGE_TAG_RE = re.compile(r'^\[image\](.+?)\[/image\]$')


async def retry_payment_failed_messages() -> int:
    """Finds every outbound message that failed with Meta's payment-issue
    error and whose customer is still inside WhatsApp's 24h free-form
    window, and resends it exactly as originally written (plain text or a
    single product image). The original row is marked 'retried' the moment
    a resend is attempted so it's never picked up twice; the resend itself
    is a brand-new Message row that gets its own real delivery status from
    Meta's webhook like any other send. If Meta billing is still broken,
    that new row fails the same way and becomes eligible for the next
    cycle on its own -- this is deliberately self-sustaining so a human
    never has to notice and manually resend again, the way this had to be
    done by hand once already.

    [Template: ...] rows are skipped -- the stored content is only a
    human-readable label, not the original template name/params, so there
    is no safe way to reconstruct that specific API call; those still need
    a manual resend from the Templates UI.

    Returns how many messages were actually resent, for the worker's log line."""
    db = get_db()
    try:
        candidates = (
            db.query(Message)
            .filter(Message.direction == "outbound", Message.delivery_status == "failed",
                    Message.delivery_error == PAYMENT_ERROR)
            .order_by(Message.created_at.asc())
            .all()
        )
        if not candidates:
            return 0

        by_phone: dict[str, list[Message]] = {}
        for m in candidates:
            by_phone.setdefault(m.phone, []).append(m)

        last_inbound = dict(
            db.query(Message.phone, func.max(Message.created_at))
            .filter(Message.phone.in_(list(by_phone.keys())), Message.direction == "inbound")
            .group_by(Message.phone)
            .all()
        )

        retried = 0
        now = datetime.utcnow()
        for phone, msgs in by_phone.items():
            last_in = last_inbound.get(phone)
            if not last_in or now >= last_in + timedelta(hours=24):
                # Window's closed -- a retry would just fail differently
                # (needs an approved template, not a free-form resend).
                continue
            for m in msgs:
                if m.content.startswith("[Template:"):
                    continue
                img_match = _IMAGE_TAG_RE.match(m.content.strip())
                try:
                    if img_match:
                        _, new_wamid = await send_whatsapp_message(phone, "", image_url=img_match.group(1))
                    else:
                        new_wamid, _ = await send_whatsapp_message(phone, m.content)
                except Exception as e:
                    log.warning(f"[payment-retry] resend failed for {phone}: {e}")
                    continue
                m.delivery_status = "retried"
                db.commit()
                save_message_db(m.session_id, phone, m.name, "outbound", m.content, wamid=new_wamid)
                retried += 1
        return retried
    finally:
        db.close()


def get_payment_issue_status() -> dict:
    """Powers the admin dashboard's payment-issue banner. Windowed (not
    "ever happened") so the banner clears itself once Meta billing has
    actually been fixed and enough time has passed with no new failures,
    instead of staying stuck on forever. Plain sync function (no real
    async work inside) -- its one caller (the bot-status route in
    system.py) runs it via run_in_threadpool, same as every other
    DB-backed helper in this codebase."""
    db = get_db()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=ALERT_WINDOW_HOURS)
        count, first_at, last_at = (
            db.query(func.count(Message.id), func.min(Message.created_at), func.max(Message.created_at))
            .filter(Message.direction == "outbound", Message.delivery_error == PAYMENT_ERROR,
                    Message.created_at >= cutoff)
            .first()
        )
        return {
            "active": bool(count),
            "count": count or 0,
            "first_at": first_at.isoformat() if first_at else None,
            "last_at": last_at.isoformat() if last_at else None,
            "window_hours": ALERT_WINDOW_HOURS,
        }
    finally:
        db.close()


async def payment_retry_worker():
    """Periodic background task, same pattern as the other watchdogs in
    this file's package -- just on a much shorter cadence, since the whole
    point is closing the gap between "Meta billing gets fixed" and "queued
    messages actually go out" without anyone needing to notice and act."""
    log.info(f"Payment-issue retry worker started (interval={RETRY_INTERVAL_SECONDS}s)")
    while True:
        try:
            await asyncio.sleep(RETRY_INTERVAL_SECONDS)
            sent = await retry_payment_failed_messages()
            if sent:
                log.info(f"[payment-retry] resent {sent} message(s) previously blocked by the payment issue")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Payment-issue retry worker error: {e}")
