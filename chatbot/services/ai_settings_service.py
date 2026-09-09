"""
Provides live and draft AI content (instruction + KB) for the bot and test-chat.
The document LIST is cached in Redis (that's the expensive DB read) — which
documents actually get joined into the KB text is decided fresh on every call
from the caller's query text (see kb_retrieval.select_relevant_kb), so one
cached result can't lock every customer onto the same document subset.
Cache is invalidated whenever Go Live is called.
"""
import json

from chatbot.config import KNOWLEDGE_BASE, SYSTEM_PROMPT_TEMPLATE, log
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import AIInstruction, KBDocument
from chatbot.services.kb_retrieval import select_relevant_kb

_LIVE_CACHE_KEY  = "koolbuy:ai_settings:live"
_DRAFT_CACHE_KEY = "koolbuy:ai_settings:draft"
_LIVE_TTL  = 3600   # 1 h — refreshed on go-live
_DRAFT_TTL = 120    # 2 min — short so edits are visible quickly in test-chat


# ── helpers ──────────────────────────────────────────────────────────────────

def _db_live_docs() -> tuple[str, list]:
    """Load the live instruction + raw KB document list from DB. Falls back to
    the files (SYSTEM_PROMPT_TEMPLATE / KNOWLEDGE_BASE) when nothing is live yet."""
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
        doc_list = (
            [{"name": d.name, "content": d.content} for d in docs]
            if docs else [{"name": "knowledge_base.txt", "content": KNOWLEDGE_BASE}]
        )
        return instruction, doc_list
    finally:
        db.close()


def _db_draft_docs() -> tuple[str, list]:
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
        doc_list = (
            [{"name": d.name, "content": d.content} for d in docs]
            if docs else [{"name": "knowledge_base.txt", "content": KNOWLEDGE_BASE}]
        )
        return instruction, doc_list
    finally:
        db.close()


# ── public API ────────────────────────────────────────────────────────────────

async def get_live_content(query_text: str = "") -> tuple[str, str]:
    """Returns (instruction, kb_text) for the live WhatsApp bot. `query_text` —
    normally the customer's current message — decides which documents actually
    make it into kb_text; pass "" to get everything (small KBs, or callers that
    genuinely have no query context)."""
    instruction, doc_list = None, None
    if redis_client.client:
        try:
            raw = await redis_client.client.get(_LIVE_CACHE_KEY)
            if raw:
                d = json.loads(raw)
                instruction, doc_list = d["instruction"], d["docs"]
        except Exception:
            pass

    if doc_list is None:
        instruction, doc_list = _db_live_docs()
        if redis_client.client:
            try:
                await redis_client.client.set(
                    _LIVE_CACHE_KEY,
                    json.dumps({"instruction": instruction, "docs": doc_list}),
                    ex=_LIVE_TTL,
                )
            except Exception:
                pass

    return instruction, select_relevant_kb(doc_list, query_text)


async def get_draft_content(query_text: str = "") -> tuple[str, str]:
    """Returns (instruction, kb_text) for the test-chat preview, short-cached."""
    instruction, doc_list = None, None
    if redis_client.client:
        try:
            raw = await redis_client.client.get(_DRAFT_CACHE_KEY)
            if raw:
                d = json.loads(raw)
                instruction, doc_list = d["instruction"], d["docs"]
        except Exception:
            pass

    if doc_list is None:
        instruction, doc_list = _db_draft_docs()
        if redis_client.client:
            try:
                await redis_client.client.set(
                    _DRAFT_CACHE_KEY,
                    json.dumps({"instruction": instruction, "docs": doc_list}),
                    ex=_DRAFT_TTL,
                )
            except Exception:
                pass

    return instruction, select_relevant_kb(doc_list, query_text)


async def invalidate_cache():
    """Call after Go Live to force both caches to refresh on next request."""
    if redis_client.client:
        try:
            await redis_client.client.delete(_LIVE_CACHE_KEY, _DRAFT_CACHE_KEY)
        except Exception as e:
            log.warning(f"Cache invalidation failed: {e}")
