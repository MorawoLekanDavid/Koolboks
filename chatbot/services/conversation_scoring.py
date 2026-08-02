import json
from typing import List, Optional

from chatbot.config import GROQ_MODEL, log
from chatbot.models import Message
from chatbot.services.groq_service import groq_client
from chatbot.services.usage_tracking import log_groq_usage

# Fixed vocabulary so issue tags are consistent and filterable in analytics.
# "no_response_at_all" is assigned deterministically by the worker, never by the LLM.
ISSUE_TAGS = [
    "repeated_question",
    "ignored_customer_question",
    "generic_answer_accepted",
    "irrelevant_recommendation",
    "unclear_or_confusing",
    "customer_frustration",
    "long_response_gap",
    "no_response_at_all",
]

_LLM_SELECTABLE_TAGS = [t for t in ISSUE_TAGS if t != "no_response_at_all"]


def _responder_type(messages: List[Message]) -> Optional[str]:
    responders = {m.name for m in messages if m.direction == "outbound"}
    if not responders:
        return None
    if responders == {"KoolBot"}:
        return "bot"
    if "KoolBot" not in responders:
        return "agent"
    return "mixed"


async def score_conversation(messages: List[Message]) -> Optional[dict]:
    """Ask the LLM to rate how well the bot/agent handled this chunk of conversation.
    Returns None if messages is empty or the LLM call/parse fails."""
    if not messages:
        return None

    lines = []
    for m in messages:
        role = "Customer" if m.direction == "inbound" else (
            "Bot" if m.name == "KoolBot" else f"Agent({m.name})"
        )
        ts = m.created_at.strftime("%H:%M") if m.created_at else "?"
        lines.append(f"[{ts}] {role}: {(m.content or '')[:300]}")
    transcript = "\n".join(lines)

    prompt = (
        "You are auditing a WhatsApp sales conversation for a Nigerian solar-freezer company. "
        "Rate how well the seller side (bot or human agent) handled the customer in this excerpt — "
        "NOT how qualified or interested the customer is.\n\n"
        f"CONVERSATION:\n{transcript}\n\n"
        "Reply ONLY with valid JSON, no markdown, no extra text:\n"
        '{"quality_score": 0, "likely_lost_customer": false, "issues": [], "reasoning": ""}\n\n'
        "Rules:\n"
        "- quality_score: integer 1-10. 10 = handled perfectly, warm, relevant, and helpful. "
        "1 = actively drove the customer away. If likely_lost_customer is true, quality_score "
        "MUST be 4 or lower — losing the customer is the outcome that matters most, regardless "
        "of how well earlier turns went.\n"
        "- likely_lost_customer: true only if the customer seems to have disengaged, gone quiet "
        "right after a poor reply, or expressed frustration BECAUSE of how they were handled — "
        "not simply because the excerpt ends before they replied again.\n"
        f"- issues: pick zero or more from EXACTLY this list, no others: {_LLM_SELECTABLE_TAGS}\n"
        "  repeated_question = seller re-asked something the customer already answered\n"
        "  ignored_customer_question = customer asked something and the seller never addressed it\n"
        "  generic_answer_accepted = seller treated a vague reply (yes/ok/sure) as a specific answer\n"
        "  irrelevant_recommendation = recommended something that doesn't match the customer's stated need\n"
        "  unclear_or_confusing = reply was hard to follow, rambling, or self-contradictory\n"
        "  customer_frustration = customer explicitly showed annoyance or impatience\n"
        "  long_response_gap = seller took an unusually long time to reply, based on the timestamps\n"
        "- reasoning: ONE short sentence explaining the score.\n"
    )

    try:
        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            temperature=0,
        )
        log_groq_usage(completion, "conversation_scoring", GROQ_MODEL)
        raw = (completion.choices[0].message.content or "").strip()
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        data = json.loads(raw)
    except Exception as e:
        log.warning(f"Conversation scoring LLM call/parse failed: {e}")
        return None

    score = data.get("quality_score")
    try:
        score = int(round(float(score)))
    except (TypeError, ValueError):
        return None
    if not (1 <= score <= 10):
        return None

    issues = [t for t in (data.get("issues") or []) if t in _LLM_SELECTABLE_TAGS]
    likely_lost_customer = bool(data.get("likely_lost_customer", False))

    # Hard constraint, not just a prompt request: losing the customer IS the
    # quality failure that matters most, regardless of how smoothly earlier
    # turns went. Never let a "likely lost" conversation carry a decent score.
    if likely_lost_customer and score > 4:
        score = 4

    return {
        "quality_score": score,
        "likely_lost_customer": likely_lost_customer,
        "issues": issues,
        "reasoning": str(data.get("reasoning", ""))[:500],
        "responder_type": _responder_type(messages),
    }
