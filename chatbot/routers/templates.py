import json
import re
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
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
from chatbot.dependencies import get_admin_ctx, require_admin
from chatbot.services.whatsapp_service import save_message_db
from chatbot.utils.phone import normalize_phone

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
        save_message_db(session_id, norm, agent_name, "outbound",
                        f"[Template: {body.template_name}]" + (f" — {', '.join(body.variables)}" if body.variables else ""))
    data = r.json()
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=data)
    return data


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
