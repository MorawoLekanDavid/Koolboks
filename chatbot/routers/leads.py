from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import case, func, select

from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx, require_admin
from chatbot.models import ContactStage, ConversationTag, Lead, LeadNote, Message, Tag
from chatbot.routers.permissions import require_any_tab_permission, require_tab_permission
from chatbot.utils.phone import normalize_phone

router = APIRouter(prefix="/admin/leads", tags=["leads"])


@router.get("/by-phone/{phone}")
async def get_lead_by_phone(phone: str, ctx: dict = Depends(get_admin_ctx)):
    def _fetch():
        db = get_db()
        try:
            norm = normalize_phone(phone)
            lead = db.query(Lead).filter(Lead.phone == norm).first()
            if not lead:
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
                "assigned_to": lead.assigned_to,
                "status": lead.status or "new",
                "score": score, "interest": interest,
                "activity": {
                    "first_seen": activity.first_seen.isoformat() if activity and activity.first_seen else None,
                    "last_seen": activity.last_seen.isoformat() if activity and activity.last_seen else None,
                    "total_messages": activity.total_messages if activity else 0,
                },
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/{phone}/notes")
async def get_lead_notes(phone: str, ctx: dict = Depends(get_admin_ctx)):
    def _fetch():
        db = get_db()
        try:
            norm = normalize_phone(phone)
            notes = (
                db.query(LeadNote)
                .filter(LeadNote.lead_phone == norm)
                .order_by(LeadNote.created_at.desc())
                .all()
            )
            return [
                {"id": n.id, "content": n.content, "created_by": n.created_by,
                 "created_at": n.created_at.isoformat()}
                for n in notes
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


class LeadCreateIn(BaseModel):
    name: str
    phone: str
    product_interest: Optional[str] = None
    amount: Optional[str] = None
    payment_plan: Optional[str] = None
    address: Optional[str] = None
    status: str = "new"
    tag_id: Optional[int] = None  # auto-tag after creation


class LeadImportItem(BaseModel):
    name: str
    phone: str
    product_interest: Optional[str] = None
    amount: Optional[str] = None
    payment_plan: Optional[str] = None
    address: Optional[str] = None


class LeadImportIn(BaseModel):
    contacts: list[LeadImportItem]
    tag_id: Optional[int] = None
    status: str = "new"


class AssignIn(BaseModel):
    assign_to: Optional[str] = None   # None = unassign


class StatusIn(BaseModel):
    status: str


class NoteIn(BaseModel):
    content: str
    created_by: Optional[str] = "Agent"


@router.post("/{phone}/notes")
async def add_lead_note(phone: str, body: NoteIn, ctx: dict = Depends(get_admin_ctx)):
    if not body.content.strip():
        raise HTTPException(400, "Note content cannot be empty")
    def _create():
        db = get_db()
        try:
            norm = normalize_phone(phone)
            note = LeadNote(
                lead_phone=norm,
                content=body.content.strip(),
                created_by=body.created_by or ctx.get("name") or ctx.get("email", "Agent"),
            )
            db.add(note)
            db.commit()
            db.refresh(note)
            return {"id": note.id, "content": note.content, "created_by": note.created_by,
                    "created_at": note.created_at.isoformat()}
        finally:
            db.close()
    return await run_in_threadpool(_create)


@router.post("")
async def create_lead(body: LeadCreateIn, ctx: dict = Depends(require_tab_permission("contacts"))):
    def _create():
        db = get_db()
        try:
            norm = normalize_phone(body.phone)
            existing = db.query(Lead).filter(Lead.phone == norm).first()
            if existing:
                raise HTTPException(409, f"Contact with phone {norm} already exists")
            lead = Lead(
                name=body.name.strip(),
                phone=norm,
                product_interest=body.product_interest or None,
                amount=body.amount or None,
                payment_plan=body.payment_plan or None,
                address=body.address or None,
                status=body.status,
                source="manual",
                created_by=ctx.get("name") or ctx.get("email") or "Agent",
            )
            db.add(lead)
            db.flush()
            if body.tag_id:
                tag = db.query(Tag).filter(Tag.id == body.tag_id).first()
                if tag:
                    existing_tag = db.query(ConversationTag).filter(
                        ConversationTag.phone == norm,
                        ConversationTag.tag_id == body.tag_id,
                    ).first()
                    if not existing_tag:
                        db.add(ConversationTag(phone=norm, tag_id=body.tag_id, tagged_by=ctx.get("name", "Agent")))
            db.commit()
            return {"ok": True, "phone": norm, "name": lead.name}
        finally:
            db.close()
    return await run_in_threadpool(_create)


@router.post("/import")
async def import_leads(body: LeadImportIn, ctx: dict = Depends(require_tab_permission("contacts"))):
    def _import():
        db = get_db()
        try:
            created, skipped = 0, 0
            tag = None
            if body.tag_id:
                tag = db.query(Tag).filter(Tag.id == body.tag_id).first()
            agent_name = ctx.get("name", "Agent")
            for item in body.contacts:
                if not item.phone or not item.name:
                    skipped += 1
                    continue
                try:
                    norm = normalize_phone(item.phone)
                except Exception:
                    skipped += 1
                    continue
                existing = db.query(Lead).filter(Lead.phone == norm).first()
                if existing:
                    skipped += 1
                else:
                    lead = Lead(
                        name=item.name.strip(),
                        phone=norm,
                        product_interest=item.product_interest or None,
                        amount=item.amount or None,
                        payment_plan=item.payment_plan or None,
                        address=item.address or None,
                        status=body.status,
                        source="import",
                        created_by=agent_name,
                    )
                    db.add(lead)
                    db.flush()
                    if tag:
                        existing_tag = db.query(ConversationTag).filter(
                            ConversationTag.phone == norm,
                            ConversationTag.tag_id == body.tag_id,
                        ).first()
                        if not existing_tag:
                            db.add(ConversationTag(phone=norm, tag_id=body.tag_id, tagged_by=agent_name))
                    created += 1
            db.commit()
            return {"created": created, "skipped": skipped}
        finally:
            db.close()
    return await run_in_threadpool(_import)


@router.get("/by-tag/{tag_id}")
async def leads_by_tag(tag_id: int, ctx: dict = Depends(require_tab_permission("contacts"))):
    def _fetch():
        db = get_db()
        try:
            tagged_phones = {
                r.phone for r in db.query(ConversationTag.phone)
                .filter(ConversationTag.tag_id == tag_id).all()
            }
            if not tagged_phones:
                return []
            leads = (
                db.query(Lead)
                .filter(Lead.phone.in_(tagged_phones), Lead.phone != None, Lead.phone != "")
                .order_by(Lead.created_at.desc())
                .all()
            )
            return [
                {
                    "id": l.id, "name": l.name or "", "phone": l.phone,
                    "product_interest": l.product_interest or "",
                    "amount": l.amount or "",
                    "payment_plan": l.payment_plan or "",
                    "address": l.address or "",
                    "status": l.status or "new",
                    "source": getattr(l, "source", "bot"),
                }
                for l in leads
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("")
async def list_leads(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_tab_permission("leads")),
):
    def _fetch():
        db = get_db()
        try:
            q = db.query(Lead).filter(Lead.phone != None, Lead.phone != "")
            if date_from:
                q = q.filter(Lead.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                q = q.filter(Lead.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            leads = q.order_by(Lead.created_at.desc()).all()
            return [
                {
                    "id": l.id, "name": l.name, "phone": l.phone,
                    "whatsapp_phone": l.whatsapp_phone,
                    "product_interest": l.product_interest,
                    "business": l.business, "amount": l.amount,
                    "created_at": l.created_at.isoformat() if l.created_at else None,
                    "assigned_to": l.assigned_to,
                    "status": l.status or "new",
                }
                for l in leads
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/dropoffs")
async def list_dropoffs(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_tab_permission("leads")),
):
    def _fetch():
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
            lead_phones_norm = set()
            for l in db.query(Lead.phone, Lead.whatsapp_phone).all():
                if l.phone:
                    lead_phones_norm.add(normalize_phone(l.phone))
                if l.whatsapp_phone:
                    lead_phones_norm.add(normalize_phone(l.whatsapp_phone))
            return [
                {
                    "phone": r.phone, "name": r.name,
                    "last_message": r.last_message.isoformat() if r.last_message else None,
                    "message_count": r.message_count,
                }
                for r in msg_rows
                if normalize_phone(r.phone) not in lead_phones_norm
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.patch("/{phone}/assign")
async def assign_lead(phone: str, body: AssignIn, ctx: dict = Depends(get_admin_ctx)):
    def _update():
        db = get_db()
        try:
            norm = normalize_phone(phone)
            lead = db.query(Lead).filter(Lead.phone == norm).first()
            if not lead:
                raise HTTPException(404, "Lead not found")
            lead.assigned_to = body.assign_to or None
            db.commit()
            return {"ok": True, "assigned_to": lead.assigned_to}
        finally:
            db.close()
    return await run_in_threadpool(_update)


@router.patch("/{phone}/status")
async def update_lead_status(phone: str, body: StatusIn, ctx: dict = Depends(get_admin_ctx)):
    valid = {"new", "interested", "follow_up", "drop_off", "converted"}
    if body.status not in valid:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(sorted(valid))}")
    def _update():
        db = get_db()
        try:
            norm = normalize_phone(phone)
            lead = db.query(Lead).filter(Lead.phone == norm).first()
            if not lead:
                raise HTTPException(404, "Lead not found")
            lead.status = body.status
            db.commit()
            return {"ok": True, "status": lead.status}
        finally:
            db.close()
    return await run_in_threadpool(_update)


# ── Contacts router (source = manual | import) ────────────────────────────────
contacts_router = APIRouter(prefix="/admin/contacts", tags=["contacts"])


class ContactUpdateIn(BaseModel):
    name: str
    product_interest: Optional[str] = None
    amount: Optional[str] = None
    payment_plan: Optional[str] = None
    address: Optional[str] = None


def _actor(ctx: dict) -> str:
    return ctx.get("name") or ctx.get("email") or "Agent"


@contacts_router.get("")
async def list_contacts(ctx: dict = Depends(require_any_tab_permission(["contacts", "templates"]))):
    def _fetch():
        db = get_db()
        try:
            leads_list = (
                db.query(Lead)
                .filter(Lead.source.in_(["manual", "import"]))
                .order_by(Lead.created_at.desc())
                .all()
            )
            phones = [l.phone for l in leads_list]
            tag_rows = (
                db.query(ConversationTag, Tag)
                .join(Tag, ConversationTag.tag_id == Tag.id)
                .filter(ConversationTag.phone.in_(phones))
                .all()
            ) if phones else []
            phone_tags: dict = {}
            for ct, tag in tag_rows:
                phone_tags.setdefault(ct.phone, []).append(
                    {"id": tag.id, "name": tag.name, "color": tag.color}
                )

            # Batched response-tracking: last outbound send time + whether
            # a later inbound message came in (i.e. the contact responded).
            outbound_map, inbound_map = {}, {}
            if phones:
                outbound_map = {
                    r.phone: r.last_at
                    for r in db.execute(
                        select(Message.phone, func.max(Message.created_at).label("last_at"))
                        .where(Message.phone.in_(phones), Message.direction == "outbound")
                        .group_by(Message.phone)
                    ).all()
                }
                inbound_map = {
                    r.phone: r.last_at
                    for r in db.execute(
                        select(Message.phone, func.max(Message.created_at).label("last_at"))
                        .where(Message.phone.in_(phones), Message.direction == "inbound")
                        .group_by(Message.phone)
                    ).all()
                }

            result = []
            for l in leads_list:
                last_out = outbound_map.get(l.phone)
                last_in = inbound_map.get(l.phone)
                responded_at = last_in if (last_out and last_in and last_in > last_out) else None
                result.append({
                    "id": l.id,
                    "name": l.name or "",
                    "phone": l.phone,
                    "product_interest": l.product_interest or "",
                    "amount": l.amount or "",
                    "payment_plan": l.payment_plan or "",
                    "address": l.address or "",
                    "status": l.status or "new",
                    "source": l.source or "manual",
                    "outreach_stage": l.outreach_stage or "not_contacted",
                    "owner": l.assigned_to,
                    "created_at": l.created_at.isoformat() if l.created_at else None,
                    "created_by": l.created_by,
                    "updated_at": l.updated_at.isoformat() if l.updated_at else None,
                    "updated_by": l.updated_by,
                    "last_outbound_at": last_out.isoformat() if last_out else None,
                    "responded_at": responded_at.isoformat() if responded_at else None,
                    "tags": phone_tags.get(l.phone, []),
                })
            return result
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


# ── Customizable outreach stages ───────────────────────────────────────────────

def _default_stage_seed(db):
    """Seed the original 5 stages the first time this table is queried empty."""
    if db.query(ContactStage).count() > 0:
        return
    defaults = [
        ("not_contacted", "#94A3B8", 0),
        ("contacted", "#3B82F6", 1),
        ("responded", "#F59E0B", 2),
        ("converted", "#22C55E", 3),
        ("dead", "#EF4444", 4),
    ]
    for name, color, order in defaults:
        db.add(ContactStage(name=name, color=color, sort_order=order))
    db.commit()


@contacts_router.get("/stages")
async def list_contact_stages(ctx: dict = Depends(require_any_tab_permission(["contacts", "templates"]))):
    def _fetch():
        db = get_db()
        try:
            _default_stage_seed(db)
            rows = db.query(ContactStage).order_by(ContactStage.sort_order, ContactStage.id).all()
            return [{"id": s.id, "name": s.name, "color": s.color, "sort_order": s.sort_order} for s in rows]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


class StageCreateIn(BaseModel):
    name: str
    color: str = "#6366f1"


@contacts_router.post("/stages")
async def create_contact_stage(body: StageCreateIn, ctx: dict = Depends(require_admin)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Stage name is required")

    def _create():
        db = get_db()
        try:
            _default_stage_seed(db)
            existing = db.query(ContactStage).filter(ContactStage.name == name).first()
            if existing:
                raise HTTPException(409, "A stage with this name already exists")
            max_order = db.query(func.max(ContactStage.sort_order)).scalar() or 0
            stage = ContactStage(name=name, color=body.color or "#6366f1", sort_order=max_order + 1)
            db.add(stage)
            db.commit()
            db.refresh(stage)
            return {"id": stage.id, "name": stage.name, "color": stage.color, "sort_order": stage.sort_order}
        finally:
            db.close()
    return await run_in_threadpool(_create)


class StageUpdateIn(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None


@contacts_router.patch("/stages/{stage_id}")
async def update_contact_stage_def(stage_id: int, body: StageUpdateIn, ctx: dict = Depends(require_admin)):
    def _update():
        db = get_db()
        try:
            stage = db.query(ContactStage).filter(ContactStage.id == stage_id).first()
            if not stage:
                raise HTTPException(404, "Stage not found")
            old_name = stage.name
            if body.name is not None:
                new_name = body.name.strip()
                if not new_name:
                    raise HTTPException(400, "Stage name cannot be empty")
                if new_name != old_name:
                    clash = db.query(ContactStage).filter(ContactStage.name == new_name).first()
                    if clash:
                        raise HTTPException(409, "A stage with this name already exists")
                    # Cascade the rename onto every contact currently in this stage
                    db.query(Lead).filter(Lead.outreach_stage == old_name).update(
                        {"outreach_stage": new_name}, synchronize_session=False
                    )
                    stage.name = new_name
            if body.color is not None:
                stage.color = body.color
            if body.sort_order is not None:
                stage.sort_order = body.sort_order
            db.commit()
            return {"id": stage.id, "name": stage.name, "color": stage.color, "sort_order": stage.sort_order}
        finally:
            db.close()
    return await run_in_threadpool(_update)


@contacts_router.delete("/stages/{stage_id}")
async def delete_contact_stage(stage_id: int, ctx: dict = Depends(require_admin)):
    def _delete():
        db = get_db()
        try:
            stage = db.query(ContactStage).filter(ContactStage.id == stage_id).first()
            if not stage:
                raise HTTPException(404, "Stage not found")
            in_use = (
                db.query(Lead)
                .filter(Lead.outreach_stage == stage.name, Lead.source.in_(["manual", "import"]))
                .count()
            )
            if in_use:
                raise HTTPException(400, f"{in_use} contact(s) are in this stage — move them first")
            if db.query(ContactStage).count() <= 1:
                raise HTTPException(400, "At least one stage must remain")
            db.delete(stage)
            db.commit()
            return {"ok": True}
        finally:
            db.close()
    return await run_in_threadpool(_delete)


class ContactStageIn(BaseModel):
    stage: str


@contacts_router.patch("/{phone}/stage")
async def update_contact_stage(phone: str, body: ContactStageIn, ctx: dict = Depends(require_tab_permission("contacts"))):
    def _update():
        db = get_db()
        try:
            _default_stage_seed(db)
            valid_names = {s.name for s in db.query(ContactStage.name).all()}
            if body.stage not in valid_names:
                raise HTTPException(400, f"Invalid stage. Must be one of: {', '.join(sorted(valid_names))}")
            norm = normalize_phone(phone)
            lead = db.query(Lead).filter(Lead.phone == norm).first()
            if not lead:
                raise HTTPException(404, "Contact not found")
            if lead.source not in ("manual", "import"):
                raise HTTPException(400, "Cannot edit bot-qualified leads from the Contacts tab")
            lead.outreach_stage = body.stage
            lead.updated_by = _actor(ctx)
            db.commit()
            return {"ok": True, "outreach_stage": lead.outreach_stage}
        finally:
            db.close()
    return await run_in_threadpool(_update)


class BulkDeleteIn(BaseModel):
    phones: List[str]


@contacts_router.post("/bulk-delete")
async def bulk_delete_contacts(body: BulkDeleteIn, ctx: dict = Depends(require_tab_permission("contacts"))):
    def _delete():
        db = get_db()
        try:
            norm_phones = [normalize_phone(p) for p in body.phones]
            leads_list = (
                db.query(Lead)
                .filter(Lead.phone.in_(norm_phones), Lead.source.in_(["manual", "import"]))
                .all()
            )
            deleted_phones = [l.phone for l in leads_list]
            if deleted_phones:
                db.query(ConversationTag).filter(ConversationTag.phone.in_(deleted_phones)).delete(
                    synchronize_session=False
                )
                for l in leads_list:
                    db.delete(l)
                db.commit()
            return {"ok": True, "deleted": len(deleted_phones)}
        finally:
            db.close()
    return await run_in_threadpool(_delete)


@contacts_router.patch("/{phone}")
async def update_contact(phone: str, body: ContactUpdateIn, ctx: dict = Depends(require_tab_permission("contacts"))):
    def _update():
        db = get_db()
        try:
            norm = normalize_phone(phone)
            lead = db.query(Lead).filter(Lead.phone == norm).first()
            if not lead:
                raise HTTPException(404, "Contact not found")
            if lead.source not in ("manual", "import"):
                raise HTTPException(400, "Cannot edit bot-qualified leads from the Contacts tab")
            lead.name = body.name.strip()
            lead.product_interest = body.product_interest or None
            lead.amount = body.amount or None
            lead.payment_plan = body.payment_plan or None
            lead.address = body.address or None
            lead.updated_by = _actor(ctx)
            db.commit()
            return {"ok": True}
        finally:
            db.close()
    return await run_in_threadpool(_update)


class ContactOwnerIn(BaseModel):
    owner: Optional[str] = None  # None = unassign


@contacts_router.patch("/{phone}/owner")
async def update_contact_owner(phone: str, body: ContactOwnerIn, ctx: dict = Depends(require_tab_permission("contacts"))):
    def _update():
        db = get_db()
        try:
            norm = normalize_phone(phone)
            lead = db.query(Lead).filter(Lead.phone == norm).first()
            if not lead:
                raise HTTPException(404, "Contact not found")
            if lead.source not in ("manual", "import"):
                raise HTTPException(400, "Cannot edit bot-qualified leads from the Contacts tab")
            lead.assigned_to = body.owner or None
            lead.updated_by = _actor(ctx)
            db.commit()
            return {"ok": True, "owner": lead.assigned_to}
        finally:
            db.close()
    return await run_in_threadpool(_update)


@contacts_router.delete("/{phone}")
async def delete_contact(phone: str, ctx: dict = Depends(require_tab_permission("contacts"))):
    def _delete():
        db = get_db()
        try:
            norm = normalize_phone(phone)
            lead = db.query(Lead).filter(Lead.phone == norm).first()
            if not lead:
                raise HTTPException(404, "Contact not found")
            if lead.source not in ("manual", "import"):
                raise HTTPException(400, "Cannot delete bot-qualified leads from the Contacts tab")
            db.query(ConversationTag).filter(ConversationTag.phone == norm).delete()
            db.delete(lead)
            db.commit()
            return {"ok": True}
        finally:
            db.close()
    return await run_in_threadpool(_delete)
