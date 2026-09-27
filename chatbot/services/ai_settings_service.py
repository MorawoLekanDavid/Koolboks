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
from chatbot.models import AIInstruction, BotSetting, KBDocument
from chatbot.services.kb_retrieval import select_relevant_kb

_LIVE_CACHE_KEY  = "koolbuy:ai_settings:live"
_DRAFT_CACHE_KEY = "koolbuy:ai_settings:draft"
_LIVE_TTL  = 3600   # 1 h — refreshed on go-live
_DRAFT_TTL = 120    # 2 min — short so edits are visible quickly in test-chat

_WELCOME_KEY = "welcome_text"
_WELCOME_CACHE_KEY = "koolbuy:bot_settings:welcome_text"
_WELCOME_CACHE_TTL = 3600

# {name_part} is the one placeholder this template supports — replaced with
# either "" or ", <name>" by fixed_welcome_text() in chat_service.py, same
# curly-brace convention build_system_prompt() already uses for {bot_name}/
# {user_name}. This exact string is what shipped as a hardcoded Python
# constant before — moving it here doesn't change a single character of
# behaviour by default, it just makes it editable from AI Settings instead
# of requiring a code change + deploy for a wording tweak.
DEFAULT_WELCOME_TEXT = (
    "Hi there{name_part}! \U0001F44B\U0001F3FD Welcome to Koolboks! ❄️\n\n"
    "Let's take the heat off! ☀️ What are you looking to keep Kool today? \U0001F60A\n\n"
    "Tell us what you need, and we'll help you find the right solution."
)


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


# ── Fixed welcome text ───────────────────────────────────────────────────────
# Separate from the instruction/KB draft-live machinery above on purpose: this
# text is never seen by the model (chat_service.py sends it directly, no LLM
# call), so there's nothing for a draft+test-chat cycle to verify -- it saves
# straight to live, the same way you'd edit any other fixed site copy.

async def get_live_welcome_text() -> str:
    """The one, single source of truth for the bot's fixed opening greeting.
    Every caller — the WhatsApp bare-greeting fast path, the website widget's
    __welcome__ sentinel, and the [SEND_FIXED_WELCOME] marker the model can
    trigger for any other non-request opener — goes through
    fixed_welcome_text() in chat_service.py, which calls this. Change it once
    here (via PUT /admin/ai-settings/welcome-text) and every one of those
    three paths picks it up immediately, no code deploy involved."""
    if redis_client.client:
        try:
            cached = await redis_client.client.get(_WELCOME_CACHE_KEY)
            if cached:
                return cached
        except Exception:
            pass

    db = get_db()
    try:
        row = db.query(BotSetting).filter(BotSetting.key == _WELCOME_KEY).first()
        text = row.value if row else DEFAULT_WELCOME_TEXT
    finally:
        db.close()

    if redis_client.client:
        try:
            await redis_client.client.set(_WELCOME_CACHE_KEY, text, ex=_WELCOME_CACHE_TTL)
        except Exception:
            pass
    return text


async def set_welcome_text(text: str, updated_by: str) -> None:
    db = get_db()
    try:
        row = db.query(BotSetting).filter(BotSetting.key == _WELCOME_KEY).first()
        if row:
            row.value = text
            row.updated_by = updated_by
        else:
            db.add(BotSetting(key=_WELCOME_KEY, value=text, updated_by=updated_by))
        db.commit()
    finally:
        db.close()

    if redis_client.client:
        try:
            await redis_client.client.delete(_WELCOME_CACHE_KEY)
        except Exception as e:
            log.warning(f"Welcome text cache invalidation failed: {e}")
