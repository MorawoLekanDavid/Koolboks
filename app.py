from dotenv import load_dotenv
from groq import AsyncGroq
from typing import List, Optional
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Request, Depends
from fastapi.responses import FileResponse, Response
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from sqlalchemy import create_engine, select, func, and_, case, text as sa_text
from sqlalchemy.orm import sessionmaker, Session
import re
import asyncio
import uuid
import hashlib
import secrets
import redis.asyncio as aioredis
import os
import json
import logging
import httpx

from models import Product, Lead, Message, Agent, LeadNote, CannedResponse, init_db

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('koolbuy')


def normalize_phone(phone: str) -> str:
    """Normalize any Nigerian phone format to +234XXXXXXXXXX (E.164).
    Handles: 07037428227, 7037428227, 2347037428227, +2347037428227
    Returns original string unchanged if it doesn't match any known pattern."""
    p = phone.strip().lstrip('+')
    if p.startswith('234') and len(p) == 13:
        digits = p[3:]           # 234XXXXXXXXXX → XXXXXXXXXX
    elif p.startswith('0') and len(p) == 11:
        digits = p[1:]           # 07037428227  → 7037428227
    elif len(p) == 10 and p[0] in '789':
        digits = p               # 7037428227   → 7037428227
    else:
        return phone.strip()
    return '+234' + digits


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == h
    except Exception:
        return False

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://koolbuy:koolbuy_secure_password_2026@localhost:5432/koolbuy")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
CHAT_TTL_STR = os.environ.get("REDIS_CHAT_TTL", "3600")
MAX_HISTORY_STR = os.environ.get("MAX_HISTORY_MESSAGES", "20")
LEAD_TTL_STR = os.environ.get("REDIS_LEAD_TTL", "86400")  # 24h — leads outlive chat sessions
WHATSAPP_CONTACT = os.environ.get("WHATSAPP_CONTACT", "+2348116402869")
ZAPIER_WEBHOOK = os.environ.get("ZAPIER_WEBHOOK", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "KoolbotAdmin2026")
BOT_RESPONSE_DELAY = int(os.environ.get("BOT_RESPONSE_DELAY_SECONDS", "10"))

WABA_ID                    = os.environ.get("WABA_ID", "")
REENGAGEMENT_TEMPLATE      = os.environ.get("REENGAGEMENT_TEMPLATE", "")
REENGAGEMENT_TEMPLATE_LANG = os.environ.get("REENGAGEMENT_TEMPLATE_LANG", "en")

FOLLOW_UP_ENABLED = os.environ.get("FOLLOW_UP_ENABLED", "true").lower() == "true"
FOLLOW_UP_HOURS   = int(os.environ.get("FOLLOW_UP_HOURS", "24"))
FOLLOW_UP_RECHECK_DAYS = int(os.environ.get("FOLLOW_UP_RECHECK_DAYS", "7"))
FOLLOW_UP_MESSAGE = os.environ.get(
    "FOLLOW_UP_MESSAGE",
    "Hi! 👋 We noticed you were checking out our products earlier and wanted to follow up.\n\n"
    "Are you still interested? We're here to help you find the right solution — just reply "
    "and we'll pick up right where we left off! 😊"
)

