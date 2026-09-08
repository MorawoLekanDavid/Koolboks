import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

from chatbot.config import BASE_DIR, WHATSAPP_API_TOKEN, WHATSAPP_API_URL, log
from chatbot.core import redis_client
from chatbot.dependencies import get_admin_ctx
from chatbot.services import image_service

router = APIRouter()


@router.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@router.get("/admin")
async def admin_dashboard():
    return FileResponse(
        os.path.join(BASE_DIR, "admin", "index.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/health")
async def health():
    if not redis_client.client:
        return {"status": "ok", "redis": "not configured"}
    try:
        ping = await redis_client.client.ping()
        return {"status": "ok", "redis": "connected" if ping else "error"}
    except Exception:
        return {"status": "ok", "redis": "connection failed"}


@router.get("/img-proxy")
async def image_proxy(url: str = Query(...)):
    """Proxy S3 product images to avoid CORS / direct-access issues in browser."""
    # Security: only allow proxying from our S3 bucket
    if "koolbuy-assets.s3" not in url and "amazonaws.com" not in url:
        raise HTTPException(status_code=403, detail="Forbidden: only koolbuy S3 URLs allowed")

    # Check cache
    cached = image_service.cache_get(url)
    if cached:
        ct, data = cached
        return Response(content=data, media_type=ct,
                        headers={"Cache-Control": "public, max-age=86400"})

    try:
        client = await image_service.get_http_client()
        resp = await client.get(url)
        if resp.status_code != 200:
            log.warning(f"Image proxy: S3 returned {resp.status_code} for {url}")
            raise HTTPException(status_code=resp.status_code, detail="Image fetch failed")

        content_type = resp.headers.get("content-type", "image/jpeg")
        img_bytes = resp.content

        await image_service.cache_set(url, content_type, img_bytes)

        return Response(content=img_bytes, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    except httpx.RequestError as e:
        log.error(f"Image proxy error: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to fetch image: {str(e)}")


@router.get("/admin/media-proxy/{media_id}")
async def whatsapp_media_proxy(media_id: str, ctx: dict = Depends(get_admin_ctx)):
    """Stream a customer-sent WhatsApp image by its media ID, auth-gated same as
    the rest of the admin API. WhatsApp doesn't hand out a stable, browsable URL
    for inbound media — only an ID that resolves to a short-lived, auth-required
    download link — so this re-resolves it live on each view and caches the actual
    bytes in the same in-memory cache used for product images, instead of writing
    anything to disk (this server's already tight on space)."""
    if not WHATSAPP_API_TOKEN:
        raise HTTPException(status_code=503, detail="WhatsApp not configured")

    cache_key = f"wa_media:{media_id}"
    cached = image_service.cache_get(cache_key)
    if cached:
        ct, data = cached
        return Response(content=data, media_type=ct,
                        headers={"Cache-Control": "private, max-age=86400"})

    headers = {"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"}
    try:
        client = await image_service.get_http_client()
        meta_resp = await client.get(f"{WHATSAPP_API_URL}/{media_id}", headers=headers)
        if meta_resp.status_code != 200:
            log.warning(f"Media proxy: lookup failed for {media_id} ({meta_resp.status_code})")
            raise HTTPException(status_code=meta_resp.status_code, detail="Media lookup failed")
        media_url = meta_resp.json().get("url")
        if not media_url:
            raise HTTPException(status_code=502, detail="No media URL returned")

        file_resp = await client.get(media_url, headers=headers)
        if file_resp.status_code != 200:
            log.warning(f"Media proxy: download failed for {media_id} ({file_resp.status_code})")
            raise HTTPException(status_code=file_resp.status_code, detail="Media download failed")

        content_type = file_resp.headers.get("content-type", "image/jpeg")
        img_bytes = file_resp.content
        await image_service.cache_set(cache_key, content_type, img_bytes)

        return Response(content=img_bytes, media_type=content_type,
                        headers={"Cache-Control": "private, max-age=86400"})
    except httpx.RequestError as e:
        log.error(f"WhatsApp media proxy error for {media_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch media")
