import httpx
from fastapi import HTTPException
from groq import AsyncGroq

from chatbot.config import GROQ_API_KEY, GROQ_MODEL, log
from chatbot.utils.text import strip_internal_notes

groq_client = AsyncGroq(
    api_key=GROQ_API_KEY,
    http_client=httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
    ),
)


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