CHAT_TTL = int(CHAT_TTL_STR) if CHAT_TTL_STR and CHAT_TTL_STR.isdigit() else 3600
MAX_HISTORY = int(MAX_HISTORY_STR) if MAX_HISTORY_STR and MAX_HISTORY_STR.isdigit() else 20
LEAD_TTL = int(LEAD_TTL_STR) if LEAD_TTL_STR and LEAD_TTL_STR.isdigit() else 86400
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
    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS wamid VARCHAR(100)"))
            _c.commit()
    except Exception as _e:
        log.warning(f"wamid migration: {_e}")
    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS canned_responses (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(100),
                    content VARCHAR(2000),
                    created_by VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            _c.commit()
    except Exception as _e:
        log.warning(f"canned_responses migration: {_e}")
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

    # Start background workers
    fu_task = asyncio.create_task(follow_up_worker())
    re_task = asyncio.create_task(reengagement_worker())

    yield

    for task in (fu_task, re_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if redis_client:
        await redis_client.close()
        log.info("Redis disconnected")
    if db_engine:
        db_engine.dispose()
        log.info("Database disconnected")

async def run_follow_ups():
    """Send a personalised follow-up WhatsApp message to conversations silent for
    FOLLOW_UP_HOURS that never gave their phone number."""
    if not redis_client or not WHATSAPP_API_TOKEN:
        return
    cutoff = datetime.utcnow() - timedelta(hours=FOLLOW_UP_HOURS)
    db = get_db()
    try:
        rows = db.execute(
            select(Message.phone, func.max(Message.created_at).label("last_msg"))
            .where(Message.direction == "inbound")
            .group_by(Message.phone)
            .having(func.max(Message.created_at) < cutoff)
        ).all()

        sent = 0
        for r in rows:
            phone = r.phone
            session_id = f"wa_{phone}"
            if await redis_client.get(f"koolbuy:followup:{phone}"):
                continue
            if await redis_client.get(f"koolbuy:handoff:{session_id}"):
                continue
            lead = db.query(Lead).filter(Lead.phone == phone).first()
            if lead:
                continue

            # Build a short transcript from DB for personalisation
            msgs = db.execute(
                select(Message).where(Message.phone == phone)
                .order_by(Message.created_at.asc())
            ).scalars().all()
            lines = []
            for m in msgs[-12:]:
                role = "Customer" if m.direction == "inbound" else "KoolBot"
                lines.append(f"{role}: {m.content[:200]}")
            transcript = "\n".join(lines) or "No prior messages."

            # Ask Groq to write a personalised, polite sales follow-up
            prompt = (
                "You are a friendly, professional sales representative for Koolbuy, "
                "a Nigerian company that sells cooling and solar-powered products "
                "(freezers, refrigerators, ice makers, solar kits).\n\n"
                "A potential customer started a WhatsApp conversation but went silent "
                f"without giving their phone number. Below is the conversation so far:\n\n"
                f"{transcript}\n\n"
                "Write a SHORT follow-up WhatsApp message (2-4 sentences). Rules:\n"
                "- Sound warm and human, not like a bot\n"
                "- Use the customer's name if you know it\n"
                "- Reference what they were interested in if relevant\n"
                "- Do NOT be pushy or desperate\n"
                "- End with a gentle open question or offer to help\n"
                "- No greetings like 'Dear Customer' — keep it casual and natural\n\n"
                "Write ONLY the message text, nothing else:"
            )
            try:
                completion = await groq_client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=180,
                    temperature=0.75,
                )
                follow_up_text = (completion.choices[0].message.content or "").strip()
            except Exception as e:
                log.warning(f"Groq follow-up generation failed for {phone}: {e}")
                follow_up_text = FOLLOW_UP_MESSAGE  # fallback to default

            await send_whatsapp_message(phone, follow_up_text)
            await redis_client.set(
                f"koolbuy:followup:{phone}", "1",
                ex=86400 * FOLLOW_UP_RECHECK_DAYS
            )
            log.info(f"Follow-up sent to {phone}: {follow_up_text[:60]}...")
            sent += 1

        if sent:
            log.info(f"Follow-up run complete: {sent} message(s) sent")
    except Exception as e:
        log.error(f"Follow-up run error: {e}")
    finally:
        db.close()


async def reengagement_worker():
    """Hourly worker: sends a WhatsApp template to drop-offs whose 24h window has closed."""
    log.info("Re-engagement worker started")
    while True:
        await asyncio.sleep(3600)
        if not redis_client or not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
            continue
        # Read config from Redis (set via dashboard) or fall back to env vars
        cfg_raw = await redis_client.get("koolbuy:reengagement_config")
        cfg = json.loads(cfg_raw) if cfg_raw else {}
        tmpl_name = cfg.get("name") or REENGAGEMENT_TEMPLATE
        tmpl_lang = cfg.get("lang") or REENGAGEMENT_TEMPLATE_LANG
        enabled   = cfg.get("enabled", True) if cfg_raw else bool(REENGAGEMENT_TEMPLATE)
        if not tmpl_name or not enabled:
            continue
        try:
            cutoff_hi = datetime.utcnow() - timedelta(hours=24)
            cutoff_lo = datetime.utcnow() - timedelta(hours=72)
            db = get_db()
            try:
                rows = db.execute(
                    select(Message.phone,
                           func.max(Message.created_at).label("last_msg"),
                           func.min(Message.name).label("cust_name"))
                    .where(Message.direction == "inbound")
                    .group_by(Message.phone)
                    .having(and_(func.max(Message.created_at) <= cutoff_hi,
                                 func.max(Message.created_at) >= cutoff_lo))
                ).all()
                lead_phones = {r.phone for r in db.query(Lead.phone).all()}
            finally:
                db.close()

            sent = 0
            for row in rows:
                phone = row.phone
                if phone in lead_phones:
                    continue
                if await redis_client.get(f"koolbuy:reengaged:{phone}"):
                    continue
                session_id = f"wa_{phone}"
                if await redis_client.get(f"koolbuy:handoff:{session_id}"):
                    continue
                wa_to = phone.lstrip('+')
                cust_name = row.cust_name or "there"
                payload = {
                    "messaging_product": "whatsapp",
                    "to": wa_to,
                    "type": "template",
                    "template": {
                        "name": tmpl_name,
                        "language": {"code": tmpl_lang},
                        "components": [{"type": "body", "parameters": [{"type": "text", "text": cust_name}]}]
                    }
                }
                try:
                    async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
                        r = await client.post(
                            f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                            json=payload,
                            headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
                        )
                    if r.is_success:
                        await redis_client.set(f"koolbuy:reengaged:{phone}", "1", ex=7 * 86400)
                        save_message_db(session_id, phone, "KoolBot", "outbound", f"[Auto re-engagement: {tmpl_name}]")
                        sent += 1
                        log.info(f"Re-engagement template sent to {phone}")
                    else:
                        log.warning(f"Re-engagement failed for {phone}: {r.text}")
                except Exception as e:
                    log.warning(f"Re-engagement send error for {phone}: {e}")
            if sent:
                log.info(f"Re-engagement worker: {sent} templates sent")
        except Exception as e:
            log.error(f"Re-engagement worker error: {e}")


async def follow_up_worker():
    """Hourly background task that triggers follow-up messages."""
    log.info(f"Follow-up worker started (enabled={FOLLOW_UP_ENABLED}, hours={FOLLOW_UP_HOURS})")
    while True:
        try:
            await asyncio.sleep(3600)
            if FOLLOW_UP_ENABLED:
                await run_follow_ups()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Follow-up worker error: {e}")


app = FastAPI(title="Koolbuy Chatbot API", lifespan=lifespan)
groq_client = AsyncGroq(
    api_key=GROQ_API_KEY,
    http_client=httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
    ),
)
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
    description: Optional[str] = None


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

    lines = ["name | price | description (features and capacities)"]
    for p in products[:60]:
        desc = str(p.description)[:250].replace('\n', ' ') if p.description else ''
        lines.append(f"{p.name} | {p.price} | {desc}")
    return "\n".join(lines)


def proxy_image_url(original_url: Optional[str]) -> Optional[str]:
    """Rewrite an S3 image URL to go through our /img-proxy endpoint.
    This avoids CORS / direct-access errors in the browser."""
    if not original_url:
        return None
    from urllib.parse import quote
    return f"/img-proxy?url={quote(original_url, safe='')}"


def match_products(products: List[Product], names: List[str]) -> List[ProductCard]:
    """Match requested product names with available products"""
    if not products or not names:
        return []

    cards = []
    seen = set()
    for req_name in names:
        req_lower = req_name.strip().lower()
        for p in products:
            if req_lower in p.name.lower() and p.id not in seen:
                cards.append(ProductCard(
                    name=p.name,
                    price=str(p.price),
                    image_url=proxy_image_url(p.image_url),
                    product_url=p.product_url,
                    description=p.description,
                ))
                seen.add(p.id)
                break
    return cards


