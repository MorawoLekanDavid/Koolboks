import asyncio
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, case, func, select

from chatbot.config import (
    WHATSAPP_API_TOKEN,
    WHATSAPP_API_URL,
    WHATSAPP_PHONE_NUMBER_ID,
    log,
)
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx
from chatbot.models import CannedResponse, Message
from chatbot.services.whatsapp_service import save_message_db, send_whatsapp_message
from chatbot.utils.phone import normalize_phone

router = APIRouter(prefix="/admin", tags=["conversations"])


@router.get("/conversations")
async def list_conversations(ctx: dict = Depends(get_admin_ctx)):
    db = get_db()
    try:
        rows = db.execute(
            select(
                Message.phone,
                func.max(
                    case((Message.direction == "inbound", Message.name), else_=None)
                ).label("name"),
                func.max(Message.created_at).label("last_message"),
                func.count(Message.id).label("total")
            ).group_by(Message.phone)
            .order_by(func.max(Message.created_at).desc())
        ).all()
        # Build phone → agents-involved map in one query
        agent_rows = db.execute(
            select(Message.phone, Message.name)
            .where(and_(
                Message.direction == "outbound",
                Message.name != "KoolBot",
                Message.name.isnot(None),
                Message.name != "",
            ))
            .distinct()
        ).all()
        agent_map: dict = {}
        for ar in agent_rows:
            agent_map.setdefault(ar.phone, [])
            if ar.name not in agent_map[ar.phone]:
                agent_map[ar.phone].append(ar.name)

        phones = [r.phone for r in rows]

        # Batch all Redis calls — one mget for handoffs, one for read timestamps
        if redis_client.client and phones:
            handoff_keys = [f"koolbuy:handoff:wa_{p}" for p in phones]
            read_keys = [f"koolbuy:conv_read:{p}" for p in phones]
            handoff_vals, read_vals = await asyncio.gather(
                redis_client.client.mget(*handoff_keys),
                redis_client.client.mget(*read_keys),
            )
            handoff_map = {p: v for p, v in zip(phones, handoff_vals)}
            read_map = {p: v for p, v in zip(phones, read_vals)}
        else:
            handoff_map = {}
            read_map = {}

        # Batch unread counts — one query for all phones
        inbound_totals = {r2.phone: r2.total for r2 in db.execute(
            select(Message.phone, func.count(Message.id).label("total"))
            .where(and_(Message.phone.in_(phones), Message.direction == "inbound"))
            .group_by(Message.phone)
        ).all()} if phones else {}

        result = []
        for r in rows:
            handoff = handoff_map.get(r.phone)
            last_read = read_map.get(r.phone)
            total_in = inbound_totals.get(r.phone, 0)
            if last_read:
                try:
                    lrdt = datetime.fromisoformat(last_read)
                    unread = db.execute(
                        select(func.count(Message.id)).where(and_(
                            Message.phone == r.phone,
                            Message.direction == "inbound",
                            Message.created_at > lrdt,
                        ))
                    ).scalar() or 0
                except Exception:
                    unread = total_in
            else:
                unread = total_in
            result.append({
                "phone": r.phone,
                "name": r.name,
                "last_message": r.last_message.isoformat() if r.last_message else None,
                "total_messages": r.total,
                "mode": "agent" if handoff else "bot",
                "agent": handoff if handoff and handoff != "1" else None,
                "unread": unread,
                "agents_involved": agent_map.get(r.phone, []),
            })
        return result
    finally:
        db.close()


@router.get("/conversations/{phone}")
async def get_conversation(phone: str, ctx: dict = Depends(get_admin_ctx)):
    db = get_db()
    try:
        rows = db.execute(
            select(Message).where(Message.phone == phone)
            .order_by(Message.created_at.asc())
        ).scalars().all()
        return [{"id": m.id, "direction": m.direction, "content": m.content, "timestamp": m.created_at.isoformat(), "name": m.name} for m in rows]
    finally:
        db.close()


@router.post("/conversations/{phone}/mark-read")
async def mark_conversation_read(phone: str, ctx: dict = Depends(get_admin_ctx)):
    if redis_client.client:
        await redis_client.client.set(f"koolbuy:conv_read:{phone}", datetime.utcnow().isoformat(), ex=86400 * 7)
    return {"status": "ok"}


class CannedRequest(BaseModel):
    title: str
    content: str


