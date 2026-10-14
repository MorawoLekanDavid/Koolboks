import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from platform_v2.api import create_app, enqueue
from platform_v2.db import Config
from platform_v2.models import Base, Channel, Contact, Conversation, Event, Message, Outbox, Session, User, Workspace
from platform_v2.security import passwords
from platform_v2.worker import ProviderResult, Worker

ORIGIN = "http://localhost:5173"

@pytest.fixture
def system(tmp_path, monkeypatch):
    app = create_app(Config("sqlite:///" + str(tmp_path / "test.db"), ORIGIN, False))
    Base.metadata.create_all(app.state.engine)
    sessions = app.state.sessions
    monkeypatch.setenv("ITURA_SECRET_APP", "synthetic-app-secret")
    monkeypatch.setenv("ITURA_SECRET_VERIFY", "synthetic-verify-secret")
    with sessions() as db:
        workspace = Workspace(name="Test workspace"); other = Workspace(name="Other workspace")
        db.add_all([workspace, other]); db.flush()
        users = [User(workspace_id=workspace.id, name=role, email=role+"@test.example", password_hash=passwords.hash("test-password-12345"), role=role) for role in ["admin", "agent", "analyst"]]
        db.add_all(users); db.flush()
        channel = Channel(workspace_id=workspace.id, name="WhatsApp", provider="whatsapp", external_id="12345", enabled=True,
                          config={"app_secret_ref": "ITURA_SECRET_APP", "verify_token_ref": "ITURA_SECRET_VERIFY", "token_ref": "ITURA_SECRET_TOKEN", "api_version": "v99.0"})
        web = Channel(workspace_id=workspace.id, name="Web", provider="web", enabled=True, config={"origins": [ORIGIN]})
        db.add_all([channel, web]); db.commit()
        ids = {"workspace": workspace.id, "other": other.id, "channel": channel.id, "web": web.id, **{u.role:u.id for u in users}}
    client = TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN})
    return app, sessions, client, ids

def login(client, role="admin"):
    r = client.post("/api/auth/login", json={"email": role+"@test.example", "password": "test-password-12345"})
    assert r.status_code == 200
    client.headers["x-csrf-token"] = client.cookies.get("itura_csrf")
    return r

def payload(body="Hello", message_id="wamid.test", channel="12345", kind="text"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": channel}, "contacts": [{"wa_id": "2348000000000", "profile": {"name": "Synthetic customer"}}],
        "messages": [{"id": message_id, "from": "2348000000000", "timestamp": str(int(time.time())), "type": kind, "text": {"body": body}}]
    }}]}]}