def auto_detect_products(products: List[Product], raw_text: str, product_hint: str = None) -> List[ProductCard]:
    """Fallback: scan AI response text for any product names mentioned.
    This catches cases where the LLM writes the name incorrectly or forgets PRODUCTS: tag."""
    if not products or not raw_text:
        return []

    # If we have a specific product name hallucinated by the AI, score against THAT strictly.
    # Otherwise, score against the entire conversational text.
    text_to_score = product_hint.lower() if product_hint else raw_text.lower()
    
    # Strategy 1: Word Intersection Scoring (Find the closest name match)
    # Score each product based on how many of its significant name keywords are in the text
    best_product = None
    highest_score = 0
    
    for p in products:
        name_lower = p.name.lower()
        # Extract meaningful alphanumeric words (e.g. "208L", "45Ah", "koolboks", "solar")
        name_words = re.findall(r'[a-z0-9]+', name_lower)
        # Filter short common words
        sig_words = set(w for w in name_words if len(w) > 2)
        
        if not sig_words:
            continue
            
        # Count how many of these significant words appear in the text
        score = sum(1 for w in sig_words if w in text_to_score)
        
        # We need a minimum threshold of matches to consider it a real match usually (e.g. brand + capacity)
        if score > highest_score and score >= 2:
            highest_score = score
            best_product = p
            
    if best_product:
        return [ProductCard(
            name=best_product.name,
            price=str(best_product.price),
            image_url=proxy_image_url(best_product.image_url),
            product_url=best_product.product_url,
            description=best_product.description,
        )]

    # Strategy 2: Absolute Fallback - Match by exact price mentioned if name scoring completely failed
    prices_in_text = re.findall(r'[\d,]+(?:\.\d+)?', raw_text.replace('N', '').replace('₦', ''))
    for price_str in prices_in_text:
        try:
            price_val = float(price_str.replace(',', ''))
            if price_val < 10000:
                continue
            for p in products:
                if abs(p.price - price_val) < 100:
                    return [ProductCard(
                        name=p.name,
                        price=str(p.price),
                        image_url=proxy_image_url(p.image_url),
                        product_url=p.product_url,
                        description=p.description,
                    )]
        except (ValueError, TypeError):
            continue

    return []

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
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as json_err:
            log.warning(f"Lead JSON parse failed: {json_err} | raw={raw[:200]}")
            data = {}
        log.info(f"Lead extracted: {data}")
    except Exception as e:
        log.warning(f"Lead extraction failed: {e}")

    try:
        db = get_db()
        clean = clean_name(data.get("name") or user_name)
        norm_phone = normalize_phone(phone)
        existing = db.query(Lead).filter(Lead.phone == norm_phone).first()
        if existing:
            if data.get("name"):        existing.name             = clean
            if data.get("business"):    existing.business         = data["business"]
            if data.get("product_interest"): existing.product_interest = data["product_interest"]
            if data.get("amount"):      existing.amount           = data["amount"]
            if data.get("payment_plan"): existing.payment_plan    = data["payment_plan"]
            if data.get("pain_point"):  existing.pain_point       = data["pain_point"]
            if data.get("power_type"):  existing.power_type       = data["power_type"]
            if data.get("address"):     existing.address          = data["address"]
            existing.active_duration = duration
            existing.updated_at = datetime.utcnow()
            log.info(f"Lead updated: {clean} | {norm_phone} | duration={duration}")
        else:
            lead = Lead(
                name=clean,
                phone=norm_phone,
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
            log.info(f"Lead saved: {clean} | {phone} | duration={duration}")
        db.commit()
        db.close()
    except Exception as e:
        log.error(f"Failed to save lead to DB: {e}")

    # ── Push to CRM via Zapier webhook ─────────────────────────────────────────
    if ZAPIER_WEBHOOK:
        try:
            payload = {
                "name":             clean_name(data.get("name") or user_name),
                "phone":            phone,
                "business":         data.get("business", ""),
                "product_interest": data.get("product_interest", ""),
                "amount":           data.get("amount", ""),
                "payment_plan":     data.get("payment_plan", ""),
                "pain_point":       data.get("pain_point", ""),
                "power_type":       data.get("power_type", ""),
                "address":          data.get("address", ""),
                "active_duration":  duration,
                "source":           "koolbuy_chatbot",
            }
            async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
                resp = await client.post(ZAPIER_WEBHOOK, json=payload)
            log.info(f"Zapier webhook sent: status={resp.status_code}")
        except Exception as e:
            log.warning(f"Zapier webhook failed: {e}")


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
                Lead.phone == normalize_phone(phone),
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
SESSION_ID_RE = re.compile(r'^[a-zA-Z0-9_\-\+]{8,120}$')
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_MESSAGES", "50"))


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
        raise HTTPException(status_code=502, detail="AI service error. Please try again.")

# ── Chat endpoint ──────────────────────────────────────────────────────────────


@app.post("/chat", response_model=ChatResponse)
async def chat_handler(request: ChatRequest, background_tasks: BackgroundTasks):
    # Validate session ID format
    if not SESSION_ID_RE.match(request.session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID.")

    # Rate limiting: max RATE_LIMIT user messages per session per hour
    if redis_client and request.message.strip() != "__welcome__":
        try:
            rate_key = f"koolbuy:rate:{request.session_id}"
            count = await redis_client.incr(rate_key)
            if count == 1:
                await redis_client.expire(rate_key, 3600)
            if count > RATE_LIMIT:
                raise HTTPException(status_code=429, detail="Message limit reached. Please start a new conversation.")
        except HTTPException:
            raise
        except Exception as e:
            log.warning(f"Rate limit check failed: {e}")

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

    try:
        phone_redis = await redis_client.get(f"koolbuy:phone:{request.session_id}") if redis_client else None
        delivery_redis = await redis_client.get(f"koolbuy:delivery:{request.session_id}") if redis_client else None
    except Exception as e:
        log.warning(f"Redis read failed in chat handler: {e}")
        phone_redis = None
        delivery_redis = None

    already_captured = bool(phone_redis) or any(
        "[VALID phone captured" in msg.get("content", "") for msg in history
    )

    delivery_captured = bool(delivery_redis) or any(
        "[DELIVERY confirmed" in msg.get("content", "") for msg in history
    )

    # Build state summary for clarity
    state_summary = "─── CAPTURED STATE ───\n"
    if already_captured:
        if phone_redis:
            extracted_phone = phone_redis
        else:
            extracted_phone = phone_from_history(history)
            if redis_client and extracted_phone:
                await redis_client.set(f"koolbuy:phone:{request.session_id}", extracted_phone, ex=LEAD_TTL)
        state_summary += f"✓ Phone CAPTURED: {extracted_phone}\n"
    else:
        state_summary += "× Phone: NOT YET CAPTURED\n"

    if delivery_captured:
        if redis_client and not delivery_redis:
             await redis_client.set(f"koolbuy:delivery:{request.session_id}", "captured", ex=LEAD_TTL)
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
        if redis_client:
            await redis_client.set(f"koolbuy:phone:{request.session_id}", phone, ex=LEAD_TTL)
        messages[-1]["content"] = (
            f"{request.message}\n\n"
            f"[VALID phone captured: {phone}. "
            f"Your NEXT message MUST do these three things in order: "
            f"1) Thank the customer warmly by name in one sentence. "
            f"2) Say our agent will call soon, also reachable on WhatsApp {WHATSAPP_CONTACT}. "
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
                phone_from_history(history) or (phone_redis if phone_redis else ""),
                request.message.strip()
            )
            if redis_client:
                await redis_client.set(f"koolbuy:delivery:{request.session_id}", "captured", ex=LEAD_TTL)
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
        
        # FALLBACK: If the AI tried to use the PRODUCTS: tag but hallucinated the name, 
        # use the intelligent text-scoring fallback on its response to figure out what it meant
        if not cards:
            cards = auto_detect_products(df, raw, product_hint=m.group(1))

    # Debug: log what product cards we're sending to frontend
    if cards:
        for c in cards:
            log.info(f"PRODUCT CARD → name={c.name} | price={c.price} | image_url={c.image_url}")
    else:
        log.info("NO product cards matched for this response")

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

# ── Image proxy ────────────────────────────────────────────────────────────────
_img_cache: dict = {}  # Simple in-memory cache: url -> (content_type, bytes)
_img_cache_lock = asyncio.Lock()
_http_client: Optional[httpx.AsyncClient] = None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    return _http_client


@app.get("/img-proxy")
async def image_proxy(url: str = Query(...)):
    """Proxy S3 product images to avoid CORS / direct-access issues in browser."""
    # Security: only allow proxying from our S3 bucket
    if "koolbuy-assets.s3" not in url and "amazonaws.com" not in url:
        raise HTTPException(status_code=403, detail="Forbidden: only koolbuy S3 URLs allowed")

    # Check cache
    if url in _img_cache:
        ct, data = _img_cache[url]
        return Response(content=data, media_type=ct,
                        headers={"Cache-Control": "public, max-age=86400"})

    try:
        client = await _get_http_client()
        resp = await client.get(url)
        if resp.status_code != 200:
            log.warning(f"Image proxy: S3 returned {resp.status_code} for {url}")
            raise HTTPException(status_code=resp.status_code, detail="Image fetch failed")

        content_type = resp.headers.get("content-type", "image/jpeg")
        img_bytes = resp.content

        # Cache it (limit cache to ~100 images to avoid memory bloat)
        async with _img_cache_lock:
            if len(_img_cache) < 100:
                _img_cache[url] = (content_type, img_bytes)

        return Response(content=img_bytes, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    except httpx.RequestError as e:
        log.error(f"Image proxy error: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to fetch image: {str(e)}")


@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


# ── Admin Dashboard ───────────────────────────────────────────────────────────

async def get_admin_ctx(key: str = Query(...)) -> dict:
    if key == ADMIN_KEY:
        # Look up super_admin account for their real name
        db = get_db()
        try:
            sa = db.query(Agent).filter(Agent.role == "super_admin").first()
            name = sa.name if sa else "Admin"
        finally:
            db.close()
        return {"role": "super_admin", "name": name}
    if redis_client:
        try:
            raw = await redis_client.get(f"koolbuy:agent_session:{key}")
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    raise HTTPException(status_code=403, detail="Unauthorized")


async def require_super_admin(ctx: dict = Depends(get_admin_ctx)) -> dict:
    if ctx.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return ctx


async def require_admin(ctx: dict = Depends(get_admin_ctx)) -> dict:
    if ctx.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return ctx


@app.get("/admin/me")
async def get_me(ctx: dict = Depends(get_admin_ctx)):
    return {"role": ctx.get("role", "agent"), "name": ctx.get("name", "")}


@app.get("/admin")
async def admin_dashboard():
    return FileResponse(os.path.join(BASE_DIR, "admin", "index.html"))


# ── Agent Auth ────────────────────────────────────────────────────────────────

class AgentLoginRequest(BaseModel):
    email: str
    password: str
    admin_key: Optional[str] = None  # provided only when registering/logging in as super_admin


@app.post("/admin/agent-login")
async def agent_login(body: AgentLoginRequest):
    db = get_db()
    try:
        email = body.email.strip().lower()

        # Super admin path: valid key provided → register or login as super_admin
        if body.admin_key and body.admin_key == ADMIN_KEY:
            agent = db.query(Agent).filter(func.lower(Agent.email) == email).first()
            if not agent:
                # First-time super_admin registration
                if not body.password:
                    raise HTTPException(400, "Password is required to create your account.")
                agent = Agent(name=body.email.split("@")[0].capitalize(),
                              email=email,
                              password_hash=hash_password(body.password),
                              role="super_admin")
                db.add(agent)
                db.commit()
                db.refresh(agent)
                log.info(f"Super admin account created: {email}")
            else:
                if agent.role != "super_admin":
                    raise HTTPException(403, "This email is registered as a regular agent, not super admin.")
                if not body.password:
                    raise HTTPException(400, "Password is required.")
                if not verify_password(body.password, agent.password_hash):
                    # Knowing the admin key authorizes resetting a forgotten super admin password
                    agent.password_hash = hash_password(body.password)
                    db.commit()
                    log.info(f"Super admin password reset via admin key: {email}")
            # Super admin token is always the ADMIN_KEY for backward compat
            return {"token": ADMIN_KEY, "name": agent.name, "role": "super_admin"}

        # Normal login path (agent or admin)
        agent = db.query(Agent).filter(func.lower(Agent.email) == email).first()
        if not agent:
            raise HTTPException(403, "Email not registered. Contact your admin.")
        if not agent.password_hash:
            raise HTTPException(403, "Password not set. Ask your admin to reset it.")
        if not verify_password(body.password, agent.password_hash):
            raise HTTPException(403, "Incorrect password.")
        token = str(uuid.uuid4())
        session_data = json.dumps({
            "role": agent.role,
            "name": agent.name,
            "email": agent.email,
            "agent_id": agent.id,
        })
        if redis_client:
            await redis_client.set(f"koolbuy:agent_session:{token}", session_data, ex=86400)
        return {"token": token, "name": agent.name, "role": agent.role}
    finally:
        db.close()


# ── Agent Management ──────────────────────────────────────────────────────────

class AgentCreate(BaseModel):
    name: str
    email: str
    password: str


@app.get("/admin/agents")
async def list_agents(ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        agents = db.query(Agent).order_by(Agent.created_at.asc()).all()
        return [{"id": a.id, "name": a.name, "email": a.email, "role": a.role,
                 "created_at": a.created_at.isoformat()} for a in agents]
    finally:
        db.close()


@app.post("/admin/agents")
async def register_agent(body: AgentCreate, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        existing = db.query(Agent).filter(
            func.lower(Agent.email) == body.email.strip().lower()
        ).first()
        if existing:
            raise HTTPException(409, "An agent with this email already exists.")
        agent = Agent(
            name=body.name.strip(),
            email=body.email.strip().lower(),
            password_hash=hash_password(body.password),
            role="agent",
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)
        return {"id": agent.id, "name": agent.name, "email": agent.email, "role": agent.role}
    finally:
        db.close()


class RoleUpdate(BaseModel):
    role: str  # "admin" or "agent"

@app.patch("/admin/agents/{agent_id}/role")
async def update_agent_role(agent_id: int, body: RoleUpdate, ctx: dict = Depends(require_super_admin)):
    if body.role not in ("admin", "agent"):
        raise HTTPException(400, "Role must be 'admin' or 'agent'")
    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "Agent not found.")
        if agent.role == "super_admin":
            raise HTTPException(403, "Cannot change the super admin's role.")
        agent.role = body.role
        db.commit()
        return {"id": agent.id, "name": agent.name, "role": agent.role}
    finally:
        db.close()


@app.delete("/admin/agents/{agent_id}")
async def delete_agent(agent_id: int, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "Agent not found.")
        if agent.role == "super_admin":
            raise HTTPException(403, "Cannot remove the super admin account.")
        if agent.role == "admin" and ctx.get("role") != "super_admin":
            raise HTTPException(403, "Only super admin can remove an admin account.")
        db.delete(agent)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@app.post("/admin/change-password")
async def change_password(body: ChangePasswordRequest, ctx: dict = Depends(get_admin_ctx)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters.")
    db = get_db()
    try:
        agent_id = ctx.get("agent_id")
        if agent_id:
            agent = db.query(Agent).filter(Agent.id == agent_id).first()
        else:
            agent = db.query(Agent).filter(Agent.role == "super_admin").first()
        if not agent or not agent.password_hash:
            raise HTTPException(404, "Account not found.")
        if not verify_password(body.old_password, agent.password_hash):
            raise HTTPException(403, "Current password is incorrect.")
        agent.password_hash = hash_password(body.new_password)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


class ResetPasswordRequest(BaseModel):
    new_password: str


@app.post("/admin/agents/{agent_id}/reset-password")
async def reset_agent_password(agent_id: int, body: ResetPasswordRequest, ctx: dict = Depends(require_admin)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters.")
    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "Agent not found.")
        if agent.role == "super_admin":
            raise HTTPException(403, "Use the login screen's admin key to reset the super admin password.")
        if agent.role == "admin" and ctx.get("role") != "super_admin":
            raise HTTPException(403, "Only super admin can reset an admin's password.")
        agent.password_hash = hash_password(body.new_password)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


# ── Conversations ─────────────────────────────────────────────────────────────

@app.get("/admin/conversations")
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
        if redis_client and phones:
            handoff_keys   = [f"koolbuy:handoff:wa_{p}" for p in phones]
            read_keys      = [f"koolbuy:conv_read:{p}"  for p in phones]
            handoff_vals, read_vals = await asyncio.gather(
                redis_client.mget(*handoff_keys),
                redis_client.mget(*read_keys),
            )
            handoff_map  = {p: v for p, v in zip(phones, handoff_vals)}
            read_map     = {p: v for p, v in zip(phones, read_vals)}
        else:
            handoff_map = {}; read_map = {}

        # Batch unread counts — one query for all phones
        inbound_totals = {r2.phone: r2.total for r2 in db.execute(
            select(Message.phone, func.count(Message.id).label("total"))
            .where(and_(Message.phone.in_(phones), Message.direction == "inbound"))
            .group_by(Message.phone)
        ).all()} if phones else {}

        result = []
        for r in rows:
            handoff    = handoff_map.get(r.phone)
            last_read  = read_map.get(r.phone)
            total_in   = inbound_totals.get(r.phone, 0)
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


@app.get("/admin/conversations/{phone}")
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


@app.post("/admin/conversations/{phone}/mark-read")
async def mark_conversation_read(phone: str, ctx: dict = Depends(get_admin_ctx)):
    if redis_client:
        await redis_client.set(f"koolbuy:conv_read:{phone}", datetime.utcnow().isoformat(), ex=86400 * 7)
    return {"status": "ok"}


class CannedRequest(BaseModel):
    title: str
    content: str


@app.get("/admin/canned-responses")
async def list_canned(ctx: dict = Depends(get_admin_ctx)):
    db = get_db()
    try:
        rows = db.query(CannedResponse).order_by(CannedResponse.created_at.asc()).all()
        return [{"id": r.id, "title": r.title, "content": r.content, "created_by": r.created_by} for r in rows]
    finally:
        db.close()


@app.post("/admin/canned-responses")
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


@app.patch("/admin/canned-responses/{canned_id}")
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


@app.delete("/admin/canned-responses/{canned_id}")
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


@app.post("/admin/conversations/{phone}/reply")
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
    if redis_client:
        history = await get_history(session_id)
        history.append({
            "role": "assistant",
            "content": f"[Agent {display_name}]: {body.message}",
            "ts": datetime.now().isoformat(),
        })
        await save_history(session_id, history)

    return {"status": "sent"}


class HandoffRequest(BaseModel):
    agent_name: str = "Agent"


@app.post("/admin/handoff/{phone}")
async def toggle_handoff(phone: str, body: HandoffRequest = HandoffRequest(), ctx: dict = Depends(get_admin_ctx)):
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    session_id = f"wa_{normalize_phone(phone)}"
    handoff_key = f"koolbuy:handoff:{session_id}"
    current = await redis_client.get(handoff_key)
    agent_display = ctx.get("name") or body.agent_name or "Agent"
    if current:
        await redis_client.delete(handoff_key)
        mode = "bot"
        agent = None
    else:
        await redis_client.set(handoff_key, agent_display, ex=86400)
        mode = "agent"
        agent = agent_display
    log.info(f"Handoff toggled for {phone}: now {mode} ({agent})")
    return {"phone": phone, "mode": mode, "agent": agent}


# ── Follow-up management ─────────────────────────────────────────────────────

@app.get("/admin/follow-up/config")
async def get_follow_up_config(ctx: dict = Depends(require_admin)):
    return {
        "enabled": FOLLOW_UP_ENABLED,
        "hours": FOLLOW_UP_HOURS,
        "recheck_days": FOLLOW_UP_RECHECK_DAYS,
        "message": FOLLOW_UP_MESSAGE,
    }


@app.get("/admin/follow-up/trigger")
async def trigger_follow_ups(ctx: dict = Depends(require_admin)):
    """Manually trigger a follow-up run (useful for testing)."""
    await run_follow_ups()
    return {"status": "done"}


# ── Analytics (super admin only) ──────────────────────────────────────────────

@app.get("/admin/analytics/conversations-handled")
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


@app.get("/admin/analytics/product-recommendations")
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


@app.get("/admin/analytics/lead-funnel")
async def lead_funnel(ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        # WhatsApp sender phones (international format e.g. 2348012345678)
        msg_phones = {r.phone for r in db.execute(
            select(Message.phone).where(Message.direction == "inbound").distinct()
        ).all()}

        # Lead phones normalized to local format for cross-format comparison
        lead_phones_norm = {normalize_phone(r.phone) for r in db.query(Lead.phone).all()}
        total_leads = len(lead_phones_norm)

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


# ── Lead Management ───────────────────────────────────────────────────────────

@app.get("/admin/leads/by-phone/{phone}")
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


@app.get("/admin/leads/{phone}/notes")
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

@app.post("/admin/leads/{phone}/notes")
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


@app.get("/admin/leads")
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
            "product_interest": l.product_interest,
            "business": l.business,
            "amount": l.amount,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        } for l in leads]
    finally:
        db.close()


@app.get("/admin/leads/dropoffs")
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


# ── Product Management ────────────────────────────────────────────────────────

class ProductIn(BaseModel):
    name: str
    price: float
    image_url: Optional[str] = ""
    product_url: Optional[str] = ""
    description: Optional[str] = ""

@app.get("/admin/products")
async def list_products(ctx: dict = Depends(get_admin_ctx)):
    db = get_db()
    try:
        products = db.query(Product).order_by(Product.name).all()
        return [{"id": p.id, "name": p.name, "price": p.price,
                 "image_url": p.image_url, "product_url": p.product_url,
                 "description": p.description,
                 "created_at": p.created_at.isoformat() if p.created_at else None} for p in products]
    finally:
        db.close()

@app.post("/admin/products")
async def create_product(body: ProductIn, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        p = Product(name=body.name.strip(), price=body.price,
                    image_url=body.image_url or "", product_url=body.product_url or "",
                    description=body.description or "")
        db.add(p)
        db.commit()
        db.refresh(p)
        return {"id": p.id, "name": p.name, "price": p.price,
                "image_url": p.image_url, "product_url": p.product_url, "description": p.description}
    finally:
        db.close()

@app.patch("/admin/products/{product_id}")
async def update_product(product_id: int, body: ProductIn, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        p = db.query(Product).filter(Product.id == product_id).first()
        if not p:
            raise HTTPException(404, "Product not found")
        p.name = body.name.strip()
        p.price = body.price
        p.image_url = body.image_url or ""
        p.product_url = body.product_url or ""
        p.description = body.description or ""
        p.updated_at = datetime.utcnow()
        db.commit()
        return {"id": p.id, "name": p.name, "price": p.price,
                "image_url": p.image_url, "product_url": p.product_url, "description": p.description}
    finally:
        db.close()

@app.delete("/admin/products/{product_id}")
async def delete_product(product_id: int, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        p = db.query(Product).filter(Product.id == product_id).first()
        if not p:
            raise HTTPException(404, "Product not found")
        db.delete(p)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


class ProductBulkIn(BaseModel):
    products: List[ProductIn]

@app.post("/admin/products/bulk")
async def bulk_upsert_products(body: ProductBulkIn, ctx: dict = Depends(require_admin)):
    if not body.products:
        raise HTTPException(400, "No products provided")
    db = get_db()
    try:
        created, updated = 0, 0
        for item in body.products:
            name = item.name.strip()
            if not name:
                continue
            existing = db.query(Product).filter(Product.name == name).first()
            if existing:
                existing.price = item.price
                existing.image_url = item.image_url or ""
                existing.product_url = item.product_url or ""
                existing.description = item.description or ""
                existing.updated_at = datetime.utcnow()
                updated += 1
            else:
                db.add(Product(name=name, price=item.price,
                               image_url=item.image_url or "",
                               product_url=item.product_url or "",
                               description=item.description or ""))
                created += 1
        db.commit()
        return {"created": created, "updated": updated}
    finally:
        db.close()


# ── WhatsApp Templates ────────────────────────────────────────────────────────

class CreateTemplateRequest(BaseModel):
    name: str
    category: str = "UTILITY"
    language: str = "en"
    body: str
    header: Optional[str] = None
    footer: Optional[str] = None
    body_samples: List[str] = []  # example values for {{1}}, {{2}}, … in body

class SendTemplateRequest(BaseModel):
    template_name: str
    language: str = "en"
    variables: List[str] = []


@app.get("/admin/templates")
async def list_templates(ctx: dict = Depends(get_admin_ctx)):
    if not WABA_ID or not WHATSAPP_API_TOKEN:
        raise HTTPException(status_code=400, detail="WABA_ID or API token not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{WHATSAPP_API_URL}/{WABA_ID}/message_templates",
            params={"access_token": WHATSAPP_API_TOKEN, "limit": 100,
                    "fields": "id,name,status,category,language,components"}
        )
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


@app.post("/admin/templates")
async def create_template(body: CreateTemplateRequest, ctx: dict = Depends(require_admin)):
    if not WABA_ID or not WHATSAPP_API_TOKEN:
        raise HTTPException(status_code=400, detail="WABA_ID or API token not configured")
    import re as _re
    components = []
    if body.header:
        components.append({"type": "HEADER", "format": "TEXT", "text": body.header})
    body_comp: dict = {"type": "BODY", "text": body.body}
    # Meta requires sample values for every {{n}} variable
    var_count = len(set(_re.findall(r'\{\{\d+\}\}', body.body)))
    if var_count:
        samples = body.body_samples or []
        # Pad with "Sample text" if fewer samples than variables
        while len(samples) < var_count:
            samples.append("Sample text")
        body_comp["example"] = {"body_text": [samples[:var_count]]}
    components.append(body_comp)
    if body.footer:
        components.append({"type": "FOOTER", "text": body.footer})
    payload = {
        "name": body.name.lower().replace(" ", "_"),
        "category": body.category.upper(),
        "language": body.language,
        "components": components,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{WHATSAPP_API_URL}/{WABA_ID}/message_templates",
            json=payload,
            headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
        )
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


@app.delete("/admin/templates/{template_name}")
async def delete_template(template_name: str, ctx: dict = Depends(require_admin)):
    if not WABA_ID or not WHATSAPP_API_TOKEN:
        raise HTTPException(status_code=400, detail="WABA_ID or API token not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.delete(
            f"{WHATSAPP_API_URL}/{WABA_ID}/message_templates",
            params={"name": template_name, "access_token": WHATSAPP_API_TOKEN}
        )
    return {"success": r.is_success}


@app.post("/admin/conversations/{phone}/send-template")
async def send_template_to_phone(phone: str, body: SendTemplateRequest, ctx: dict = Depends(get_admin_ctx)):
    norm = normalize_phone(phone)
    wa_to = norm.lstrip('+')
    payload = {
        "messaging_product": "whatsapp",
        "to": wa_to,
        "type": "template",
        "template": {
            "name": body.template_name,
            "language": {"code": body.language},
        }
    }
    if body.variables:
        payload["template"]["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": v} for v in body.variables]
        }]
    async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
        r = await client.post(
            f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
            json=payload,
            headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
        )
    if r.is_success:
        session_id = f"wa_{norm}"
        agent_name = ctx.get("name", "Agent")
        save_message_db(session_id, norm, agent_name, "outbound",
                        f"[Template: {body.template_name}]" + (f" — {', '.join(body.variables)}" if body.variables else ""))
    data = r.json()
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=data)
    return data


@app.post("/admin/templates/reengagement-config")
async def set_reengagement_template(body: dict, ctx: dict = Depends(require_admin)):
    """Store the chosen re-engagement template name in Redis so the worker picks it up."""
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    name = body.get("template_name", "")
    lang = body.get("language", "en")
    enabled = body.get("enabled", True)
    await redis_client.set("koolbuy:reengagement_config", json.dumps({"name": name, "lang": lang, "enabled": enabled}))
    return {"ok": True}


@app.get("/admin/templates/reengagement-config")
async def get_reengagement_config(ctx: dict = Depends(get_admin_ctx)):
    if not redis_client:
        return {"name": REENGAGEMENT_TEMPLATE, "lang": REENGAGEMENT_TEMPLATE_LANG, "enabled": bool(REENGAGEMENT_TEMPLATE)}
    raw = await redis_client.get("koolbuy:reengagement_config")
    if raw:
        return json.loads(raw)
    return {"name": REENGAGEMENT_TEMPLATE, "lang": REENGAGEMENT_TEMPLATE_LANG, "enabled": bool(REENGAGEMENT_TEMPLATE)}


# ── WhatsApp Webhook ──────────────────────────────────────────────────────────

WHATSAPP_VERIFY_TOKEN  = os.environ.get("WHATSAPP_VERIFY_TOKEN", "koolbuy_whatsapp_2026")
WHATSAPP_API_TOKEN     = os.environ.get("WHATSAPP_API_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_API_URL       = "https://graph.facebook.com/v19.0"


async def mark_whatsapp_read(message_id: str):
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        return
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    try:
        async with httpx.AsyncClient(timeout=5.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            await client.post(url, json=payload, headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"})
    except Exception as e:
        log.warning(f"Failed to mark message as read: {e}")


async def send_whatsapp_message(to: str, body: str, image_url: str = None) -> Optional[str]:
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        log.warning("WhatsApp credentials not configured")
        return None
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
    wamid = None
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            if image_url:
                img_payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": image_url}}
                await client.post(url, json=img_payload, headers=headers)
            text_payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
            resp = await client.post(url, json=text_payload, headers=headers)
        log.info(f"WhatsApp message sent to {to}: status={resp.status_code}")
        try:
            wamid = resp.json()["messages"][0]["id"]
        except Exception:
            pass
    except Exception as e:
        log.error(f"Failed to send WhatsApp message: {e}")
    return wamid


def save_message_db(session_id: str, phone: str, name: str, direction: str, content: str, wamid: str = None):
    try:
        db = get_db()
        db.add(Message(session_id=session_id, phone=normalize_phone(phone), name=name, direction=direction, content=content, wamid=wamid))
        db.commit()
        db.close()
    except Exception as e:
        log.error(f"Failed to save message: {e}")


async def delayed_bot_response(session_id: str, wa_from: str, name: str, text: str):
    """Wait for the agent takeover window, then respond if no agent claimed the session."""
    if BOT_RESPONSE_DELAY > 0:
        await asyncio.sleep(BOT_RESPONSE_DELAY)

    # Re-check handoff — agent may have taken over during the delay
    handoff_key = f"koolbuy:handoff:{session_id}"
    in_handoff = await redis_client.get(handoff_key) if redis_client else None
    if in_handoff:
        log.info(f"[delay] {session_id} claimed by agent during window — bot silent")
        return

    # Generate bot response
    bg = BackgroundTasks()
    chat_req = ChatRequest(session_id=session_id, message=text, user_name=name)
    try:
        chat_resp = await chat_handler(chat_req, bg)
    except Exception as e:
        log.error(f"[delay] chat_handler failed for {session_id}: {e}")
        return

    reply_text = chat_resp.response
    image_url = None
    if chat_resp.products:
        product = chat_resp.products[0]
        reply_text += f"\n\n🛒 *{product.name}*\n💰 N{float(product.price):,.0f}"
        if product.image_url and "img-proxy?url=" in product.image_url:
            from urllib.parse import unquote
            image_url = unquote(product.image_url.split("img-proxy?url=")[1])

    save_message_db(session_id, wa_from, "KoolBot", "outbound", reply_text)
    await send_whatsapp_message(wa_from, reply_text, image_url)

    # Run background tasks queued by chat_handler (save_lead, update_lead_address, etc.)
    for task in bg.tasks:
        try:
            if asyncio.iscoroutinefunction(task.func):
                await task.func(*task.args, **task.kwargs)
            else:
                task.func(*task.args, **task.kwargs)
        except Exception as e:
            log.warning(f"[delay] bg task {task.func.__name__} failed: {e}")


@app.get("/webhook")
async def whatsapp_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        log.info("WhatsApp webhook verified successfully")
        return Response(content=hub_challenge, media_type="text/plain")
    log.warning("WhatsApp webhook verification failed")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    log.info(f"WhatsApp webhook received: {payload}")
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for msg in messages:
                    if msg.get("type") != "text":
                        continue
                    wa_from  = normalize_phone(msg["from"])
                    text     = msg["text"]["body"]
                    contacts = value.get("contacts", [{}])
                    name     = contacts[0].get("profile", {}).get("name", "Customer") if contacts else "Customer"
                    session_id = f"wa_{wa_from}"
                    msg_id = msg.get("id")
                    log.info(f"WhatsApp message from {wa_from} ({name}): {text}")

                    # Mark message as read immediately
                    if msg_id:
                        background_tasks.add_task(mark_whatsapp_read, msg_id)

                    # Save inbound message to DB
                    background_tasks.add_task(save_message_db, session_id, wa_from, name, "inbound", text)

                    # Check if agent has taken over this session
                    handoff_key = f"koolbuy:handoff:{session_id}"
                    in_handoff = await redis_client.get(handoff_key) if redis_client else None
                    if in_handoff:
                        # Auto-reset stale handoffs — if no agent has replied in 8+ hours,
                        # the conversation was abandoned. Let the bot resume.
                        auto_reset_h = int(os.environ.get("HANDOFF_AUTO_RESET_HOURS", "8"))
                        stale = False
                        try:
                            _db = get_db()
                            last_out = _db.execute(
                                select(func.max(Message.created_at))
                                .where(and_(Message.phone == wa_from,
                                            Message.direction == "outbound"))
                            ).scalar()
                            _db.close()
                            if last_out is None or \
                               (datetime.utcnow() - last_out).total_seconds() > auto_reset_h * 3600:
                                stale = True
                        except Exception as _e:
                            log.warning(f"Handoff stale-check failed: {_e}")
                        if stale:
                            await redis_client.delete(handoff_key)
                            in_handoff = None
                            log.info(f"Auto-reset stale handoff for {session_id} (no agent reply in {auto_reset_h}h)")
                        else:
                            log.info(f"Session {session_id} is in handoff mode — bot silent")
                            history = await get_history(session_id)
                            history.append({"role": "user", "content": text,
                                            "ts": datetime.now().isoformat()})
                            await save_history(session_id, history)
                            continue

                    # Check session state and reset completed sessions
                    if redis_client:
                        history_key = f"koolbuy:chat:{session_id}"
                        raw_history = await redis_client.get(history_key)
                        history_text = raw_history if raw_history else ""
                        is_complete = "[VALID phone captured" in history_text and "[DELIVERY confirmed" in history_text

                        if is_complete:
                            await redis_client.delete(history_key)
                            await redis_client.delete(f"koolbuy:phone:{session_id}")
                            await redis_client.delete(f"koolbuy:delivery:{session_id}")
                            log.info(f"Session {session_id} reset for new conversation")

                    # Fire delayed response — gives agents BOT_RESPONSE_DELAY seconds to take over
                    asyncio.create_task(delayed_bot_response(session_id, wa_from, name, text))
    except Exception as e:
        log.error(f"WhatsApp webhook processing error: {e}")
    return Response(content="OK", status_code=200)
