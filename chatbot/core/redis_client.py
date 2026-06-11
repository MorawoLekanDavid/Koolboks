import json
from typing import Optional

import redis.asyncio as aioredis

from chatbot.config import REDIS_URL, MAX_HISTORY, CHAT_TTL, log

client: Optional[aioredis.Redis] = None


async def connect():
    global client
    try:
        client = await aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
        await client.ping()
        log.info("Redis connected successfully")
    except Exception as e:
        log.warning(f"Redis connection failed: {e}. Chat history will not be persisted.")
        client = None


async def disconnect():
    global client
    if client:
        await client.close()
        log.info("Redis disconnected")


async def get_history(session_id: str) -> list:
    if not client:
        return []
    try:
        raw = await client.get(f"koolbuy:chat:{session_id}")
        return json.loads(raw) if raw else []
    except Exception:
        return []


async def save_history(session_id: str, history: list):
    if not client:
        return
    try:
        trimmed = history[-MAX_HISTORY:]
        await client.set(f"koolbuy:chat:{session_id}", json.dumps(trimmed), ex=CHAT_TTL)
    except Exception:
        pass
