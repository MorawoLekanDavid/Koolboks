import asyncio
import json
import re
import uuid
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from chatbot.config import (
    REENGAGEMENT_TEMPLATE,
    REENGAGEMENT_TEMPLATE_LANG,
    WABA_ID,
    WHATSAPP_API_TOKEN,
    WHATSAPP_API_URL,
    WHATSAPP_PHONE_NUMBER_ID,
)
from chatbot.core import redis_client
from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx, require_admin
from chatbot.models import ConversationOwner
from chatbot.services.whatsapp_service import save_message_db
from chatbot.utils.phone import normalize_phone
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/admin", tags=["templates"])


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


@router.get("/templates")
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


@router.post("/templates")
async def create_template(body: CreateTemplateRequest, ctx: dict = Depends(require_admin)):
    if not WABA_ID or not WHATSAPP_API_TOKEN:
        raise HTTPException(status_code=400, detail="WABA_ID or API token not configured")
    components = []
    if body.header:
        components.append({"type": "HEADER", "format": "TEXT", "text": body.header})
    body_comp: dict = {"type": "BODY", "text": body.body}
    # Meta requires sample values for every {{n}} variable
    var_count = len(set(re.findall(r'\{\{\d+\}\}', body.body)))
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


@router.delete("/templates/{template_name}")
async def delete_template(template_name: str, ctx: dict = Depends(require_admin)):
    if not WABA_ID or not WHATSAPP_API_TOKEN:
        raise HTTPException(status_code=400, detail="WABA_ID or API token not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.delete(
            f"{WHATSAPP_API_URL}/{WABA_ID}/message_templates",
            params={"name": template_name, "access_token": WHATSAPP_API_TOKEN}
        )
    return {"success": r.is_success}


@router.post("/conversations/{phone}/send-template")
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
        agent_email = ctx.get("email", "")
        save_message_db(session_id, norm, agent_name, "outbound",
                        f"[Template: {body.template_name}]" + (f" — {', '.join(body.variables)}" if body.variables else ""))

        def _claim_owner():
            db = get_db()
            try:
                existing = db.query(ConversationOwner).filter(ConversationOwner.phone == norm).first()
                if not existing:
                    db.add(ConversationOwner(phone=norm, owner_name=agent_name, owner_email=agent_email))
                    db.commit()
            finally:
                db.close()
        await run_in_threadpool(_claim_owner)

    data = r.json()
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=data)
    return data


class BulkBroadcastRequest(BaseModel):
    phones: List[str]
    template_name: str
    language: str = "en"
    variables: List[str] = []


async def _run_bulk_broadcast(job_id: str, phones: List[str], template_name: str,
                               language: str, variables: List[str], agent_name: str):
    """Background task: send template to each phone, track progress in Redis."""
    total = len(phones)
    failed = []

    async with httpx.AsyncClient(
        timeout=15.0,
        transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
    ) as client:
        for i, raw_phone in enumerate(phones):
            try:
                norm = normalize_phone(raw_phone)
                wa_to = norm.lstrip("+")

                payload: dict = {
                    "messaging_product": "whatsapp",
                    "to": wa_to,
                    "type": "template",
                    "template": {
                        "name": template_name,
                        "language": {"code": language},
                    },
                }
                if variables:
                    payload["template"]["components"] = [{
                        "type": "body",
                        "parameters": [{"type": "text", "text": v} for v in variables],
                    }]

                r = await client.post(
                    f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                    json=payload,
                    headers={"Authorization": f"Bearer {WHATSAPP_API_TOKEN}"},
                )

                if r.is_success:
                    session_id = f"wa_{norm}"
                    save_message_db(
                        session_id, norm, agent_name, "outbound",
                        f"[Template: {template_name}]" + (f" — {', '.join(variables)}" if variables else ""),
                    )
                else:
                    failed.append({"phone": raw_phone, "error": r.text[:120]})

            except Exception as e:
                failed.append({"phone": raw_phone, "error": str(e)[:120]})

            # Update progress in Redis every message
            if redis_client.client:
                sent = i + 1 - len(failed)
                progress = {
                    "status": "running",
                    "total": total,
                    "sent": sent,
                    "failed_count": len(failed),
                    "failed": failed[-20:],  # keep last 20 failures max
                    "current": i + 1,
                }
                await redis_client.client.set(
                    f"koolbuy:broadcast:{job_id}", json.dumps(progress), ex=3600
                )

            # 300 ms between sends — ~3/sec, stays within Meta limits
            await asyncio.sleep(0.3)

    # Final status
    if redis_client.client:
        final = {
            "status": "done",
            "total": total,
            "sent": total - len(failed),
            "failed_count": len(failed),
            "failed": failed,
            "finished_at": datetime.utcnow().isoformat(),
        }
        await redis_client.client.set(
            f"koolbuy:broadcast:{job_id}", json.dumps(final), ex=86400
        )


@router.post("/templates/bulk-broadcast")
async def bulk_broadcast(
    body: BulkBroadcastRequest,
    background_tasks: BackgroundTasks,
    ctx: dict = Depends(get_admin_ctx),
):
    if not body.phones:
        raise HTTPException(400, "No phone numbers provided")
    if len(body.phones) > 1000:
        raise HTTPException(400, "Maximum 1,000 numbers per broadcast")
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise HTTPException(400, "WhatsApp API not configured")

    # Deduplicate
    seen: set = set()
    clean: List[str] = []
    for p in body.phones:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            clean.append(p)

    job_id = uuid.uuid4().hex
    agent_name = ctx.get("name", "Agent")

    # Seed Redis with initial state so /status is immediately available
    if redis_client.client:
        await redis_client.client.set(
            f"koolbuy:broadcast:{job_id}",
            json.dumps({"status": "running", "total": len(clean), "sent": 0,
                        "failed_count": 0, "failed": [], "current": 0}),
            ex=3600,
        )

    background_tasks.add_task(
        _run_bulk_broadcast, job_id, clean,
        body.template_name, body.language, body.variables, agent_name,
    )
    return {"job_id": job_id, "total": len(clean)}


@router.get("/templates/bulk-broadcast/{job_id}")
async def bulk_broadcast_status(job_id: str, ctx: dict = Depends(get_admin_ctx)):
    if not redis_client.client:
        raise HTTPException(503, "Redis unavailable")
    raw = await redis_client.client.get(f"koolbuy:broadcast:{job_id}")
    if not raw:
        raise HTTPException(404, "Job not found or expired")
    return json.loads(raw)


@router.post("/templates/reengagement-config")
async def set_reengagement_template(body: dict, ctx: dict = Depends(require_admin)):
    """Store the chosen re-engagement template name in Redis so the worker picks it up."""
    if not redis_client.client:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    name = body.get("template_name", "")
    lang = body.get("language", "en")
    enabled = body.get("enabled", True)
    await redis_client.client.set("koolbuy:reengagement_config", json.dumps({"name": name, "lang": lang, "enabled": enabled}))
    return {"ok": True}


@router.get("/templates/reengagement-config")
async def get_reengagement_config(ctx: dict = Depends(get_admin_ctx)):
    if not redis_client.client:
        return {"name": REENGAGEMENT_TEMPLATE, "lang": REENGAGEMENT_TEMPLATE_LANG, "enabled": bool(REENGAGEMENT_TEMPLATE)}
    raw = await redis_client.client.get("koolbuy:reengagement_config")
    if raw:
        return json.loads(raw)
    return {"name": REENGAGEMENT_TEMPLATE, "lang": REENGAGEMENT_TEMPLATE_LANG, "enabled": bool(REENGAGEMENT_TEMPLATE)}
