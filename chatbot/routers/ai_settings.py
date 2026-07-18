from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from chatbot.config import GROQ_MODEL, log
from chatbot.database import get_db
from chatbot.dependencies import require_admin, require_super_admin
from chatbot.models import AIInstruction, KBDocument
from chatbot.services.ai_settings_service import get_draft_content, invalidate_cache
from chatbot.services.groq_service import groq_client
from chatbot.utils.file_parser import extract_text

router = APIRouter(prefix="/admin/ai-settings", tags=["ai-settings"])


# ── AI Instructions ───────────────────────────────────────────────────────────

@router.get("/instructions")
async def get_instructions(ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        live = (
            db.query(AIInstruction)
            .filter(AIInstruction.status == "live")
            .order_by(AIInstruction.created_at.desc())
            .first()
        )
        draft = (
            db.query(AIInstruction)
            .filter(AIInstruction.status == "draft")
            .order_by(AIInstruction.created_at.desc())
            .first()
        )
        history = (
            db.query(AIInstruction)
            .filter(AIInstruction.status == "archived")
            .order_by(AIInstruction.created_at.desc())
            .limit(20)
            .all()
        )
        return {
            "live":    _fmt_inst(live),
            "draft":   _fmt_inst(draft),
            "history": [_fmt_inst(h) for h in history],
            "has_draft": draft is not None,
        }
    finally:
        db.close()


class InstructionIn(BaseModel):
    content: str


@router.post("/instructions/draft")
async def save_draft_instruction(body: InstructionIn, ctx: dict = Depends(require_admin)):
    if not body.content.strip():
        raise HTTPException(400, "Instruction content cannot be empty")
    db = get_db()
    try:
        # Replace any existing draft
        db.query(AIInstruction).filter(AIInstruction.status == "draft").delete()
        live = (
            db.query(AIInstruction)
            .filter(AIInstruction.status == "live")
            .order_by(AIInstruction.created_at.desc())
            .first()
        )
        next_version = (live.version + 1) if live else 1
        draft = AIInstruction(
            content=body.content.strip(),
            status="draft",
            version=next_version,
            created_by=ctx.get("name") or ctx.get("email", "admin"),
        )
        db.add(draft)
        db.commit()
        db.refresh(draft)
        await invalidate_cache()
        return _fmt_inst(draft)
    finally:
        db.close()


@router.post("/instructions/go-live")
async def publish_instruction(ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        draft = (
            db.query(AIInstruction)
            .filter(AIInstruction.status == "draft")
            .order_by(AIInstruction.created_at.desc())
            .first()
        )
        if not draft:
            raise HTTPException(400, "No draft instruction to publish")

        # Archive current live
        db.query(AIInstruction).filter(AIInstruction.status == "live").update({"status": "archived"})
        draft.status = "live"
        db.commit()
        db.refresh(draft)
        await invalidate_cache()
        log.info(f"AI instruction v{draft.version} published by {ctx.get('name')}")
        return _fmt_inst(draft)
    finally:
        db.close()


@router.post("/instructions/{inst_id}/restore")
async def restore_instruction(inst_id: int, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        inst = db.query(AIInstruction).filter(AIInstruction.id == inst_id).first()
        if not inst:
            raise HTTPException(404, "Instruction version not found")

        # Save current live as archived, make this the new draft for review
        db.query(AIInstruction).filter(AIInstruction.status == "draft").delete()
        live = (
            db.query(AIInstruction)
            .filter(AIInstruction.status == "live")
            .order_by(AIInstruction.created_at.desc())
            .first()
        )
        next_version = (live.version + 1) if live else 1
        restored = AIInstruction(
            content=inst.content,
            status="draft",
            version=next_version,
            created_by=ctx.get("name") or ctx.get("email", "admin"),
        )
        db.add(restored)
        db.commit()
        db.refresh(restored)
        await invalidate_cache()
        return {"message": "Restored as draft — review and click Go Live to publish", "draft": _fmt_inst(restored)}
    finally:
        db.close()


@router.delete("/instructions/{inst_id}")
async def delete_archived_instruction(inst_id: int, ctx: dict = Depends(require_super_admin)):
    db = get_db()
    try:
        inst = db.query(AIInstruction).filter(AIInstruction.id == inst_id).first()
        if not inst:
            raise HTTPException(404, "Not found")
        if inst.status == "live":
            raise HTTPException(400, "Cannot delete the live instruction")
        db.delete(inst)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


# ── Knowledge Base Documents ──────────────────────────────────────────────────

@router.get("/kb")
async def get_kb_documents(ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        docs = db.query(KBDocument).order_by(KBDocument.created_at.asc()).all()
        return {
            "live":    [_fmt_doc(d) for d in docs if d.status == "live"],
            "draft":   [_fmt_doc(d) for d in docs if d.status == "draft"],
            "pending_trash": [_fmt_doc(d) for d in docs if d.status == "pending_trash"],
            "trashed": [_fmt_doc(d) for d in docs if d.status == "trashed"],
            "has_pending": any(d.status in ("draft", "pending_trash") for d in docs),
        }
    finally:
        db.close()


@router.post("/kb/upload")
async def upload_kb_document(
    file: UploadFile = File(...),
    ctx: dict = Depends(require_admin),
):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:  # 10 MB cap
        raise HTTPException(400, "File too large (max 10 MB)")
    try:
        file_type, text = extract_text(file.filename or "upload.txt", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not text.strip():
        raise HTTPException(400, "Could not extract any text from this file")

    db = get_db()
    try:
        doc = KBDocument(
            name=file.filename,
            content=text.strip(),
            file_type=file_type,
            file_size=len(data),
            status="draft",
            created_by=ctx.get("name") or ctx.get("email", "admin"),
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        await invalidate_cache()
        return _fmt_doc(doc)
    finally:
        db.close()


@router.get("/kb/{doc_id}")
async def get_kb_document(doc_id: int, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        doc = db.query(KBDocument).filter(KBDocument.id == doc_id).first()
        if not doc:
            raise HTTPException(404, "Document not found")
        return {
            "id": doc.id,
            "name": doc.name,
            "content": doc.content,
            "file_type": doc.file_type,
            "status": doc.status,
        }
    finally:
        db.close()


@router.patch("/kb/{doc_id}/trash")
async def mark_kb_trash(doc_id: int, ctx: dict = Depends(require_admin)):
    """Mark a live doc for removal (applied on Go Live) or delete a draft immediately."""
    db = get_db()
    try:
        doc = db.query(KBDocument).filter(KBDocument.id == doc_id).first()
        if not doc:
            raise HTTPException(404, "Document not found")
        if doc.status == "draft":
            db.delete(doc)
        elif doc.status == "live":
            doc.status = "pending_trash"
        else:
            raise HTTPException(400, f"Cannot trash a document with status '{doc.status}'")
        db.commit()
        await invalidate_cache()
        return {"status": "ok"}
    finally:
        db.close()


@router.patch("/kb/{doc_id}/restore")
async def restore_kb_document(doc_id: int, ctx: dict = Depends(require_admin)):
    """Restore a trashed or pending_trash document back to live."""
    db = get_db()
    try:
        doc = db.query(KBDocument).filter(KBDocument.id == doc_id).first()
        if not doc:
            raise HTTPException(404, "Document not found")
        if doc.status not in ("trashed", "pending_trash"):
            raise HTTPException(400, "Only trashed documents can be restored")
        doc.status = "live"
        db.commit()
        db.refresh(doc)
        await invalidate_cache()
        return _fmt_doc(doc)
    finally:
        db.close()


@router.delete("/kb/{doc_id}")
async def delete_kb_document_permanently(doc_id: int, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        doc = db.query(KBDocument).filter(KBDocument.id == doc_id).first()
        if not doc:
            raise HTTPException(404, "Document not found")
        if doc.status == "live":
            raise HTTPException(400, "Move to trash before permanently deleting")
        db.delete(doc)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


# ── Go Live (KB + Instructions together) ─────────────────────────────────────

@router.post("/go-live")
async def go_live(ctx: dict = Depends(require_admin)):
    """Publish all pending KB changes and draft instruction in one action."""
    db = get_db()
    changes = []
    try:
        # Publish draft instruction if one exists
        draft_inst = (
            db.query(AIInstruction)
            .filter(AIInstruction.status == "draft")
            .order_by(AIInstruction.created_at.desc())
            .first()
        )
        if draft_inst:
            db.query(AIInstruction).filter(AIInstruction.status == "live").update({"status": "archived"})
            draft_inst.status = "live"
            changes.append("AI instruction published")

        # Promote draft KB docs to live
        draft_docs = db.query(KBDocument).filter(KBDocument.status == "draft").all()
        for d in draft_docs:
            d.status = "live"
        if draft_docs:
            changes.append(f"{len(draft_docs)} KB document(s) published")

        # Move pending_trash docs to trashed
        trash_docs = db.query(KBDocument).filter(KBDocument.status == "pending_trash").all()
        for d in trash_docs:
            d.status = "trashed"
        if trash_docs:
            changes.append(f"{len(trash_docs)} KB document(s) removed")

        if not changes:
            raise HTTPException(400, "No pending changes to publish")

        db.commit()
        await invalidate_cache()
        log.info(f"Go Live by {ctx.get('name')}: {'; '.join(changes)}")
        return {"status": "live", "changes": changes}
    finally:
        db.close()


# ── Test Chat ─────────────────────────────────────────────────────────────────

class TestChatMessage(BaseModel):
    message: str
    history: Optional[list] = []


@router.post("/test-chat")
async def test_chat(body: TestChatMessage, ctx: dict = Depends(require_admin)):
    if not body.message.strip():
        raise HTTPException(400, "Message cannot be empty")

    instruction, kb = await get_draft_content()
    system_content = instruction.replace("{knowledge_base}", kb).replace("{user_name}", "Tester").replace("{inventory}", "")

    messages = [{"role": "system", "content": system_content}]
    for m in (body.history or [])[-10:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": body.message.strip()})

    try:
        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=500,
            temperature=0.4,
        )
        reply = completion.choices[0].message.content or "No response"
        return {"reply": reply}
    except Exception as e:
        log.error(f"Test chat error: {e}")
        raise HTTPException(500, "AI error — check your API key and try again")


# ── Serialisers ───────────────────────────────────────────────────────────────

def _fmt_inst(inst: AIInstruction | None) -> dict | None:
    if not inst:
        return None
    return {
        "id": inst.id,
        "content": inst.content,
        "status": inst.status,
        "version": inst.version,
        "created_by": inst.created_by,
        "created_at": inst.created_at.isoformat() if inst.created_at else None,
    }


def _fmt_doc(doc: KBDocument) -> dict:
    return {
        "id": doc.id,
        "name": doc.name,
        "file_type": doc.file_type,
        "file_size": doc.file_size,
        "status": doc.status,
        "created_by": doc.created_by,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "preview": doc.content[:200] + ("…" if len(doc.content) > 200 else ""),
    }