@router.get("/canned-responses")
async def list_canned(ctx: dict = Depends(get_admin_ctx)):
    db = get_db()
    try:
        rows = db.query(CannedResponse).order_by(CannedResponse.created_at.asc()).all()
        return [{"id": r.id, "title": r.title, "content": r.content, "created_by": r.created_by} for r in rows]
    finally:
        db.close()


@router.post("/canned-responses")
async def create_canned(body: CannedRequest, ctx: dict = Depends(get_admin_ctx)):
    if not body.title.strip() or not body.content.strip():
        raise HTTPException(400, "Title and content required")
    db = get_db()
    try:
        row = CannedResponse(title=body.title.strip(), content=body.content.strip(), created_by=ctx.get("name", "Agent"))
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"id": row.id, "title": row.title, "content": row.content, "created_by": row.created_by}
    finally:
        db.close()


@router.patch("/canned-responses/{canned_id}")
async def update_canned(canned_id: int, body: CannedRequest, ctx: dict = Depends(get_admin_ctx)):
    if not body.title.strip() or not body.content.strip():
        raise HTTPException(400, "Title and content required")
    db = get_db()
    try:
        row = db.query(CannedResponse).filter(CannedResponse.id == canned_id).first()
        if not row:
            raise HTTPException(404, "Not found")
        row.title = body.title.strip()
        row.content = body.content.strip()
        db.commit()
        return {"id": row.id, "title": row.title, "content": row.content, "created_by": row.created_by}
    finally:
        db.close()


@router.delete("/canned-responses/{canned_id}")
async def delete_canned(canned_id: int, ctx: dict = Depends(get_admin_ctx)):
    db = get_db()
    try:
        row = db.query(CannedResponse).filter(CannedResponse.id == canned_id).first()
        if not row:
            raise HTTPException(404, "Not found")
        db.delete(row)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


class AgentReply(BaseModel):
    message: str
    agent_name: str = "Agent"
    image_url: Optional[str] = None


@router.post("/conversations/{phone}/reply")
async def agent_reply(phone: str, body: AgentReply, ctx: dict = Depends(get_admin_ctx)):
    session_id = f"wa_{phone}"
    display_name = ctx.get("name") or body.agent_name or "Agent"

    # Send product image as its own WhatsApp message, saved separately so dashboard shows it
    if body.image_url:
        try:
            img_payload = {
                "messaging_product": "whatsapp", "to": normalize_phone(phone).lstrip('+'),
                "type": "image", "image": {"link": body.image_url}
            }
            async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as _c:
                ir = await _c.post(
                    f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                    json=img_payload,
                    headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
                )
            img_wamid = ir.json()["messages"][0]["id"] if ir.is_success else None
            save_message_db(session_id, phone, display_name, "outbound",
                            f"[image]{body.image_url}[/image]", wamid=img_wamid)
        except Exception as e:
            log.warning(f"Agent image send error: {e}")

    wamid = await send_whatsapp_message(phone, body.message)
    save_message_db(session_id, phone, display_name, "outbound", body.message, wamid=wamid)

    # Keep Redis history in sync so the bot has full context when it resumes
    if redis_client.client:
        history = await redis_client.get_history(session_id)
        history.append({
            "role": "assistant",
            "content": f"[Agent {display_name}]: {body.message}",
            "ts": datetime.now().isoformat(),
        })
        await redis_client.save_history(session_id, history)

    return {"status": "sent"}


class HandoffRequest(BaseModel):
    agent_name: str = "Agent"


@router.post("/handoff/{phone}")
async def toggle_handoff(phone: str, body: HandoffRequest = HandoffRequest(), ctx: dict = Depends(get_admin_ctx)):
    if not redis_client.client:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    session_id = f"wa_{normalize_phone(phone)}"
    handoff_key = f"koolbuy:handoff:{session_id}"
    current = await redis_client.client.get(handoff_key)
    agent_display = ctx.get("name") or body.agent_name or "Agent"
    if current:
        await redis_client.client.delete(handoff_key)
        mode = "bot"
        agent = None
    else:
        await redis_client.client.set(handoff_key, agent_display, ex=86400)
        mode = "agent"
        agent = agent_display
    log.info(f"Handoff toggled for {phone}: now {mode} ({agent})")
    return {"phone": phone, "mode": mode, "agent": agent}