def webhook(client, channel_id, data):
    raw = json.dumps(data).encode()
    signature = "sha256=" + hmac.new(b"synthetic-app-secret", raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp/" + channel_id, content=raw, headers={"x-hub-signature-256": signature, "content-type": "application/json"})

def inbound(system, **kwargs):
    app, sessions, client, ids = system
    assert webhook(client, ids["channel"], payload(**kwargs)).status_code == 200
    with sessions() as db: return db.query(Conversation).first().id

def takeover(system, cid, role="admin"):
    app, sessions, client, ids = system
    login(client, role)
    with sessions() as db: version = db.get(Conversation, cid).version
    r = client.patch("/api/conversations/" + cid, json={"version": version, "owner_id": ids[role], "mode": "human", "state": "open", "attention": False})
    assert r.status_code == 200
    return r.json()

def enable_ai(system, cid):
    app, sessions, client, ids = system
    with sessions() as db:
        row = db.get(Conversation, cid); row.mode = "ai"
        db.get(Workspace, ids["workspace"]).automation_enabled = True
        db.commit()

async def accepted(*_): return ProviderResult("accepted", "wamid.outbound")
async def reply(*_): return {"reply": "Synthetic grounded response", "escalate": False}

def test_cookie_session_csrf_logout_revokes(system):
    app, sessions, client, ids = system
    assert client.get("/api/auth/me").status_code == 401
    response = login(client)
    assert "HttpOnly" in response.headers.get("set-cookie")
    assert "token" not in response.json()
    csrf = client.headers.pop("x-csrf-token")
    assert client.post("/api/presence").status_code == 403
    client.headers["x-csrf-token"] = csrf
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    with sessions() as db: assert db.query(Session).count() == 0

def test_wrong_origin_rejected(system):
    app, sessions, client, ids = system
    assert client.post("/api/auth/login", json={"email":"admin@test.example", "password":"test-password-12345"}, headers={"origin":"https://attacker.example"}).status_code == 403

def test_disable_user_revokes_existing_session(system):
    app, sessions, client, ids = system
    login(client, "agent")
    with sessions() as db: db.get(User, ids["agent"]).active=False; db.commit()
    assert client.get("/api/conversations").status_code == 401

def test_role_change_applies_without_cached_privilege(system):
    app, sessions, client, ids = system
    login(client)
    with sessions() as db: db.get(User, ids["admin"]).role="analyst"; db.commit()
    assert client.get("/api/settings").status_code == 403

def test_invalid_signature_creates_no_data(system):
    app, sessions, client, ids = system
    r=client.post("/webhooks/whatsapp/"+ids["channel"],json=payload(),headers={"x-hub-signature-256":"sha256=bad"})
    assert r.status_code==403
    with sessions() as db: assert db.query(Event).count()==0

def test_wrong_account_rejected(system):
    app,sessions,client,ids=system
    assert webhook(client,ids["channel"],payload(channel="666")).status_code==403

def test_replay_dedup_is_durable(system):
    app,sessions,client,ids=system
    inbound(system)
    assert webhook(client,ids["channel"],payload()).status_code==200
    # A separate application/process with the same database still deduplicates.
    second=create_app(Config(str(app.state.engine.url),ORIGIN,False))
    client2=TestClient(second,base_url=ORIGIN)
    assert webhook(client2,ids["channel"],payload()).status_code==200
    with sessions() as db:
        assert db.query(Event).count()==1
        assert db.query(Message).count()==1

def test_same_phone_on_different_accounts_is_isolated(system):
    app,sessions,client,ids=system
    inbound(system)
    with sessions() as db:
        channel=Channel(workspace_id=ids["workspace"],name="Second",provider="whatsapp",external_id="54321",enabled=True,config={"app_secret_ref":"ITURA_SECRET_APP"})
        db.add(channel);db.commit();cid=channel.id
    assert webhook(client,cid,payload(channel="54321")).status_code==200
    with sessions() as db: assert db.query(Conversation).count()==2; assert db.query(Contact).count()==2

def test_agent_scope_and_analyst_write_denial(system):
    app,sessions,client,ids=system
    cid=inbound(system);takeover(system,cid)
    login(client,"agent")
    assert client.get("/api/conversations").json()["total"]==0
    assert client.get("/api/conversations/"+cid+"/messages").status_code==403
    login(client,"analyst")
    assert client.get("/api/conversations/"+cid+"/messages").status_code==200
    assert client.post("/api/conversations/"+cid+"/reply",json={"body":"test", "request_id":"request123"}).status_code==403

def test_control_optimistic_conflict(system):
    app,sessions,client,ids=system
    cid=inbound(system); row=takeover(system,cid)
    body={"version":row["version"]-1,"mode":"ai","state":"open","attention":False,"owner_id":ids["admin"]}
    assert client.patch("/api/conversations/"+cid,json=body).status_code==409

def test_idempotent_reply_and_worker_restart(system):
    app,sessions,client,ids=system
    cid=inbound(system);takeover(system,cid)
    body={"body":"Thanks", "request_id":"request123"}
    a=client.post("/api/conversations/"+cid+"/reply",json=body)
    b=client.post("/api/conversations/"+cid+"/reply",json=body)
    assert a.status_code==202 and a.json()==b.json()
    worker=Worker(sessions,sender=accepted)
    asyncio.run(worker.send_once())
    with sessions() as db:
        assert db.query(Outbox).count()==1
        assert db.query(Outbox).one().state=="accepted"

def test_unknown_acceptance_never_automatically_resends(system):
    app,sessions,client,ids=system
    cid=inbound(system);takeover(system,cid)
    client.post("/api/conversations/"+cid+"/reply",json={"body":"Thanks", "request_id":"request123"})
    calls=[]
    async def timeout(*_): calls.append(1);return ProviderResult("unknown",error="timeout")
    worker=Worker(sessions,sender=timeout)
    asyncio.run(worker.send_once());worker.recover();asyncio.run(worker.send_once())
    assert len(calls)==1
    with sessions() as db: assert db.query(Outbox).one().state=="unknown"

def test_crash_during_send_quarantined(system):
    app,sessions,client,ids=system
    cid=inbound(system);takeover(system,cid)
    client.post("/api/conversations/"+cid+"/reply",json={"body":"Thanks", "request_id":"request123"})
    with sessions() as db:
        row=db.query(Outbox).one();row.state="sending";row.lease_until=time.time()-1;db.commit()
    Worker(sessions).recover()
    with sessions() as db: assert db.query(Outbox).one().state=="unknown"

def test_ai_job_recovery_after_process_restart(system):
    app,sessions,client,ids=system
    cid=inbound(system);enable_ai(system,cid)
    with sessions() as db:
        event=db.query(Event).one();event.state="processing";event.lease_until=time.time()-1;db.commit()
    worker=Worker(sessions,generator=reply);worker.recover();asyncio.run(worker.ai_once())
    with sessions() as db: assert db.query(Event).one().state=="done";assert db.query(Outbox).count()==1

def test_takeover_during_generation_discards_ai(system):
    app,sessions,client,ids=system
    cid=inbound(system);enable_ai(system,cid)
    async def switching(*_):
        with sessions() as db:
            c=db.get(Conversation,cid);c.mode="human";c.version+=1;db.commit()
        return {"reply":"stale", "escalate":False}
    asyncio.run(Worker(sessions,generator=switching).ai_once())
    with sessions() as db: assert db.query(Outbox).count()==0

def test_new_message_during_generation_discards_stale_reply(system):
    app,sessions,client,ids=system
    cid=inbound(system);enable_ai(system,cid)
    async def newer(*_):
        assert webhook(client,ids["channel"],payload(body="Actually another question",message_id="wamid.newer")).status_code==200
        return {"reply":"stale", "escalate":False}
    asyncio.run(Worker(sessions,generator=newer).ai_once())
    with sessions() as db: assert db.query(Outbox).count()==0

def test_global_pause_prevents_generated_send(system):
    app,sessions,client,ids=system
    cid=inbound(system);enable_ai(system,cid)
    worker=Worker(sessions,generator=reply,sender=accepted);asyncio.run(worker.ai_once())
    with sessions() as db: db.get(Workspace,ids["workspace"]).automation_enabled=False;db.commit()
    asyncio.run(worker.send_once())
    with sessions() as db: assert db.query(Outbox).one().state=="pending"

def test_optout_blocks_queued_send(system):
    app,sessions,client,ids=system
    cid=inbound(system);takeover(system,cid)
    client.post("/api/conversations/"+cid+"/reply",json={"body":"Thanks", "request_id":"request123"})
    webhook(client,ids["channel"],payload(body="STOP",message_id="wamid.stop"))
    asyncio.run(Worker(sessions,sender=accepted).send_once())
    with sessions() as db: assert db.query(Outbox).one().state=="cancelled"

def test_web_sessions_do_not_accept_chosen_conversation_ids(system):
    app,sessions,client,ids=system
    start=client.post("/web/"+ids["web"]+"/sessions",json={"name":"Web visitor"})
    assert start.status_code==201
    assert client.get("/web/messages").status_code==401
    token=start.json()["token"]
    client.headers["authorization"]="Bearer "+token
    assert client.post("/web/messages",json={"body":"Hello", "request_id":"web-request-1"}).status_code==202
    assert client.get("/web/messages").json()[0]["body"]=="Hello"
    other=client.post("/web/"+ids["web"]+"/sessions",json={"name":"Other visitor"}).json()
    client.headers["authorization"]="Bearer "+other["token"]
    assert client.get("/web/messages").json()==[]

def test_web_agent_reply_reaches_widget(system):
    app,sessions,client,ids=system
    start=client.post("/web/"+ids["web"]+"/sessions",json={"name":"Web visitor"}).json()
    cid=start["conversation_id"];takeover(system,cid)
    client.post("/api/conversations/"+cid+"/reply",json={"body":"An agent reply", "request_id":"request123"})
    asyncio.run(Worker(sessions).send_once())
    client.headers["authorization"]="Bearer "+start["token"]
    assert client.get("/web/messages").json()[0]["delivery"]=="delivered"

def test_cross_workspace_record_access_is_denied(system):
    app,sessions,client,ids=system
    login(client)
    with sessions() as db:
        channel=Channel(workspace_id=ids["other"],name="Other",provider="web",enabled=True,config={"origins":[ORIGIN]})
        db.add(channel);db.commit();entity_id=channel.id
    assert client.patch("/api/channels/"+entity_id,json={"name":"changed", "provider":"web", "enabled":True,"config":{"origins":[ORIGIN]}}).status_code==404

def test_prices_are_decimal_and_negative_rejected(system):
    app,sessions,client,ids=system;login(client)
    assert client.post("/api/products",json={"name":"Product", "price":"-1.00"}).status_code==422
    r=client.post("/api/products",json={"name":"Product", "price":"12345.67"})
    assert r.status_code==201 and r.json()["price"]=="12345.67"

def test_status_callbacks_are_monotonic(system):
    app,sessions,client,ids=system
    cid=inbound(system);takeover(system,cid)
    client.post("/api/conversations/"+cid+"/reply",json={"body":"Thanks", "request_id":"request123"})
    asyncio.run(Worker(sessions,sender=accepted).send_once())
    for state in ["read", "delivered", "failed"]:
        data={"object":"whatsapp_business_account", "entry":[{"changes":[{"value":{"metadata":{"phone_number_id":"12345"},"statuses":[{"id":"wamid.outbound", "status":state}]}}]}]}
        assert webhook(client,ids["channel"],data).status_code==200
    with sessions() as db: assert db.query(Message).filter_by(direction="outbound").one().delivery=="read"

def test_missing_schema_fails_readiness(tmp_path):
    app=create_app(Config("sqlite:///"+str(tmp_path/"missing.db"),ORIGIN,False))
    client=TestClient(app)
    assert client.get("/health/live").status_code==200
    assert client.get("/health/ready").status_code==503
