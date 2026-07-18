"""
Provides live and draft AI content (instruction + KB) for the bot and test-chat.
Results are cached in Redis so every WhatsApp message doesn't hit the DB.
Cache is invalidated whenever Go Live is called.
"""
import json

from chatbot.config import KNOWLEDGE_BASE, SYSTEM_PROMPT_TEMPLATE, log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import AIInstruction, KBDocument

_LIVE_CACHE_KEY  = "koolbuy:ai_settings:live"
_DRAFT_CACHE_KEY = "koolbuy:ai_settings:draft"
_LIVE_TTL  = 3600   # 1 h — refreshed on go-live
_DRAFT_TTL = 120    # 2 min — short so edits are visible quickly in test-chat


# ── helpers ──────────────────────────────────────────────────────────────────

def _db_live_content() -> tuple[str, str]:
    """Load live instruction + combined KB text from DB. Falls back to files."""
    db = get_db()
    try:
        inst = (
            db.query(AIInstruction)
            .filter(AIInstruction.status == "live")
            .order_by(AIInstruction.created_at.desc())
            .first()
        )
        docs = (
            db.query(KBDocument)
            .filter(KBDocument.status == "live")
            .order_by(KBDocument.created_at.asc())
            .all()
        )
        instruction = inst.content if inst else SYSTEM_PROMPT_TEMPLATE
        kb = "\n\n---\n\n".join(d.content for d in docs) if docs else KNOWLEDGE_BASE
        return instruction, kb
    finally:
        db.close()


def _db_draft_content() -> tuple[str, str]:
    """
    Preview content: draft instruction (or live if no draft) + live docs + draft docs,
    excluding anything marked pending_trash.
    """
    db = get_db()
    try:
        draft_inst = (
            db.query(AIInstruction)
            .filter(AIInstruction.status == "draft")
            .order_by(AIInstruction.created_at.desc())
            .first()
        )
        if not draft_inst:
            draft_inst = (
                db.query(AIInstruction)
                .filter(AIInstruction.status == "live")
                .order_by(AIInstruction.created_at.desc())
                .first()
            )
        docs = (
            db.query(KBDocument)
            .filter(KBDocument.status.in_(["live", "draft"]))
            .order_by(KBDocument.created_at.asc())
            .all()
        )
        instruction = draft_inst.content if draft_inst else SYSTEM_PROMPT_TEMPLATE
        kb = "\n\n---\n\n".join(d.content for d in docs) if docs else KNOWLEDGE_BASE
        return instruction, kb
    finally:
        db.close()


# ── public API ────────────────────────────────────────────────────────────────

async def get_live_content() -> tuple[str, str]:
    """Returns (instruction, kb_text) for the live WhatsApp bot, Redis-cached."""
    if redis_client.client:
        try:
            raw = await redis_client.client.get(_LIVE_CACHE_KEY)
            if raw:
                d = json.loads(raw)
                return d["instruction"], d["kb"]
        except Exception:
            pass

    instruction, kb = _db_live_content()

    if redis_client.client:
        try:
            await redis_client.client.set(
                _LIVE_CACHE_KEY,
                json.dumps({"instruction": instruction, "kb": kb}),
                ex=_LIVE_TTL,
            )
        except Exception:
            pass

    return instruction, kb


async def get_draft_content() -> tuple[str, str]:
    """Returns (instruction, kb_text) for the test-chat preview, short-cached."""
    if redis_client.client:
        try:
            raw = await redis_client.client.get(_DRAFT_CACHE_KEY)
            if raw:
                d = json.loads(raw)
                return d["instruction"], d["kb"]
        except Exception:
            pass

    instruction, kb = _db_draft_content()

    if redis_client.client:
        try:
            await redis_client.client.set(
                _DRAFT_CACHE_KEY,
                json.dumps({"instruction": instruction, "kb": kb}),
                ex=_DRAFT_TTL,
            )
        except Exception:
            pass

    return instruction, kb


async def invalidate_cache():
    """Call after Go Live to force both caches to refresh on next request."""
    if redis_client.client:
        try:
            await redis_client.client.delete(_LIVE_CACHE_KEY, _DRAFT_CACHE_KEY)
        except Exception as e:
            log.warning(f"Cache invalidation failed: {e}")
