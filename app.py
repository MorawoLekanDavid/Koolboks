from dotenv import load_dotenv
from groq import AsyncGroq
from typing import List, Optional
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from datetime import datetime
from sqlalchemy import create_engine, select, func, and_
from sqlalchemy.orm import sessionmaker, Session
import re
import redis.asyncio as aioredis
import os
import json
import logging

from models import Product, Lead, init_db

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('koolbuy')

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://koolbuy:koolbuy_secure_password_2026@localhost:5432/koolbuy")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
CHAT_TTL = int(os.environ.get("REDIS_CHAT_TTL", 3600))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY_MESSAGES", 20))
IDLE_THRESHOLD = 5 * 60  # seconds — gaps longer than this are treated as idle

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_FILE = os.path.join(BASE_DIR, "system_prompt.txt")
KB_FILE = os.path.join(BASE_DIR, "knowledge_base.txt")


def load_text_file(path: str, label: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        log.info(f"Loaded {label} from {path} ({len(content)} chars)")
        return content
    except FileNotFoundError:
        log.warning(f"{label} not found at {path}")
        return ""


SYSTEM_PROMPT_TEMPLATE = load_text_file(PROMPT_FILE, "system prompt")
KNOWLEDGE_BASE = load_text_file(KB_FILE, "knowledge base")

redis_client = None
db_engine = None
SessionLocal = None


def init_database():
    """Initialize database connection and session factory"""
    global db_engine, SessionLocal
    db_engine = init_db(DATABASE_URL)
    SessionLocal = sessionmaker(bind=db_engine)
    log.info("Database initialized successfully")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client

    # Initialize database
    init_database()

    # Initialize Redis
    try:
        redis_client = await aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
        await redis_client.ping()
        log.info("Redis connected successfully")
    except Exception as e:
        log.warning(
            f"Redis connection failed: {e}. Chat history will not be persisted.")
        redis_client = None

    yield

    if redis_client:
        await redis_client.close()
        log.info("Redis disconnected")
    if db_engine:
        db_engine.dispose()
        log.info("Database disconnected")

app = FastAPI(title="Koolbuy Chatbot API", lifespan=lifespan)
groq_client = AsyncGroq(api_key=GROQ_API_KEY)
app.add_middleware(CORSMiddleware, allow_origins=[
                   "*"], allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    session_id:    str = Field(...)
    message:       str = Field(..., min_length=1, max_length=2000)
    user_name:     str = Field(default="Customer")
    business_type: str = Field(default="")
    volume:        str = Field(default="")
    power:         str = Field(default="")


class ProductCard(BaseModel):
    name:        str
    price:       str
    image_url:   Optional[str] = None
    product_url: Optional[str] = None


class ChatResponse(BaseModel):
    session_id:    str
    response:      str
    products:      List[ProductCard] = []
    lead_captured: bool = False

# ── Inventory (Database) ──────────────────────────────────────────────────────


def get_db():
    """Get database session"""
    return SessionLocal()


def load_products() -> List[Product]:
    """Load all products from database"""
    try:
        db = get_db()
        products = db.query(Product).all()
        db.close()
        return products
    except Exception as e:
        log.warning(f"Failed to load products from DB: {e}")
        return []


def inventory_text(products: List[Product]) -> str:
    """Format products for the system prompt"""
    if not products:
        return "No inventory loaded."

    lines = ["name,price"]
    for p in products[:60]:
        lines.append(f"{p.name},{p.price}")
    return "\n".join(lines)


def match_products(products: List[Product], names: List[str]) -> List[ProductCard]:
    """Match requested product names with available products"""
    if not products or not names:
        return []

    cards = []
    for req_name in names:
        req_lower = req_name.strip().lower()
        for p in products:
            if req_lower in p.name.lower():
                cards.append(ProductCard(
                    name=p.name,
                    price=str(p.price),
                    image_url=p.image_url,
                    product_url=p.product_url,
                ))
                break
    return cards

# ── Redis ──────────────────────────────────────────────────────────────────────


async def get_history(session_id: str) -> list:
    if not redis_client:
        return []
    try:
        raw = await redis_client.get(f"koolbuy:chat:{session_id}")
        return json.loads(raw) if raw else []
    except Exception:
        return []


async def save_history(session_id: str, history: list):
    if not redis_client:
        return
    try:
        trimmed = history[-MAX_HISTORY:]
        await redis_client.set(f"koolbuy:chat:{session_id}", json.dumps(trimmed), ex=CHAT_TTL)
    except Exception:
        pass

# ── Active duration ────────────────────────────────────────────────────────────


def calc_active_duration(history: list) -> str:
    """
    Sum only the gaps between consecutive messages that are under IDLE_THRESHOLD.
    Gaps longer than IDLE_THRESHOLD (default 5 min) are treated as idle time
    and excluded — so pausing the chat for hours doesn't inflate the duration.
    Returns a human-readable string like '4m 32s'.
    """
    timestamps = []
    for msg in history:
        ts = msg.get("ts")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(ts))
            except Exception:
                pass

    if len(timestamps) < 2:
        return ""

    active_seconds = 0
    for i in range(1, len(timestamps)):
        gap = (timestamps[i] - timestamps[i - 1]).total_seconds()
        if gap <= IDLE_THRESHOLD:
            active_seconds += gap

    if active_seconds < 60:
        return f"{int(active_seconds)}s"
    mins = int(active_seconds // 60)
    secs = int(active_seconds % 60)
    return f"{mins}m {secs}s"

# ── Name cleaner ───────────────────────────────────────────────────────────────


def clean_name(raw: str) -> str:
    name = raw.strip()
    patterns = [
        r"(?i)^(hel+o+|hi+|hey+)[,!]?\s*(i'?m|i am|my name is|this is|am)?\s*",
        r"(?i)^(good\s+(morning|evening|afternoon))[,!]?\s*",
        r"(?i)^(i'?m|i am|my name is|this is)\s+",
        r"(?i)^'?m\s+",
    ]
    for p in patterns:
        name = re.sub(p, "", name).strip()
    name = name.lstrip("',-. ")
    name = re.sub(r"[^\w\s-]", "", name).strip()
    result = " ".join(w.capitalize() for w in name.split() if w)
    return result or raw.strip()

# ── Lead saving ────────────────────────────────────────────────────────────────


async def save_lead(user_name: str, phone: str, history: list):
    """Extract rich lead data from conversation and save to leads.csv."""
    lines = []
    for msg in history:
        role = "Customer" if msg.get("role") == "user" else "KoolBot"
        content = re.sub(r'\[[^\]]*\]', '', msg.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    transcript = "\n".join(lines[-40:])

    # Calculate active duration before the Groq call
    duration = calc_active_duration(history)

    data = {}


async def save_lead(user_name: str, phone: str, history: list):
    """Extract rich lead data from conversation and save to database"""
    lines = []
    for msg in history:
        role = "Customer" if msg.get("role") == "user" else "KoolBot"
        content = re.sub(r'\[[^\]]*\]', '', msg.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    transcript = "\n".join(lines[-40:])

    # Calculate active duration before the Groq call
    duration = calc_active_duration(history)

    data = {}
    try:
        prompt = (
            "Read this sales conversation and extract the following fields. "
            "Reply ONLY with valid JSON, no markdown, no extra text:\n"
            '{"name": "", "business": "", "product_interest": "", "amount": "", '
            '"payment_plan": "", "pain_point": "", "power_type": "", "address": ""}\n\n'
            "Rules:\n"
            "- name: customer's actual name ONLY — strip ALL greetings like "
            "'hello', 'hi', 'I am', 'I\\'m', 'my name is' — return ONLY the clean name\n"
            "- business: what they sell or their business type\n"
            "- product_interest: exact product they agreed on or showed most interest in\n"
            "- amount: price of that product as mentioned in the chat\n"
            "- payment_plan: 'outright' if full payment, installment details, or 'flex 70/30'\n"
            "- pain_point: their main challenge e.g. spoilage, power cuts, fuel cost\n"
            "- power_type: 'grid' if NEPA/generator, 'off-grid' if no power, 'solar' if solar\n"
            "- address: delivery location if mentioned, else leave empty\n\n"
            f"CONVERSATION:\n{transcript}"
        )
        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0,
        )
        raw = (completion.choices[0].message.content or "").strip()
        raw = re.sub(r'^```[a-z]*\n?', '', raw).strip()
        raw = re.sub(r'\n?```$', '', raw).strip()
        data = json.loads(raw)
        log.info(f"Lead extracted: {data}")
    except Exception as e:
        log.warning(f"Lead extraction failed: {e}")

    try:
        db = get_db()
        lead = Lead(
            name=clean_name(data.get("name") or user_name),
            phone=phone,
            business=data.get("business", ""),
            product_interest=data.get("product_interest", ""),
            amount=data.get("amount", ""),
            payment_plan=data.get("payment_plan", ""),
            pain_point=data.get("pain_point", ""),
            power_type=data.get("power_type", ""),
            address=data.get("address", ""),
            active_duration=duration,
        )
        db.add(lead)
        db.commit()
        db.close()
        log.info(
            f"Lead saved: {clean_name(data.get('name') or user_name)} | {phone} | duration={duration}")
    except Exception as e:
        log.error(f"Failed to save lead to DB: {e}")


# ── Lead address updater ───────────────────────────────────────────────────────


def phone_from_history(history: list) -> str:
    for msg in history:
        m = re.search(r'VALID phone captured: (\S+)', msg.get("content", ""))
        if m:
            return m.group(1).rstrip('.')
    return ""


async def update_lead_address(phone: str, address: str):
    """Update delivery address for a lead in database"""
    if not phone or not address:
        return
    try:
        db = get_db()
        lead = db.query(Lead).filter(
            and_(
                Lead.phone == phone.strip(),
                (Lead.address == None) | (Lead.address == "")
            )
        ).order_by(Lead.created_at.desc()).first()

        if lead:
            lead.address = address.strip()
            db.commit()
            log.info(f"Lead address updated: {phone} -> {address}")
        db.close()
    except Exception as e:
        log.warning(f"Failed to update lead address: {e}")

# ── Phone validation ───────────────────────────────────────────────────────────
PHONE_RE = re.compile(r'(?<!\d)(0[789]\d{9}|[789]\d{9}|\+234[789]\d{9})(?!\d)')


def extract_valid_phone(text: str) -> Optional[str]:
    m = PHONE_RE.search(text)
    if not m:
        return None
    number = m.group()
    if len(number) == 10 and not number.startswith('0') and not number.startswith('+'):
        number = '0' + number
    return number


# ── Internal note stripper ─────────────────────────────────────────────────────
INTERNAL_NOTE_PATTERNS = [
    r'\[VALID phone captured[^\]]*\]',
    r'\[INVALID phone[^\]]*\]',
    r'\[DELIVERY confirmed[^\]]*\]',
    r'\[SYSTEM NOTE:[^\]]*\]',
    r'\[conversation started\]',
    r'\(waiting for[^)]*\)',
    r'─── CAPTURED STATE ───.*?───────────────────',
]


def strip_internal_notes(text: str) -> str:
    for pattern in INTERNAL_NOTE_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    return text.strip()

# ── System prompt builder ──────────────────────────────────────────────────────


def build_system_prompt(user_name: str, inv: str) -> dict:
    content = SYSTEM_PROMPT_TEMPLATE
    content = content.replace("{user_name}", user_name)
    content = content.replace("{knowledge_base}", KNOWLEDGE_BASE)
    content = content.replace("{inventory}", inv)
    return {"role": "system", "content": content}

# ── Groq call ──────────────────────────────────────────────────────────────────


async def call_groq(messages: list, max_tokens: int = 600) -> str:
    try:
        log.info(f"Calling Groq | model={GROQ_MODEL} | turns={len(messages)}")
        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL, messages=messages,
            max_tokens=max_tokens, temperature=0.7,
        )
        text = (completion.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("Empty response")
        log.info(f"Groq: {text[:80]}...")
        return strip_internal_notes(text)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Groq failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI error: {str(e)}")

# ── Chat endpoint ──────────────────────────────────────────────────────────────


@app.post("/chat", response_model=ChatResponse)
async def chat_handler(request: ChatRequest, background_tasks: BackgroundTasks):
    df = load_products()
    inv = inventory_text(df)
    system = build_system_prompt(request.user_name, inv)

    # Welcome
    if request.message.strip() == "__welcome__":
        history = await get_history(request.session_id)
        if not history:
            welcome_prompt = (
                f"Greet {request.user_name} warmly in one sentence. "
                f"Then ask what they sell or store. Max 2 sentences total."
            )
            messages = [system, {"role": "user", "content": welcome_prompt}]
            welcome_text = await call_groq(messages, max_tokens=100)
            # Save with timestamp
            await save_history(request.session_id, [{
                "role": "assistant",
                "content": welcome_text,
                "ts": datetime.now().isoformat(),
            }])
            return ChatResponse(session_id=request.session_id, response=welcome_text)
        return ChatResponse(session_id=request.session_id, response="")

    # Normal chat
    history = await get_history(request.session_id)
    if history and history[0].get("role") == "assistant":
        history = [{"role": "user", "content": "[conversation started]",
                    "ts": history[0].get("ts", "")}] + history

    # Strip ts timestamps before sending to Groq — Groq only accepts role+content
    history_for_groq = [
        {"role": m["role"], "content": m["content"]} for m in history]

    already_captured = any(
        "[VALID phone captured" in msg.get("content", "")
        for msg in history
    )

    delivery_captured = any(
        "[DELIVERY confirmed" in msg.get("content", "")
        for msg in history
    )

    # Build state summary for clarity
    state_summary = "─── CAPTURED STATE ───\n"
    if already_captured:
        extracted_phone = phone_from_history(history)
        state_summary += f"✓ Phone CAPTURED: {extracted_phone}\n"
    else:
        state_summary += "× Phone: NOT YET CAPTURED\n"

    if delivery_captured:
        state_summary += "✓ Delivery location CAPTURED\n"
    elif already_captured:
        state_summary += "× Delivery location: NOT YET CAPTURED\n"
    state_summary += "───────────────────"

    messages = [system, {"role": "user", "content": state_summary}] + history_for_groq + \
        [{"role": "user", "content": request.message}]

    phone = extract_valid_phone(request.message)
    lead_captured = False
    looks_like_phone = bool(
        re.search(r'\b0\d{7,11}\b|\+234\d{7,11}\b|\b[789]\d{9}\b', request.message))

    if phone and not already_captured:
        background_tasks.add_task(save_lead, request.user_name, phone, history)
        lead_captured = True
        messages[-1]["content"] = (
            f"{request.message}\n\n"
            f"[VALID phone captured: {phone}. "
            f"Your NEXT message MUST do these three things in order: "
            f"1) Thank the customer warmly by name in one sentence. "
            f"2) Say our agent will call soon, also reachable on WhatsApp +2348106912022. "
            f"3) Ask EXACTLY: 'What area or city should we deliver to?' "
            f"Do NOT skip the delivery question. Do NOT end without asking it.]"
        )
    elif already_captured and not phone and not delivery_captured:
        address_keywords = [
            "lagos", "abuja", "ibadan", "kano", "ph", "port harcourt",
            "enugu", "benin", "owerri", "jos", "kaduna", "osun", "oyo",
            "ondo", "ekiti", "kwara", "kogi", "delta", "rivers", "anambra",
            "imo", "abia", "cross river", "akwa ibom", "bayelsa", "edo",
            "abeokuta", "ilorin", "warri", "asaba", "uyo", "calabar",
            "street", "estate", "island", "mainland", "ikeja", "lekki",
            "surulere", "yaba", "gbagada", "state", "road", "close",
            "avenue", "way", "area", "town", "city", "market",
        ]
        msg_lower = request.message.lower()
        is_address = any(kw in msg_lower for kw in address_keywords) and len(
            request.message) > 3
        if is_address:
            background_tasks.add_task(
                update_lead_address,
                phone_from_history(history),
                request.message.strip()
            )
            # Add delivery address confirmation marker
            messages[-1]["content"] = (
                f"{request.message}\n\n"
                f"[DELIVERY confirmed: {request.message.strip()}. "
                f"Address captured successfully. Do not ask for delivery location again. "
                f"Proceed to closing message.]"
            )
    elif looks_like_phone and not already_captured:
        messages[-1]["content"] = (
            f"{request.message}\n\n"
            f"[INVALID phone. Ask for valid Nigerian number — 11 digits starting "
            f"070, 080, 081, 090, 091 or 10 digits starting 7, 8, or 9.]"
        )

    raw = await call_groq(messages)
    cards: List[ProductCard] = []
    m = re.search(r'PRODUCTS:\s*(.+)', raw, re.IGNORECASE)
    if m:
        names = [n.strip() for n in m.group(1).split("|")]
        cards = match_products(df, names)
    clean = re.sub(r'PRODUCTS:\s*.+\n?', '', raw, flags=re.IGNORECASE).strip()

    now = datetime.now().isoformat()
    # Save annotated message (with phone note) to Redis history with timestamp
    user_content = messages[-1]["content"] if messages[-1]["role"] == "user" else request.message
    history.append({"role": "user",      "content": user_content, "ts": now})
    history.append(
        {"role": "assistant", "content": raw,                     "ts": now})
    await save_history(request.session_id, history)

    return ChatResponse(session_id=request.session_id, response=clean, products=cards, lead_captured=lead_captured)

# ── Utility endpoints ──────────────────────────────────────────────────────────


@app.delete("/chat/{session_id}")
async def clear_session(session_id: str):
    await redis_client.delete(f"koolbuy:chat:{session_id}")
    return {"session_id": session_id, "message": "Session cleared."}


@app.get("/chat/{session_id}/history")
async def get_chat_history(session_id: str):
    history = await get_history(session_id)
    return {"session_id": session_id, "history": history, "count": len(history)}


@app.get("/health")
async def health():
    if not redis_client:
        return {"status": "ok", "redis": "not configured"}
    try:
        ping = await redis_client.ping()
        return {"status": "ok", "redis": "connected" if ping else "error"}
    except Exception:
        return {"status": "ok", "redis": "connection failed"}

@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))
