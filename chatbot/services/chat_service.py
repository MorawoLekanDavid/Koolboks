import re
from datetime import datetime
from typing import List, Optional

from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from chatbot.config import (
    BOT_NAME,
    LEAD_TTL,
    RATE_LIMIT,
    WHATSAPP_CONTACT,
    log,
)
from chatbot.services.ai_settings_service import get_live_content
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.models import Message, Product
from chatbot.services.groq_service import call_groq
from chatbot.services.lead_service import save_lead, update_lead_address
from chatbot.utils.phone import SESSION_ID_RE, extract_valid_phone, normalize_phone, phone_from_history

# Customer explicitly asking to reset the conversation — "let's start from scratch",
# "start over", "restart", etc. Distinct from FILLER_ACK_RE in webhook.py, which
# handles the opposite case (a bare "ok"/"thanks" that should NOT reset anything).
RESTART_RE = re.compile(
    r'\b(start\s*(over|again|afresh|from\s*scratch)|restart|reset\s*(this\s*|the\s*)?conversation)\b',
    re.IGNORECASE,
)

# Customer consents to using the WhatsApp number they're already texting from
# instead of typing it out — a real, recurring pattern ("use this number",
# "this is my number am chatting with"). Without this, extract_valid_phone()
# finds no digits, so nothing gets captured: the model still generates a
# plausible-sounding "thanks for your number" reply from context alone, while
# the system captures nothing — a real customer ready to buy silently falls
# through to "drop-off" with no phone on file at all.
PHONE_CONSENT_RE = re.compile(
    r'\buse\s+(this|the|my)\s+(whatsapp\s+)?number\b|'
    r'\bthis\s+(is\s+)?(my\s+)?(phone\s+)?number\b.{0,25}\b(chatting|texting|messaging)\b|'
    r'\bnumber\b.{0,15}\b(am|i\'?m|i\s+am)\s+(chatting|texting|messaging)\b|'
    r'\bsame\s+number\b',
    re.IGNORECASE,
)

# A bare "Hi"/"Hello" with nothing else is overwhelmingly the most common first
# message (confirmed reading real transcripts) — anything beyond a plain greeting
# (a question, a product mention, a number) fails this and falls through to the
# model as normal, so the FIRST MESSAGE / WELCOME carve-out for a specific first
# request still applies exactly as before.
BARE_GREETING_RE = re.compile(
    r'^\s*(hi+|hello+|hey+|hiya|yo|good\s*(morning|afternoon|evening|day)|greetings)'
    r'\s*(there)?[^a-zA-Z0-9]*$',
    re.IGNORECASE,
)


class ChatRequest(BaseModel):
    session_id:      str = Field(...)
    message:         str = Field(..., min_length=1, max_length=2000)
    user_name:       str = Field(default="Customer")
    business_type:   str = Field(default="")
    volume:          str = Field(default="")
    power:           str = Field(default="")
    reply_to_wamid:  Optional[str] = Field(default=None)


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


def fixed_welcome_text(name: str = "") -> str:
    """The one, single source of truth for the fixed brand welcome — every
    caller (the website's own sentinel path, the WhatsApp bare-greeting
    short-circuit, and the marker the model can trigger for any other
    non-request opener) must go through this, not carry its own copy of the
    literal string. That's not just tidiness: three independent copies is
    three chances for one of them to drift, which is exactly the class of
    bug this function exists to close off for good."""
    name_part = f", {name}" if name and name != "Customer" else ""
    return (
        f"Hi there{name_part}! 👋🏽 Welcome to Koolboks! ❄️\n\n"
        "Let's take the heat off! ☀️ What are you looking to keep Kool today? 😊\n\n"
        "Tell us what you need, and we'll help you find the right solution."
    )


def resolve_reply_to_product(reply_to_wamid: Optional[str], products: List[Product]) -> Optional[str]:
    """A customer can use WhatsApp's own "reply to this message" feature to
    quote a specific product photo the bot sent ("this one" while replying to
    the 2nd of 4 pictures) — real, observed failure: the bot had no way to
    know which photo, kept asking the customer to repeat which product they
    meant, and even a human agent reading the transcript couldn't tell either,
    since the quote relationship wasn't captured or shown anywhere. Resolves
    the quoted wamid back to the actual product by matching the stored
    "[image]<url>[/image]" row's URL against real INVENTORY."""
    if not reply_to_wamid:
        return None
    db = get_db()
    try:
        quoted = db.query(Message).filter(Message.wamid == reply_to_wamid).first()
        if not quoted or not quoted.content:
            return None
        m = re.match(r'\[image\](.+)\[/image\]', quoted.content.strip())
        if not m:
            return None
        quoted_url = m.group(1)
        for p in products:
            if p.image_url == quoted_url:
                return p.name
        return None
    finally:
        db.close()


