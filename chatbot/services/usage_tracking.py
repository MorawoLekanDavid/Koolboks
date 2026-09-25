from chatbot.config import log
from chatbot.database import get_db
from chatbot.models import ApiUsageLog

# USD per 1M tokens. Groq's published rates as of when this was written —
# verify against https://console.groq.com/docs/model/<model> before trusting
# cost numbers for billing purposes, and update here whenever config.py's
# GROQ_MODEL changes. Keep every model this app has ever actually called
# Groq with, even a retired one — the lookup key is whatever GROQ_MODEL was
# AT THE TIME a call got logged, and old ApiUsageLog rows still carry that
# model name, so removing an entry would silently mis-price historical rows
# the next time anything recomputes from it.
GROQ_PRICING = {
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60},
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
}
# Fallback for any model not in the table above, so cost still computes
# (roughly) instead of silently reading zero. This is what quietly priced
# every openai/gpt-oss-120b call at the llama-3.3-70b rate for as long as
# GROQ_MODEL had no matching entry above -- _warn_once_if_unpriced below
# exists specifically so the next model swap doesn't repeat that silently.
_DEFAULT_PRICING = {"input": 0.59, "output": 0.79}
_warned_unpriced_models = set()


def _warn_once_if_unpriced(model: str) -> None:
    if model in GROQ_PRICING or model in _warned_unpriced_models:
        return
    _warned_unpriced_models.add(model)
    log.warning(
        f"No GROQ_PRICING entry for model '{model}' -- cost numbers for it will use the "
        f"default rate ({_DEFAULT_PRICING}), which is very likely wrong. Add a real entry "
        f"to GROQ_PRICING in chatbot/services/usage_tracking.py."
    )


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    _warn_once_if_unpriced(model)
    rates = GROQ_PRICING.get(model, _DEFAULT_PRICING)
    return (prompt_tokens / 1_000_000) * rates["input"] + (completion_tokens / 1_000_000) * rates["output"]


def log_groq_usage(completion, purpose: str, model: str):
    """Extract token usage from a Groq completion and persist it. Safe to call
    even if `completion.usage` is missing — logs zeros rather than raising,
    since this must never break the actual AI call it's tracking."""
    try:
        usage = getattr(completion, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)

        db = get_db()
        try:
            db.add(ApiUsageLog(
                purpose=purpose,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=_estimate_cost(model, prompt_tokens, completion_tokens),
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        log.warning(f"Failed to log Groq usage for {purpose}: {e}")
