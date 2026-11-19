"""Meta's reason for a failed send must be visible -- both in logs (synchronous
API rejection) and in delivery_error (async 'failed' status webhook). Without
it, "messages not delivering" can't be told apart: payment vs token vs Meta."""
import asyncio
import os

os.environ.setdefault("GROQ_API_KEY", "test-dummy")  # groq client is built at import time
import logging

import httpx
import pytest

from chatbot.services import whatsapp_service as ws


def _patch_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("transport", None)
        return real(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))

    monkeypatch.setattr(ws.httpx, "AsyncClient", factory)
    monkeypatch.setattr(ws, "WHATSAPP_API_TOKEN", "t")
    monkeypatch.setattr(ws, "WHATSAPP_PHONE_NUMBER_ID", "123")


def test_failed_send_logs_meta_error_body(monkeypatch, caplog):
    body = '{"error":{"message":"Business eligibility payment issue","code":131042}}'
    _patch_transport(monkeypatch, lambda req: httpx.Response(400, text=body))
    caplog.set_level(logging.WARNING)
    ws.log.propagate = True
    text_wamid, image_wamid = asyncio.run(ws.send_whatsapp_message("2348000000000", "hi"))
    assert (text_wamid, image_wamid) == (None, None)
    assert "131042" in caplog.text and "FAILED (400)" in caplog.text


def test_successful_send_still_returns_wamid(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json={"messages": [{"id": "wamid.X"}]}))
    assert asyncio.run(ws.send_whatsapp_message("2348000000000", "hi")) == ("wamid.X", None)


def test_webhook_failed_status_records_code(monkeypatch):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from chatbot.routers import webhook

    calls = []
    monkeypatch.setattr(webhook, "_update_message_delivery", lambda *a: calls.append(a))
    app = FastAPI()
    app.include_router(webhook.router)
    payload = {"entry": [{"changes": [{"value": {"statuses": [{
        "id": "wamid.Y", "status": "failed", "timestamp": "1700000000",
        "errors": [{"code": 131042, "title": "Business eligibility payment issue"}],
    }]}}]}]}
    r = TestClient(app).post("/webhook", json=payload)
    assert r.status_code == 200
    assert calls == [("wamid.Y", "failed", "(131042) Business eligibility payment issue", "1700000000")]
