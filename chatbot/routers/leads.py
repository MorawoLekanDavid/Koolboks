from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select

from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx
from chatbot.models import Lead, LeadNote, Message
from chatbot.utils.phone import normalize_phone

router = APIRouter(prefix="/admin/leads", tags=["leads"])


@router.get("/by-phone/{phone}")
async def get_lead_by_phone(phone: str, ctx: dict = Depends(get_admin_ctx)):
    db = get_db()
    try:
        norm = normalize_phone(phone)
        lead = db.query(Lead).filter(Lead.phone == norm).first()
        if not lead:
            # try raw phone in case not yet normalized
            lead = db.query(Lead).filter(Lead.phone == phone).first()
        if not lead:
            return None
        s = 0
        if lead.name and lead.name not in ("Customer", ""): s += 15
        if lead.business: s += 10
        if lead.product_interest: s += 25
        if lead.amount: s += 15
        if lead.payment_plan: s += 10
        if lead.address: s += 25
        score = min(s, 100)
        interest = "High" if score >= 70 else "Medium" if score >= 40 else "Low"

        # Activity from messages table
        activity = db.execute(
            select(
                func.min(Message.created_at).label("first_seen"),
                func.max(Message.created_at).label("last_seen"),
                func.count(Message.id).label("total_messages"),
            ).where(Message.phone == norm)
        ).first()

        return {
            "name": lead.name, "phone": lead.phone,
            "business": lead.business, "product_interest": lead.product_interest,
            "amount": lead.amount, "payment_plan": lead.payment_plan,
            "address": lead.address, "active_duration": lead.active_duration,
            "created_at": lead.created_at.isoformat() if lead.created_at else None,
            "score": score, "interest": interest,
            "activity": {
                "first_seen": activity.first_seen.isoformat() if activity and activity.first_seen else None,
                "last_seen": activity.last_seen.isoformat() if activity and activity.last_seen else None,
                "total_messages": activity.total_messages if activity else 0,
            },
        }
    finally:
        db.close()


@router.get("/{phone}/notes")
async def get_lead_notes(phone: str, ctx: dict = Depends(get_admin_ctx)):
    db = get_db()
    try:
        norm = normalize_phone(phone)
        notes = db.query(LeadNote).filter(LeadNote.lead_phone == norm)\
                  .order_by(LeadNote.created_at.desc()).all()
        return [{"id": n.id, "content": n.content, "created_by": n.created_by,
                 "created_at": n.created_at.isoformat()} for n in notes]
    finally:
        db.close()


class NoteIn(BaseModel):
    content: str
    created_by: Optional[str] = "Agent"


@router.post("/{phone}/notes")
async def add_lead_note(phone: str, body: NoteIn, ctx: dict = Depends(get_admin_ctx)):
    if not body.content.strip():
        raise HTTPException(400, "Note content cannot be empty")
    db = get_db()
    try:
        norm = normalize_phone(phone)
        note = LeadNote(lead_phone=norm, content=body.content.strip(),
                        created_by=body.created_by or ctx.get("name") or ctx.get("email", "Agent"))
        db.add(note)
        db.commit()
        db.refresh(note)
        return {"id": note.id, "content": note.content, "created_by": note.created_by,
                "created_at": note.created_at.isoformat()}
    finally:
        db.close()


@router.get("")
async def list_leads(
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    ctx: dict = Depends(get_admin_ctx)
):
    """Returns leads who gave a valid phone number (Interested)."""
    db = get_db()
    try:
        q = db.query(Lead).filter(Lead.phone != None, Lead.phone != "")
        if date_from:
            q = q.filter(Lead.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            q = q.filter(Lead.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
        leads = q.order_by(Lead.created_at.desc()).all()
        return [{
            "id": l.id,
            "name": l.name,
            "phone": l.phone,
            "whatsapp_phone": l.whatsapp_phone,
            "product_interest": l.product_interest,
            "business": l.business,
            "amount": l.amount,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        } for l in leads]
    finally:
        db.close()


@router.get("/dropoffs")
async def list_dropoffs(
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    ctx: dict = Depends(get_admin_ctx)
):
    """Returns phones that messaged on WhatsApp but never gave their number (Drop-offs)."""
    db = get_db()
    try:
        q = select(
            Message.phone,
            func.max(case((Message.direction == "inbound", Message.name), else_=None)).label("name"),
            func.max(Message.created_at).label("last_message"),
            func.count(Message.id).label("message_count"),
        ).where(Message.direction == "inbound")
        if date_from:
            q = q.where(Message.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            q = q.where(Message.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
        msg_rows = db.execute(
            q.group_by(Message.phone).order_by(func.max(Message.created_at).desc())
        ).all()

        lead_phones_norm = {normalize_phone(l.phone) for l in db.query(Lead.phone).all()}

        return [
            {
                "phone": r.phone,
                "name": r.name,
                "last_message": r.last_message.isoformat() if r.last_message else None,
                "message_count": r.message_count,
            }
            for r in msg_rows
            if normalize_phone(r.phone) not in lead_phones_norm
        ]
    finally:
        db.close()