def fix_multi_product_prices(raw: str, cards: list) -> str:
    """Same trust problem the single-product price guard exists for, extended
    to the multi-product comparison case it deliberately skips. Real, observed
    bug: listing several products, the model priced the first one correctly
    then reused that exact figure for the next two — three different products,
    two different real prices, one number in the message. Checked per line
    (the model's own bullet-per-product format) against only the products it
    already tagged in this reply (`cards`, already resolved to real DB rows),
    never the wider catalog — this catalog has several near-identical SKUs
    differing only by an add-on (a solar panel, an inverter), so scoring
    against everything risks confidently "fixing" a price to the WRONG
    neighbor instead of catching a real mistake. Requires prices on 2+
    different lines before it applies at all; a single stray price with no
    product words nearby is left alone rather than guessed at."""
    if len(cards) < 2:
        return raw
    price_re = re.compile(r"[N₦]\s?\d[\d,]*(?:\.\d+)?")
    lines = raw.split("\n")
    if sum(1 for ln in lines if price_re.search(ln)) < 2:
        return raw
    fixed_lines = []
    for line in lines:
        m = price_re.search(line)
        if not m:
            fixed_lines.append(line)
            continue
        line_lower = line.lower()
        # Score against every card, not just track the best one — several
        # cards can plausibly share descriptive words (e.g. a base unit and
        # its +panel upgrade both look "solar"-ish in casual phrasing), and
        # picking a close-scoring runner-up with false confidence is worse
        # than leaving an already-wrong price alone. Require a real margin.
        scored = []
        for card in cards:
            words = set(w for w in re.findall(r"[a-z0-9]+", card.name.lower()) if len(w) > 2)
            scored.append((sum(1 for w in words if w in line_lower), card))
        scored.sort(key=lambda x: -x[0])
        best_score, best_card = scored[0]
        runner_up_score = scored[1][0] if len(scored) > 1 else 0
        if best_card and best_score >= 2 and (best_score - runner_up_score) >= 2:
            try:
                correct_price = float(best_card.price)
                stated = float(m.group(0)[1:].replace(",", "").replace("₦", "").strip())
                if abs(stated - correct_price) > 1:
                    line = line[:m.start()] + f"N{correct_price:,.0f}" + line[m.end():]
            except (ValueError, TypeError):
                pass
        fixed_lines.append(line)
    return "\n".join(fixed_lines)


def bnpl_breakdown(price: float) -> str:
    """20% down, zero-interest balance over up to 23 months. Computed here rather than
    left to the model — a wrong monthly figure quoted to a customer is a real trust
    problem, and this is simple arithmetic that doesn't belong in free-form generation."""
    down = round(price * 0.20)
    monthly = round((price - down) / 23)
    return f"Payment plan: N{down:,.0f} down (20%), then N{monthly:,.0f}/month for up to 23 months at zero interest."


def proxy_image_url(original_url: Optional[str]) -> Optional[str]:
    """Rewrite an S3 image URL to go through our /img-proxy endpoint.
    This avoids CORS / direct-access errors in the browser."""
    if not original_url:
        return None
    from urllib.parse import quote
    return f"/img-proxy?url={quote(original_url, safe='')}"


def match_products(products: List[Product], names: List[str]) -> List[ProductCard]:
    """Match requested product names (from the PRODUCTS: tag) with real
    inventory rows. Tries exact substring containment first — fast and
    precise on the common case where the model copies a name verbatim. Falls
    back to fuzzy word-overlap scoring when that fails, since the model
    doesn't always reproduce a name exactly (reordered words, a dropped
    parenthetical) — without this fallback that product silently drops out
    of the result entirely, which is how a multi-product reply's downstream
    price-accuracy check (fix_multi_product_prices, gated on having 2+ cards
    to check against) ends up with too few products to catch a wrong price
    against — a real, observed failure: a 4-product listing where only the
    first name matched exactly, so the other three products' wrong prices
    went completely unvalidated."""
    if not products or not names:
        return []

    cards = []
    seen = set()
    for req_name in names:
        req_lower = req_name.strip().lower()
        matched = None
        for p in products:
            if req_lower in p.name.lower() and p.id not in seen:
                matched = p
                break
        if not matched:
            req_words = set(w for w in re.findall(r"[a-z0-9]+", req_lower) if len(w) > 2)
            best_score, best_p = 0, None
            for p in products:
                if p.id in seen:
                    continue
                p_words = set(w for w in re.findall(r"[a-z0-9]+", p.name.lower()) if len(w) > 2)
                score = len(req_words & p_words)
                if score > best_score:
                    best_score, best_p = score, p
            if best_score >= 3:
                matched = best_p
        if matched:
            cards.append(ProductCard(
                name=matched.name,
                price=str(matched.price),
                image_url=proxy_image_url(matched.image_url),
                original_image_url=matched.image_url,
                product_url=matched.product_url,
                description=matched.description,
            ))
            seen.add(matched.id)
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


