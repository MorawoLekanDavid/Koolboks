import asyncio
import time
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
import pytest

from platform_v2.worker import generate, send

@pytest.mark.parametrize("code,body,expected", [(200,{"messages":[{"id":"receipt"}]},"accepted"),(200,{},"unknown"),(429,{},"retry"),(400,{},"failed"),(503,{},"unknown")])
def test_provider_acceptance_contract(monkeypatch,code,body,expected):
    monkeypatch.setenv("ITURA_SECRET_TEST", "synthetic-token")
    channel=SimpleNamespace(provider="whatsapp",external_id="123",config={"api_version":"v99.0","token_ref":"ITURA_SECRET_TEST"})
    contact=SimpleNamespace(external_id="2348000000000")
    message=SimpleNamespace(body="Synthetic message")
    response=httpx.Response(code,json=body,request=httpx.Request("POST","https://example.test"))
    with patch("platform_v2.worker.httpx.AsyncClient") as factory:
        client=factory.return_value.__aenter__.return_value
        client.post=AsyncMock(return_value=response)
        result=asyncio.run(send(channel,contact,message))
        assert result.state==expected

def test_provider_timeout_is_unknown_not_retry(monkeypatch):
    monkeypatch.setenv("ITURA_SECRET_TEST", "synthetic-token")
    channel=SimpleNamespace(provider="whatsapp",external_id="123",config={"api_version":"v99.0","token_ref":"ITURA_SECRET_TEST"})
    with patch("platform_v2.worker.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value.post=AsyncMock(side_effect=httpx.ReadTimeout("synthetic"))
        result=asyncio.run(send(channel,SimpleNamespace(external_id="123"),SimpleNamespace(body="hello")))
        assert result.state=="unknown"

def ai_response(value):
    import json
    return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(value)}}]},request=httpx.Request("POST","https://example.test"))

@pytest.mark.parametrize("question", ["Can I see a picture?", "an i see the piture?", "Send photos", "200L", "Do you have a 300 litre freezer?"])
def test_unsupported_media_and_unlisted_capacity_handoff_without_provider(question):
    with patch("platform_v2.worker.httpx.AsyncClient") as factory:
        result = asyncio.run(generate(SimpleNamespace(settings={}), None,
            [{"role": "user", "content": question}], []))
        assert result["escalate"] is True
        assert "teammate" in result["reply"]
        factory.assert_not_called()

def test_general_question_still_uses_provider(monkeypatch):
    monkeypatch.setenv("ITURA_SECRET_AI", "synthetic-token")
    workspace = SimpleNamespace(settings={"model": "test", "ai_secret_ref": "ITURA_SECRET_AI"})
    with patch("platform_v2.worker.httpx.AsyncClient") as factory:
        post = AsyncMock(return_value=ai_response({"reply": "Solar panels provide electricity.", "escalate": False, "product_ids": []}))
        factory.return_value.__aenter__.return_value.post = post
        result = asyncio.run(generate(workspace, None, [{"role": "user", "content": "How do solar freezers work?"}], []))
        assert result["escalate"] is False
        post.assert_awaited_once()

def test_hallucinated_product_id_is_rejected(monkeypatch):
    monkeypatch.setenv("ITURA_SECRET_AI", "synthetic-token")
    workspace=SimpleNamespace(settings={"model":"test","instruction":"test","ai_secret_ref":"ITURA_SECRET_AI"})
    with patch("platform_v2.worker.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value.post=AsyncMock(return_value=ai_response({"reply":"Here is a product", "escalate":False,"product_ids":["made-up"]}))
        with pytest.raises(ValueError,match="ungrounded_product"):
            asyncio.run(generate(workspace,None,[],[]))

def test_payment_details_are_replaced_and_escalated(monkeypatch):
    monkeypatch.setenv("ITURA_SECRET_AI", "synthetic-token")
    workspace=SimpleNamespace(settings={"model":"test","instruction":"test","ai_secret_ref":"ITURA_SECRET_AI"})
    with patch("platform_v2.worker.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value.post=AsyncMock(return_value=ai_response({"reply":"Bank account 1234567890", "escalate":False,"product_ids":[]}))
        result=asyncio.run(generate(workspace,None,[],[]))
        assert result["escalate"] is True
        assert "1234567890" not in result["reply"]

def test_whatsapp_photo_upload_then_send_uses_media_id(monkeypatch):
    monkeypatch.setenv("ITURA_SECRET_TEST", "synthetic-token")
    channel = SimpleNamespace(provider="whatsapp", external_id="123", config={"api_version": "v99.0", "token_ref": "ITURA_SECRET_TEST"})
    message = SimpleNamespace(body="Approved caption", kind="image", media={"url": "https://images.example/a.png"})
    def reply(body): return httpx.Response(200, json=body, request=httpx.Request("POST", "https://example.test"))
    with patch("platform_v2.worker.fetch_image", return_value=(b"synthetic", "image/png")), patch("platform_v2.worker.httpx.AsyncClient") as factory:
        post = AsyncMock(side_effect=[reply({"id": "uploaded-media"}), reply({"messages": [{"id": "image-receipt"}]})])
        factory.return_value.__aenter__.return_value.post = post
        result = asyncio.run(send(channel, SimpleNamespace(external_id="2348000000000"), message))
        assert result.state == "accepted" and result.provider_id == "image-receipt"
        assert post.call_args_list[0].args[0].endswith("/media")
        assert post.call_args_list[1].kwargs["json"]["image"] == {"id": "uploaded-media", "caption": "Approved caption"}

def test_unavailable_photo_never_reaches_provider(monkeypatch):
    from platform_v2.media import ImageError
    monkeypatch.setenv("ITURA_SECRET_TEST", "synthetic-token")
    channel = SimpleNamespace(provider="whatsapp", external_id="123", config={"api_version": "v99.0", "token_ref": "ITURA_SECRET_TEST"})
    with patch("platform_v2.worker.fetch_image", side_effect=ImageError("blocked")), patch("platform_v2.worker.httpx.AsyncClient") as factory:
        post = AsyncMock(); factory.return_value.__aenter__.return_value.post = post
        result = asyncio.run(send(channel, SimpleNamespace(external_id="123"), SimpleNamespace(kind="image", body="caption", media={"url": "https://internal.example/a"})))
        assert result.state == "failed"
        post.assert_not_called()
