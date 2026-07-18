import json
import re
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from chatbot.config import GROQ_MODEL, log
from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx, require_admin
from chatbot.models import ConversationTag, Message, Tag
from chatbot.services.groq_service import groq_client

router = APIRouter(prefix="/admin", tags=["tags"])


class TagIn(BaseModel):
    name: str
    color: str = "#6366f1"


# ── Tag management (admin only for create/update/delete) ──────────────────────

@router.get("/tags")
async def list_tags(ctx: dict = Depends(get_admin_ctx)):
    def _fetch():
        db = get_db()
        try:
            tags = db.query(Tag).order_by(Tag.name).all()
            return [{"id": t.id, "name": t.name, "color": t.color} for t in tags]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.post("/tags")
async def create_tag(body: TagIn, ctx: dict = Depends(require_admin)):
    if not body.name.strip():
        raise HTTPException(400, "Tag name required")

    def _create():
        db = get_db()
        try:
            tag = Tag(
                name=body.name.strip()[:50],
                color=body.color or "#6366f1",
                created_by=ctx.get("name", "admin"),
            )
            db.add(tag)
            db.commit()
            db.refresh(tag)
            return {"id": tag.id, "name": tag.name, "color": tag.color}
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "A tag with this name already exists")
        finally:
            db.close()

    return await run_in_threadpool(_create)


@router.patch("/tags/{tag_id}")
async def update_tag(tag_id: int, body: TagIn, ctx: dict = Depends(require_admin)):
    def _update():
        db = get_db()
        try:
            tag = db.query(Tag).filter(Tag.id == tag_id).first()
            if not tag:
                raise HTTPException(404, "Tag not found")
            tag.name = body.name.strip()[:50]
            tag.color = body.color or tag.color
            db.commit()
            return {"id": tag.id, "name": tag.name, "color": tag.color}
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "A tag with this name already exists")
        finally:
            db.close()

    return await run_in_threadpool(_update)


@router.delete("/tags/{tag_id}")
async def delete_tag(tag_id: int, ctx: dict = Depends(require_admin)):
    def _delete():
        db = get_db()
        try:
            tag = db.query(Tag).filter(Tag.id == tag_id).first()
            if not tag:
                raise HTTPException(404, "Tag not found")
            db.delete(tag)
            db.commit()
            return {"status": "deleted"}
        finally:
            db.close()

    return await run_in_threadpool(_delete)


# ── Conversation tagging (all agents) ────────────────────────────────────────

@router.get("/conversations/{phone}/tags")
async def get_conversation_tags(phone: str, ctx: dict = Depends(get_admin_ctx)):
    def _fetch():
        db = get_db()
        try:
            rows = (
                db.query(ConversationTag, Tag)
                .join(Tag, ConversationTag.tag_id == Tag.id)
                .filter(ConversationTag.phone == phone)
                .all()
            )
            return [
                {"id": ct.id, "tag_id": t.id, "name": t.name, "color": t.color, "tagged_by": ct.tagged_by}
                for ct, t in rows
            ]
        finally:
            db.close()

    return await run_in_threadpool(_fetch)


@router.post("/conversations/{phone}/tags/{tag_id}")
async def add_conversation_tag(phone: str, tag_id: int, ctx: dict = Depends(get_admin_ctx)):
    agent = ctx.get("name", "Agent")

    def _add():
        db = get_db()
        try:
            tag = db.query(Tag).filter(Tag.id == tag_id).first()
            if not tag:
                raise HTTPException(404, "Tag not found")
            existing = (
                db.query(ConversationTag)
                .filter(ConversationTag.phone == phone, ConversationTag.tag_id == tag_id)
                .first()
            )
            if existing:
                return {"id": existing.id, "tag_id": tag.id, "name": tag.name, "color": tag.color, "tagged_by": existing.tagged_by}
            ct = ConversationTag(phone=phone, tag_id=tag_id, tagged_by=agent)
            db.add(ct)
            db.commit()
            db.refresh(ct)
            return {"id": ct.id, "tag_id": tag.id, "name": tag.name, "color": tag.color, "tagged_by": agent}
        finally:
            db.close()

    return await run_in_threadpool(_add)


@router.delete("/conversations/{phone}/tags/{tag_id}")
async def remove_conversation_tag(phone: str, tag_id: int, ctx: dict = Depends(get_admin_ctx)):
    def _remove():
        db = get_db()
        try:
            ct = (
                db.query(ConversationTag)
                .filter(ConversationTag.phone == phone, ConversationTag.tag_id == tag_id)
                .first()
            )
            if not ct:
                raise HTTPException(404, "Tag not on this conversation")
            db.delete(ct)
            db.commit()
            return {"status": "removed"}
        finally:
            db.close()

    return await run_in_threadpool(_remove)


# ── AI auto-tagging ───────────────────────────────────────────────────────────

@router.post("/conversations/{phone}/auto-tag")
async def auto_tag_conversation(phone: str, ctx: dict = Depends(get_admin_ctx)):
    def _fetch_data():
        db = get_db()
        try:
            msgs = db.execute(
                select(Message.direction, Message.content, Message.name)
                .where(Message.phone == phone)
                .order_by(Message.created_at.desc())
                .limit(40)
            ).all()
            tags = db.query(Tag).order_by(Tag.name).all()
            return list(reversed(msgs)), [{"id": t.id, "name": t.name} for t in tags]
        finally:
            db.close()

    msgs, all_tags = await run_in_threadpool(_fetch_data)

    if not all_tags:
        raise HTTPException(400, "No tags exist yet — create some tags first")
    if not msgs:
        raise HTTPException(400, "No messages in this conversation")

    tag_names = [t["name"] for t in all_tags]
    conv_text = "\n".join(
        f"{'Customer' if m.direction == 'inbound' else 'Agent/Bot'}: {m.content}"
        for m in msgs
    )

    prompt = (
        f"You are classifying a WhatsApp business conversation.\n"
        f"Select ALL tags that apply from this list: {json.dumps(tag_names)}\n"
        f"Reply with ONLY a JSON array of matching tag names, e.g. [\"Hot Lead\",\"Follow-up Needed\"]. "
        f"If none apply reply with [].\n\nConversation:\n{conv_text[:3000]}"
    )

    try:
        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.1,
        )
        raw = completion.choices[0].message.content.strip()
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        suggested: List[str] = json.loads(match.group()) if match else []
        suggested = [s for s in suggested if s in tag_names]
    except Exception as e:
        log.error(f"Auto-tag AI error: {e}")
        raise HTTPException(500, "AI error during auto-tagging")

    tag_map = {t["name"]: t["id"] for t in all_tags}
    applied: List[str] = []

    def _apply_tags():
        db = get_db()
        try:
            for name in suggested:
                tid = tag_map.get(name)
                if not tid:
                    continue
                exists = (
                    db.query(ConversationTag)
                    .filter(ConversationTag.phone == phone, ConversationTag.tag_id == tid)
                    .first()
                )
                if not exists:
                    db.add(ConversationTag(phone=phone, tag_id=tid, tagged_by="AI ✨"))
                    applied.append(name)
            db.commit()
        finally:
            db.close()

    await run_in_threadpool(_apply_tags)
    return {"applied": applied, "suggested": suggested}