async def build_system_prompt(user_name: str, inv: str, query_text: str = "") -> dict:
    instruction, kb = await get_live_content(query_text)
    content = (instruction.replace("{bot_name}", BOT_NAME).replace("{user_name}", user_name)
               .replace("{knowledge_base}", kb).replace("{inventory}", inv))
    return {"role": "system", "content": content}


async def generate_chat_response(request: ChatRequest, background_tasks: BackgroundTasks):
    """Does everything chat_handler does EXCEPT commit the turn to Redis
    history — returns (ChatResponse, persist), where persist is a zero-arg
    async callable that actually writes it. Split out for the WhatsApp
    debounce path: that worker generates a reply, then re-checks whether a
    newer customer message has arrived before deciding to actually send it.
    Calling the old all-in-one chat_handler and discarding its result on a
    late supersession still left the discarded reply saved to history as if
    it had really been sent — corrupting every later turn's context, since
    the model would "remember" saying something the customer never saw.
    chat_handler() below is the same as before for every other caller: it
    generates and immediately persists, unconditionally."""
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
    is_welcome_sentinel = request.message.strip() == "__welcome__"
    system = await build_system_prompt(request.user_name, inv, "" if is_welcome_sentinel else request.message)

    # Welcome — the widget shows its own fixed brand greeting (index.html)
    # before it ever asks for a name or calls this, so by the time __welcome__
    # arrives here the customer has already been welcomed and already asked
    # what they need. Generating and showing a second, LLM-written welcome on
    # top of that would repeat the greeting and re-ask a question they weren't
    # given yet a chance to answer. Seed history with that same fixed text
    # (so the model treats Step 1 as already done on their next real message,
    # instead of firing it again) and just acknowledge the name — no LLM call
    # needed for this at all anymore.
    if is_welcome_sentinel:
        history = await redis_client.get_history(request.session_id)
        if not history:
            welcome_text = fixed_welcome_text()
            # Re-anchors the question the welcome already asked, rather than leaving
            # the customer on a bare "nice to meet you" with no sense of what to do
            # next — real customers replied "okay but that's not why i'm here" to the
            # unadorned version, since the pending question was two messages back by
            # the time they'd finished giving their name.
            name_part = f", {request.user_name}" if request.user_name else ""
            ack_text = f"Nice to meet you{name_part}! 😊 So — what are you looking to keep Kool today?"

            async def _persist_welcome():
                await redis_client.save_history(request.session_id, [{
                    "role": "assistant",
                    "content": welcome_text,
                    "ts": datetime.now().isoformat(),
                }])
            return ChatResponse(session_id=request.session_id, response=ack_text), _persist_welcome

        async def _persist_noop():
            pass
        return ChatResponse(session_id=request.session_id, response=""), _persist_noop

    # Normal chat
    history = await redis_client.get_history(request.session_id)

    # A bare greeting as the very first message needs no LLM call at all — it's
    # always the same fixed welcome, so send it directly. Confirmed live: even
    # with an explicit "word-for-word, never reworded" instruction, the model
    # doesn't always reproduce this reliably — one real reply silently
    # "corrected" the intentional Koolboks pun "Kool" to "cool". A message this
    # fixed doesn't need to go through the model at all.
    if not history and BARE_GREETING_RE.match(request.message.strip()):
        welcome_text = fixed_welcome_text(request.user_name)

        async def _persist_bare_greeting():
            now = datetime.now().isoformat()
            await redis_client.save_history(request.session_id, [
                {"role": "user", "content": request.message, "ts": now},
                {"role": "assistant", "content": welcome_text, "ts": now},
            ])
        return ChatResponse(session_id=request.session_id, response=welcome_text), _persist_bare_greeting

    # Explicit customer request to restart — the CRITICAL MEMORY RULE in the system
    # prompt otherwise makes the model plow through the existing flow no matter what
    # the customer says, so this has to be handled here rather than left to the LLM.
    if history and RESTART_RE.search(request.message):
        history = []
        if redis_client.client:
            await redis_client.client.delete(f"koolbuy:chat:{request.session_id}")
            await redis_client.client.delete(f"koolbuy:phone:{request.session_id}")
            await redis_client.client.delete(f"koolbuy:delivery:{request.session_id}")
        log.info(f"Session {request.session_id} restarted by customer request")

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
    if not history:
        state_summary += (
            f"✓ FIRST MESSAGE — this is {request.user_name}'s very first message in this "
            f"conversation (or they just asked to restart). See FIRST MESSAGE / WELCOME "
            f"below for the fixed welcome text — but check that section's own carve-out "
            f"first: if their message already reads like a specific request or a "
            f"continuation of something in progress, that takes priority over the welcome.\n"
        )
        # A real WhatsApp session with genuinely empty history is either a brand-new
        # contact or one that legitimately restarted — but if this phone already has
        # prior outbound messages in the DB, an empty history here means something
        # reset it unexpectedly. There's a confirmed occurrence of this we couldn't
        # trace after the fact (no logs survived); this makes any recurrence visible
        # immediately instead of only discoverable by re-reading a transcript later.
        if request.session_id.startswith("wa_"):
            try:
                _db = get_db()
                phone = request.session_id[3:]
                prior = _db.query(Message).filter(
                    Message.phone == phone, Message.direction == "outbound"
                ).first()
                _db.close()
                if prior:
                    log.warning(
                        f"Session {request.session_id} has empty history but {phone} has "
                        f"prior outbound messages in the DB — likely an unexpected session "
                        f"reset, not a genuinely new contact."
                    )
            except Exception as e:
                log.warning(f"First-message reset diagnostic check failed: {e}")
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

    replied_product = resolve_reply_to_product(request.reply_to_wamid, df)
    state_summary += "───────────────────"

    messages = [system, {"role": "user", "content": state_summary}] + history_for_groq + \
        [{"role": "user", "content": request.message}]

    phone = extract_valid_phone(request.message)
    if (not phone and not already_captured and request.session_id.startswith("wa_")
            and PHONE_CONSENT_RE.search(request.message)):
        phone = normalize_phone(request.session_id[3:])
    lead_captured = False
    looks_like_phone = bool(
        re.search(r'\b0\d{7,11}\b|\+234\d{7,11}\b|\+254\d{6,9}\b|\+256\d{6,9}\b|\b[789]\d{9}\b', request.message))

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
                f"If this message ALSO contains a genuine question — not just the address — "
                f"answer it briefly first. Don't drop it just because the address was captured. "
                f"Proceed to closing message.]"
            )
    elif looks_like_phone and not already_captured:
        messages[-1]["content"] = (
            f"{request.message}\n\n"
            f"[INVALID phone. If this message also contains OTHER details "
            f"(name, location, product, plan, etc.), briefly acknowledge those "
            f"first in one short clause — don't just ignore them. Then ask for "
            f"a valid number — Nigerian (11 digits starting 070/080/081/090/091), "
            f"Kenyan (+254 7XX or 1XX XXX XXX), or Ugandan (+256 7XX XXX XXX) — "
            f"Koolbuy ships to all three countries, don't assume Nigeria. Keep "
            f"the whole reply to 2 sentences.]"
        )

    # Appended directly onto the CURRENT message (not the earlier CAPTURED
    # STATE block) deliberately — that's the same high-salience spot the
    # phone/delivery markers above use, right next to what the model is about
    # to respond to. Confirmed live this actually matters, not just style: on
    # a long, messy real conversation (16+ turns, the same ambiguity hit three
    # times), the identical hint sitting in CAPTURED STATE earlier in context
    # got silently dropped entirely — not even hedged on, just ignored — while
    # this position held on a apples-to-apples replay of the same failure.
    if replied_product:
        messages[-1]["content"] += (
            f"\n\n[CONFIRMED: this message is a WhatsApp reply directly to a photo of "
            f"\"{replied_product}\" — not a guess, this is which product they tapped "
            f"reply on. Treat it exactly as if they'd typed that full name. Do NOT ask "
            f"them to confirm or choose between it and a similar variant (different "
            f"battery, panel count, alone vs bundled) — answer about THIS exact product.]"
        )

    raw = await call_groq(messages)

    # FIRST MESSAGE / WELCOME (prompt) tells the model to output this marker,
    # verbatim and alone, for any opener that isn't a specific request rather
    # than trying to write the welcome text itself — a classification task,
    # not a generation one. This is the substitution that makes that promise
    # real: the model's actual words here are discarded and replaced with the
    # one deterministic source of truth, closing off the whole class of bug
    # where an LLM asked to reproduce fixed text verbatim occasionally doesn't
    # (confirmed live, twice, on this exact text before this fix existed).
    if "[SEND_FIXED_WELCOME]" in raw:
        raw = fixed_welcome_text(request.user_name)

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

    # Guard against the model stating a different Naira price in its own
    # sentence than the real price of the single product it just tagged —
    # a real, observed bug: near-identical product names (same freezer,
    # "+ solar panel" suffix the only difference) sit close together in the
    # inventory list, and the model sometimes recalls the wrong neighbor's
    # price. Same trust-problem class bnpl_breakdown() exists to prevent,
    # just for the sticker price instead of the monthly math — this must
    # run before that gets appended below, or it would also try to "fix"
    # the down-payment/monthly figures to match the full price. Only safe
    # to blanket-replace when exactly one product is being priced; a
    # multi-product comparison legitimately mentions more than one price.
    if len(cards) == 1:
        try:
            correct_price = float(cards[0].price)
            correct_str = f"N{correct_price:,.0f}"

            def _fix_price(match: re.Match) -> str:
                stated = float(match.group(0)[1:].replace(",", "").replace("₦", "").strip())
                return correct_str if abs(stated - correct_price) > 1 else match.group(0)

            raw = re.sub(r"[N₦]\s?\d[\d,]*(?:\.\d+)?", _fix_price, raw)
        except (ValueError, TypeError):
            pass
    elif len(cards) > 1:
        raw = fix_multi_product_prices(raw, cards)

    # Strip a stray extra number the model occasionally appends right after
    # a price with nothing between them — observed live, reproducibly, as
    # "N1,950,000 953 000". _fix_price above only matches from the currency
    # symbol up to the first non-digit/comma character (the space before the
    # stray number), so it never sees this trailing fragment to validate or
    # remove. A customer skimming this could easily misread it as one much
    # larger number. Only strips a bare thousand-grouped number sitting
    # immediately after a price with just whitespace between them — real
    # sentences have a word there ("down", "for"), so this shouldn't catch
    # legitimate text.
    raw = re.sub(r"([N₦]\d[\d,]*(?:\.\d+)?)\s+\d{2,3}[,\s]\d{3}(?!\d)", r"\1", raw)

    # Attach an accurate payment-plan breakdown whenever a single product is being
    # priced — computed here instead of trusting the model's mental math, and shown
    # proactively rather than only after the customer objects to the price. Skipped
    # for multi-product comparisons (too cluttered) and if the model already wrote
    # its own breakdown (defensive — the prompt asks it not to, but this avoids a
    # duplicate/conflicting figure on the rare turn it does anyway).
    if len(cards) == 1 and "23 months" not in raw:
        try:
            price_val = float(cards[0].price)
            if price_val > 0:
                raw = f"{raw}\n\n{bnpl_breakdown(price_val)}"
        except (ValueError, TypeError):
            pass

    clean = re.sub(r'PRODUCTS:\s*.+\n?', '', raw, flags=re.IGNORECASE).strip()

    # The prompt explicitly tells the model to re-output a bare PRODUCTS tag
    # to resend a picture ("re-output the tag with the exact name"), so a
    # reply that's ONLY that tag is expected and valid — but stripping the
    # tag then leaves `clean` empty, which both callers (the website widget
    # and the WhatsApp send) treat as "nothing to show": the widget displays
    # a raw "No response" error to the customer, and WhatsApp would try to
    # send an empty-text message alongside the image. Give it real text
    # either way so the customer never sees an empty/broken-looking reply.
    if not clean:
        clean = "Here you go! 👇" if cards else "Sorry, could you say that again? I want to make sure I get you the right info."

    now = datetime.now().isoformat()
    # Annotated message (with phone note) plus the reply, ready to commit to
    # Redis history with timestamp — committed by persist(), not here, so a
    # caller can still decide not to.
    user_content = messages[-1]["content"] if messages[-1]["role"] == "user" else request.message
    history.append({"role": "user",      "content": user_content, "ts": now})
    history.append(
        {"role": "assistant", "content": raw,                     "ts": now})

    async def _persist():
        await redis_client.save_history(request.session_id, history)

    return ChatResponse(session_id=request.session_id, response=clean, products=cards, lead_captured=lead_captured), _persist


async def chat_handler(request: ChatRequest, background_tasks: BackgroundTasks):
    """Generate a reply and commit it to history unconditionally — the
    original chat_handler behavior, for the direct /chat API and any other
    caller that always wants to send whatever comes back. The WhatsApp
    debounce path uses generate_chat_response() directly instead, so it can
    decide whether to persist before committing to anything."""
    chat_resp, persist = await generate_chat_response(request, background_tasks)
    await persist()
    return chat_resp
