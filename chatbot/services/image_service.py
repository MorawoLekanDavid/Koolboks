import asyncio
from typing import Optional

import httpx

# Simple in-memory cache: url -> (content_type, bytes)
_img_cache: dict = {}
_img_cache_lock = asyncio.Lock()
_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    return _http_client


def cache_get(url: str):
    return _img_cache.get(url)


async def cache_set(url: str, content_type: str, data: bytes):
    # Cache it (limit cache to ~100 images to avoid memory bloat)
    async with _img_cache_lock:
        if len(_img_cache) < 100:
            _img_cache[url] = (content_type, data)
