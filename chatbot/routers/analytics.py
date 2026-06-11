from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, select

from chatbot.database import get_db
from chatbot.dependencies import require_admin
from chatbot.models import Lead, Message
from chatbot.utils.phone import normalize_phone

router = APIRouter(prefix="/admin/analytics", tags=["analytics"])


@router.get("/conversations-handled")
async def conversations_handled(ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        rows = db.execute(
            select(
                Message.name,
                func.date(Message.created_at).label("date"),
                func.count(func.distinct(Message.phone)).label("count"),
            ).where(and_(
                Message.direction == "outbound",
                Message.name != "KoolBot",
                Message.name.isnot(None),
                Message.name != "",
            )).group_by(Message.name, func.date(Message.created_at))
            .order_by(func.date(Message.created_at).desc())
        ).all()
        return [{"agent": r.name, "date": str(r.date), "conversations": r.count} for r in rows]
    finally:
        db.close()


@router.get("/product-recommendations")
async def product_recommendations(ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        rows = db.execute(
            select(Lead.product_interest, func.count(Lead.id).label("count"))
            .where(Lead.product_interest != None, Lead.product_interest != "")
            .group_by(Lead.product_interest)
            .order_by(func.count(Lead.id).desc())
        ).all()
        total = sum(r.count for r in rows)
        return [{"product": r.product_interest, "count": r.count,
                 "pct": round(r.count / total * 100) if total else 0} for r in rows]
    finally:
        db.close()


@router.get("/lead-funnel")
async def lead_funnel(ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        # WhatsApp sender phones (international format e.g. 2348012345678)
        msg_phones = {r.phone for r in db.execute(
            select(Message.phone).where(Message.direction == "inbound").distinct()
        ).all()}

        # Lead phones normalized to local format for cross-format comparison
        # (includes both the contact number given and the WhatsApp number messaged from)
        lead_phones_norm = set()
        for r in db.query(Lead.phone, Lead.whatsapp_phone).all():
            if r.phone:
                lead_phones_norm.add(normalize_phone(r.phone))
            if r.whatsapp_phone:
                lead_phones_norm.add(normalize_phone(r.whatsapp_phone))
        total_leads = db.query(Lead).filter(Lead.phone != None, Lead.phone != "").count()

        # Drop-offs: WhatsApp senders who never gave their number (normalize WA phone before comparing)
        drop_off = sum(1 for p in msg_phones if normalize_phone(p) not in lead_phones_norm)

        # Total conversations = drop-offs + leads
        # (leads' original WhatsApp messages may have been purged from messages table)
        total_convs = drop_off + total_leads

        return {
            "funnel": [
                {"stage": "Conversations Started", "count": total_convs},
                {"stage": "Phone Captured", "count": total_leads},
                {"stage": "Drop-off (no phone given)", "count": drop_off},
            ]
        }
    finally:
        db.close()
