import re
from datetime import datetime
from typing import List, Optional

from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from chatbot.config import (
    LEAD_TTL,
    RATE_LIMIT,
    WHATSAPP_CONTACT,
    log,
)
from chatbot.services.ai_settings_service import get_live_content
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Product
from chatbot.services.groq_service import call_groq
from chatbot.services.lead_service import save_lead, update_lead_address
from chatbot.utils.phone import SESSION_ID_RE, extract_valid_phone, phone_from_history


class ChatRequest(BaseModel):
    session_id:    str = Field(...)
    message:       str = Field(..., min_length=1, max_length=2000)
    user_name:     str = Field(default="Customer")
    business_type: str = Field(default="")
    volume:        str = Field(default="")
    power:         str = Field(default="")


class ProductCard(BaseModel):
    name:               str
    price:              str
    image_url:          Optional[str] = None   # proxied URL for browser display
    original_image_url: Optional[str] = None   # raw URL for WhatsApp API
    product_url:        Optional[str] = None
    description:        Optional[str] = None


class ChatResponse(BaseModel):
    session_id:    str
    response:      str
    products:      List[ProductCard] = []
    lead_captured: bool = False


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
                    original_image_url=p.image_url,
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
            original_image_url=best_product.image_url,
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
                        original_image_url=p.image_url,
                        product_url=p.product_url,
                        description=p.description,
                    )]
        except (ValueError, TypeError):
            continue

    return []


async def build_system_prompt(user_name: str, inv: str) -> dict:
    instruction, kb = await get_live_content()
    content = instruction.replace("{user_name}", user_name).replace("{knowledge_base}", kb).replace("{inventory}", inv)
    return {"role": "system", "content": content}


async def chat_handler(request: ChatRequest, background_tasks: BackgroundTasks):
    # Validate session ID format
    if not SESSION_ID_RE.match(request.session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID.")

    # Rate limiting: max RATE_LIMIT user messages per session per hour
    if redis_client.client and request.message.strip() != "__welcome__":
        try:
            rate_key = f"koolbuy:rate:{request.session_id}"
            count = await redis_client.client.incr(rate_key)
            if count == 1:
                await redis_client.client.expire(rate_key, 3600)
            if count > RATE_LIMIT:
                raise HTTPException(status_code=429, detail="Message limit reached. Please start a new conversation.")
        except HTTPException:
            raise
        except Exception as e:
            log.warning(f"Rate limit check failed: {e}")

    df = load_products()
    inv = inventory_text(df)
    system = await build_system_prompt(request.user_name, inv)

    # Welcome
    if request.message.strip() == "__welcome__":
        history = await redis_client.get_history(request.session_id)
        if not history:
            welcome_prompt = (
                f"Greet {request.user_name} warmly in one sentence. "
                f"Then ask what they sell or store. Max 2 sentences total."
            )
            messages = [system, {"role": "user", "content": welcome_prompt}]
            welcome_text = await call_groq(messages, max_tokens=100)
            # Save with timestamp
            await redis_client.save_history(request.session_id, [{
                "role": "assistant",
                "content": welcome_text,
                "ts": datetime.now().isoformat(),
            }])
            return ChatResponse(session_id=request.session_id, response=welcome_text)
        return ChatResponse(session_id=request.session_id, response="")

    # Normal chat
    history = await redis_client.get_history(request.session_id)
    if history and history[0].get("role") == "assistant":
        history = [{"role": "user", "content": "[conversation started]",
                    "ts": history[0].get("ts", "")}] + history

    # Strip ts timestamps before sending to Groq — Groq only accepts role+content
    history_for_groq = [
        {"role": m["role"], "content": m["content"]} for m in history]

    try:
        phone_redis = await redis_client.client.get(f"koolbuy:phone:{request.session_id}") if redis_client.client else None
        delivery_redis = await redis_client.client.get(f"koolbuy:delivery:{request.session_id}") if redis_client.client else None
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
            if redis_client.client and extracted_phone:
                await redis_client.client.set(f"koolbuy:phone:{request.session_id}", extracted_phone, ex=LEAD_TTL)
        state_summary += f"✓ Phone CAPTURED: {extracted_phone}\n"
    else:
        state_summary += "× Phone: NOT YET CAPTURED\n"

    if delivery_captured:
        if redis_client.client and not delivery_redis:
            await redis_client.client.set(f"koolbuy:delivery:{request.session_id}", "captured", ex=LEAD_TTL)
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
        background_tasks.add_task(save_lead, request.user_name, phone, history, request.session_id)
        lead_captured = True
        if redis_client.client:
            await redis_client.client.set(f"koolbuy:phone:{request.session_id}", phone, ex=LEAD_TTL)
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
            if redis_client.client:
                await redis_client.client.set(f"koolbuy:delivery:{request.session_id}", "captured", ex=LEAD_TTL)
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
    await redis_client.save_history(request.session_id, history)

    return ChatResponse(session_id=request.session_id, response=clean, products=cards, lead_captured=lead_captured)
