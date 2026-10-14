import json
import os
import re
import time
from decimal import Decimal
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import func, select, text

from .db import Config, factory
from .models import Audit, Channel, Contact, Conversation, Event, Message, Note, Outbox, Product, Session, User, WebSession, Workspace, uid
from .security import digest, passwords, signature_ok, throttle, token, verify_password

SECRET_REF = re.compile(r"^ITURA_SECRET_[A-Z0-9_]{1,64}$")

class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

class Login(Input):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)

class ChannelInput(Input):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["whatsapp", "web"]
    external_id: str | None = Field(default=None, max_length=100)
    enabled: bool = False
    config: dict = Field(default_factory=dict)

class ProductInput(Input):
    name: str = Field(min_length=1, max_length=200)
    price: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    currency: str = Field(default="NGN", pattern=r"^[A-Z]{3}$")
    description: str = Field(default="", max_length=5000)
    active: bool = True

class UserInput(Input):
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "agent", "analyst"] = "agent"

class UserUpdate(Input):
    active: bool
    role: Literal["admin", "agent", "analyst"]

class Control(Input):
    version: int = Field(ge=1)
    mode: Literal["human", "ai"]
    state: Literal["open", "waiting", "closed"]
    owner_id: str | None = None
    attention: bool

class Body(Input):
    body: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")

class WebStart(Input):
    name: str = Field(default="", max_length=120)

class Settings(Input):
    version: int = Field(ge=1)
    automation_enabled: bool
    welcome: str = Field(max_length=2000)
    instruction: str = Field(max_length=20000)
    knowledge: str = Field(max_length=100000)
    model: str = Field(max_length=100)
    ai_secret_ref: str = Field(default="", max_length=90)

def serialize(row, fields):
    return {k: str(getattr(row, k)) if isinstance(getattr(row, k), Decimal) else getattr(row, k) for k in fields.split()}

def user_json(user):
    return serialize(user, "id workspace_id email name role active heartbeat_at away")

def conversation_json(db, row):
    result = serialize(row, "id contact_id channel_id owner_id state mode attention version updated_at last_inbound_at")
    contact = db.get(Contact, row.contact_id)
    channel = db.get(Channel, row.channel_id)
    result.update(name=contact.name, identity=contact.external_id, channel=channel.name, provider=channel.provider)
    return result

def enqueue(db, conversation, body, sender, dedup_key, automated=False, author_id=None, purpose="reply"):
    existing = db.query(Outbox).filter_by(dedup_key=dedup_key).first()
    if existing:
        return db.get(Message, existing.message_id)
    message = Message(conversation_id=conversation.id, direction="outbound", sender=sender, body=body, delivery="queued")
    db.add(message)
    db.flush()
    db.add(Outbox(dedup_key=dedup_key, message_id=message.id, automated=automated,
                  author_id=author_id, permission_version=conversation.version,
                  target_seq=conversation.inbound_seq if automated else None, purpose=purpose))
    conversation.updated_at = time.time()
    return message

def accept_message(db, channel, provider_id, external_id, name, body, kind="text", provider_time=None):
    if db.query(Event).filter_by(channel_id=channel.id, provider_id=provider_id).first():
        return None
    contact = db.query(Contact).filter_by(channel_id=channel.id, external_id=external_id).first()
    if not contact:
        contact = Contact(workspace_id=channel.workspace_id, channel_id=channel.id, external_id=external_id, name=name)
        db.add(contact); db.flush()
    conversation = db.query(Conversation).filter_by(contact_id=contact.id).with_for_update().first()
    if not conversation:
        workspace = db.get(Workspace, channel.workspace_id)
        conversation = Conversation(workspace_id=channel.workspace_id, channel_id=channel.id, contact_id=contact.id,
                                    mode="ai" if workspace.automation_enabled else "human")
        db.add(conversation); db.flush()
    conversation.updated_at = time.time()
    conversation.last_inbound_at = max(conversation.last_inbound_at or 0, min(float(provider_time or time.time()), time.time()))
    conversation.state = "open"
    conversation.attention = True
    conversation.inbound_seq = (conversation.inbound_seq or 0) + 1
    if body.strip().lower() in {"stop", "unsubscribe", "cancel messages"}:
        contact.opted_out = True
        conversation.mode = "human"
        conversation.version += 1
    db.add(Message(conversation_id=conversation.id, direction="inbound", sender=contact.name or external_id,
                   body=body, kind=kind, provider_id=provider_id))
    event = Event(channel_id=channel.id, provider_id=provider_id, conversation_id=conversation.id,
                  kind="message", payload={"body": body, "kind": kind, "conversation_version": conversation.version,
                                           "inbound_seq": conversation.inbound_seq})
    db.add(event)
    return conversation

def create_app(config: Config):
    engine, sessions = factory(config)
    app = FastAPI(title="ITURA Operations API", version="2.0.0")
    app.state.engine, app.state.sessions = engine, sessions

    @app.middleware("http")
    async def boundary(request, call_next):
        request_id = uid()
        try: length = int(request.headers.get("content-length", "0") or 0)
        except ValueError: return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        if length > config.max_body_bytes:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != config.origin:
                return JSONResponse({"detail": "Untrusted request origin"}, status_code=403)
        web_origin = None
        if request.url.path.startswith("/web/"):
            origin = request.headers.get("origin")
            with sessions() as db:
                allowed = any(origin in c.config.get("origins", []) for c in db.query(Channel).filter_by(provider="web", enabled=True).all())
            if allowed: web_origin = origin
            if request.method == "OPTIONS":
                if not allowed: return JSONResponse({"detail": "Widget origin not allowed"}, status_code=403)
                return Response(status_code=204, headers={"Access-Control-Allow-Origin": web_origin,
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Authorization, Content-Type", "Vary": "Origin"})
        response = await call_next(request)
        if web_origin:
            response.headers.update({"Access-Control-Allow-Origin": web_origin, "Vary": "Origin"})
        response.headers.update({"X-Request-ID": request_id, "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
        return response

    def db_session():
        with sessions() as db:
            yield db

    def current(request: Request, db=Depends(db_session)):
        raw = request.cookies.get("itura_session", "")
        record = db.get(Session, digest(raw)) if raw else None
        if not record or record.expires_at <= time.time():
            raise HTTPException(401, "Sign in required")
        user = db.get(User, record.user_id)
        if not user or not user.active:
            raise HTTPException(401, "Account is unavailable")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if digest(request.headers.get("x-csrf-token", "")) != record.csrf_hash:
                raise HTTPException(403, "Request verification failed")
        return user

    def admin(user=Depends(current)):
        if user.role != "admin":
            raise HTTPException(403, "Administrator access required")
        return user

    def audit(db, user, action, entity):
        db.add(Audit(workspace_id=user.workspace_id, actor_id=user.id, action=action, entity_id=entity))

    def scoped(db, model, entity_id, user, lock=False):
        query = db.query(model).filter_by(id=entity_id, workspace_id=user.workspace_id)
        if lock: query = query.with_for_update()
        row = query.first()
        if not row: raise HTTPException(404, "Record not found")
        return row

    def conversation_access(db, entity_id, user, write=False, lock=False):
        row = scoped(db, Conversation, entity_id, user, lock)
        if user.role == "agent" and row.owner_id not in {None, user.id}:
            raise HTTPException(403, "This conversation belongs to another agent")
        if write and user.role == "analyst":
            raise HTTPException(403, "Read-only access")
        return row

    @app.get("/health/live")
    def live(): return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        try:
            with sessions() as db:
                db.execute(text("SELECT 1"))
                db.query(Workspace).limit(1).all()
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.post("/api/auth/login")
    def login(body: Login, request: Request, response: Response, db=Depends(db_session)):
        throttle(db, "login:" + (request.client.host if request.client else "unknown"), 15, 900)
        user = db.query(User).filter_by(email=body.email.lower()).first()
        if not user or not user.active or not verify_password(user.password_hash, body.password):
            raise HTTPException(401, "Invalid email or password")
        raw, csrf = token(), token()
        db.add(Session(token_hash=digest(raw), user_id=user.id, csrf_hash=digest(csrf),
                       expires_at=time.time() + config.session_hours * 3600))
        audit(db, user, "login", user.id); db.commit()
        response.set_cookie("itura_session", raw, httponly=True, secure=config.secure_cookies,
                            samesite="strict", max_age=config.session_hours * 3600)
        response.set_cookie("itura_csrf", csrf, secure=config.secure_cookies, samesite="strict",
                            max_age=config.session_hours * 3600)
        return user_json(user)

    @app.get("/api/auth/me")
    def me(user=Depends(current)): return user_json(user)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user=Depends(current), db=Depends(db_session)):
        record = db.get(Session, digest(request.cookies.get("itura_session", "")))
        if record: db.delete(record)
        user = db.get(User, user.id); user.heartbeat_at = None
        audit(db, user, "logout", user.id); db.commit()
        response.delete_cookie("itura_session"); response.delete_cookie("itura_csrf")
        return {"ok": True}

    @app.post("/api/presence")
    def presence(away: bool = False, user=Depends(current), db=Depends(db_session)):
        row = db.get(User, user.id); row.heartbeat_at = time.time(); row.away = away; db.commit()
        return {"ok": True}

    @app.get("/api/team")
    def team(user=Depends(current), db=Depends(db_session)):
        return [user_json(r) for r in db.query(User).filter_by(workspace_id=user.workspace_id).all()]

    @app.post("/api/team", status_code=201)
    def add_user(body: UserInput, user=Depends(admin), db=Depends(db_session)):
        if "@" not in body.email or db.query(User).filter_by(email=body.email.lower()).first():
            raise HTTPException(409, "Email is unavailable")
        row = User(workspace_id=user.workspace_id, email=body.email.lower(), name=body.name,
                   password_hash=passwords.hash(body.password), role=body.role)
        db.add(row); db.flush(); audit(db, user, "user.create", row.id); db.commit()
        return user_json(row)

    @app.patch("/api/team/{entity_id}")
    def update_user(entity_id: str, body: UserUpdate, user=Depends(admin), db=Depends(db_session)):
        row = scoped(db, User, entity_id, user, True)
        if row.id == user.id: raise HTTPException(409, "Use another administrator to change your access")
        row.active, row.role = body.active, body.role
        db.query(Session).filter_by(user_id=row.id).delete()
        audit(db, user, "user.access", row.id); db.commit(); return user_json(row)

    @app.get("/api/conversations")
    def conversations(view: str = "all", q: str = "", offset: int = 0, limit: int = 30,
                      user=Depends(current), db=Depends(db_session)):
        query = db.query(Conversation).filter_by(workspace_id=user.workspace_id)
        if user.role == "agent": query = query.filter((Conversation.owner_id == user.id) | Conversation.owner_id.is_(None))
        if view == "mine": query = query.filter(Conversation.owner_id == user.id)
        elif view == "unassigned": query = query.filter(Conversation.owner_id.is_(None))
        elif view == "attention": query = query.filter(Conversation.attention.is_(True))
        elif view in {"open", "waiting", "closed"}: query = query.filter(Conversation.state == view)
        elif view != "all": raise HTTPException(400, "Unknown view")
        if q:
            query = query.join(Contact).filter((Contact.name.ilike("%" + q[:100] + "%")) | (Contact.external_id.ilike("%" + q[:100] + "%")))
        total = query.count(); rows = query.order_by(Conversation.updated_at.desc(), Conversation.id).offset(max(offset, 0)).limit(min(max(limit, 1), 100)).all()
        return {"total": total, "items": [conversation_json(db, r) for r in rows]}

    @app.get("/api/conversations/{entity_id}/messages")
    def messages(entity_id: str, user=Depends(current), db=Depends(db_session)):
        conversation_access(db, entity_id, user)
        rows = db.query(Message).filter_by(conversation_id=entity_id).order_by(Message.created_at.desc(), Message.id).limit(200).all()
        return [serialize(r, "id direction sender body kind delivery created_at") for r in reversed(rows)]

    @app.patch("/api/conversations/{entity_id}")
    def control(entity_id: str, body: Control, user=Depends(current), db=Depends(db_session)):
        row = conversation_access(db, entity_id, user, True, True)
        if row.version != body.version: raise HTTPException(409, "Conversation changed; refresh before retrying")
        if user.role != "admin" and body.owner_id not in {user.id, row.owner_id}:
            raise HTTPException(403, "An administrator must approve ownership transfer")
        if body.owner_id:
            owner = scoped(db, User, body.owner_id, user)
            if not owner.active or owner.role == "analyst": raise HTTPException(400, "Owner cannot receive conversations")
        row.owner_id, row.mode, row.state, row.attention = body.owner_id, body.mode, body.state, body.attention
        row.version += 1; audit(db, user, "conversation.control", row.id); db.commit()
        return conversation_json(db, row)

    @app.post("/api/conversations/{entity_id}/reply", status_code=202)
    def reply(entity_id: str, body: Body, user=Depends(current), db=Depends(db_session)):
        row = conversation_access(db, entity_id, user, True, True)
        if row.mode != "human": raise HTTPException(409, "Take over this conversation before replying")
        if not row.owner_id: row.owner_id = user.id; row.version += 1
        contact = db.get(Contact, row.contact_id)
        if contact.opted_out: raise HTTPException(409, "Contact opted out of messaging")
        message = enqueue(db, row, body.body, user.name, "reply:" + row.id + ":" + body.request_id, author_id=user.id)
        audit(db, user, "message.queue", message.id); db.commit()
        return serialize(message, "id delivery")

    @app.get("/api/conversations/{entity_id}/notes")
    def notes(entity_id: str, user=Depends(current), db=Depends(db_session)):
        conversation_access(db, entity_id, user)
        return [serialize(r, "id author_id body created_at") for r in db.query(Note).filter_by(conversation_id=entity_id).order_by(Note.created_at).all()]

    @app.post("/api/conversations/{entity_id}/notes", status_code=201)
    def add_note(entity_id: str, body: Body, user=Depends(current), db=Depends(db_session)):
        conversation_access(db, entity_id, user, True)
        row = Note(conversation_id=entity_id, author_id=user.id, body=body.body)
        db.add(row); db.flush(); audit(db, user, "note.create", row.id); db.commit()
        return serialize(row, "id author_id body created_at")

    @app.get("/api/contacts")
    def contacts(user=Depends(current), db=Depends(db_session)):
        query = db.query(Contact).filter_by(workspace_id=user.workspace_id)
        if user.role == "agent":
            query = query.join(Conversation).filter((Conversation.owner_id == user.id) | Conversation.owner_id.is_(None))
        return [serialize(r, "id name stage external_id channel_id opted_out attributes") for r in query.limit(200).all()]

    @app.get("/api/products")
    def products(user=Depends(current), db=Depends(db_session)):
        return [serialize(r, "id name price currency description active") for r in db.query(Product).filter_by(workspace_id=user.workspace_id).order_by(Product.name).all()]

    @app.post("/api/products", status_code=201)
    def product_create(body: ProductInput, user=Depends(admin), db=Depends(db_session)):
        row = Product(workspace_id=user.workspace_id, **body.model_dump()); db.add(row); db.flush()
        audit(db, user, "product.create", row.id); db.commit(); return serialize(row, "id name price currency description active")

    @app.patch("/api/products/{entity_id}")
    def product_update(entity_id: str, body: ProductInput, user=Depends(admin), db=Depends(db_session)):
        row = scoped(db, Product, entity_id, user)
        for key, value in body.model_dump().items(): setattr(row, key, value)
        audit(db, user, "product.update", row.id); db.commit(); return serialize(row, "id name price currency description active")

    @app.get("/api/channels")
    def channels(user=Depends(admin), db=Depends(db_session)):
        return [serialize(r, "id name provider external_id enabled config") for r in db.query(Channel).filter_by(workspace_id=user.workspace_id).all()]

    def validate_channel(body):
        if body.provider == "whatsapp":
            allowed = {"token_ref", "app_secret_ref", "verify_token_ref", "api_version"}
            if not body.external_id or not body.external_id.isdigit(): raise HTTPException(400, "A numeric phone-number ID is required")
            if set(body.config) - allowed: raise HTTPException(400, "Unsupported channel configuration")
            for key in {"token_ref", "app_secret_ref", "verify_token_ref"}:
                if not SECRET_REF.fullmatch(str(body.config.get(key, ""))): raise HTTPException(400, "Use a protected ITURA_SECRET_ environment reference")
            if not re.fullmatch(r"v\d+\.\d+", str(body.config.get("api_version", ""))): raise HTTPException(400, "Choose the approved Graph API version")
        else:
            if set(body.config) - {"origins"}: raise HTTPException(400, "Unsupported web configuration")
            origins = body.config.get("origins", [])
            if not isinstance(origins, list) or not origins or any(not isinstance(o, str) or not o.startswith("https://") and not (not config.secure_cookies and o.startswith("http://localhost")) for o in origins):
                raise HTTPException(400, "Provide exact HTTPS origins for the widget")

    @app.post("/api/channels", status_code=201)
    def channel_create(body: ChannelInput, user=Depends(admin), db=Depends(db_session)):
        validate_channel(body)
        row = Channel(workspace_id=user.workspace_id, **body.model_dump()); db.add(row); db.flush()
        audit(db, user, "channel.create", row.id); db.commit(); return serialize(row, "id name provider external_id enabled config")

    @app.patch("/api/channels/{entity_id}")
    def channel_update(entity_id: str, body: ChannelInput, user=Depends(admin), db=Depends(db_session)):
        validate_channel(body); row = scoped(db, Channel, entity_id, user)
        if body.provider != row.provider or body.external_id != row.external_id:
            raise HTTPException(409, "Create a separate connection for a different account")
        for key, value in body.model_dump().items(): setattr(row, key, value)
        audit(db, user, "channel.update", row.id); db.commit(); return serialize(row, "id name provider external_id enabled config")

    @app.get("/api/settings")
    def settings(user=Depends(admin), db=Depends(db_session)):
        row = db.get(Workspace, user.workspace_id)
        return {"name": row.name, "version": row.version, "automation_enabled": row.automation_enabled,
                "welcome": "", "instruction": "", "knowledge": "", "model": "", "ai_secret_ref": "", **row.settings}

    @app.put("/api/settings")
    def set_settings(body: Settings, user=Depends(admin), db=Depends(db_session)):
        row = db.query(Workspace).filter_by(id=user.workspace_id).with_for_update().one()
        if body.version != row.version: raise HTTPException(409, "Settings changed; reload before publishing")
        if body.ai_secret_ref and not SECRET_REF.fullmatch(body.ai_secret_ref): raise HTTPException(400, "Use a protected AI credential reference")
        if body.automation_enabled and (not body.instruction or not body.model or not os.environ.get(body.ai_secret_ref)):
            raise HTTPException(400, "Approved AI instructions, model and credentials are required")
        row.settings = body.model_dump(exclude={"version", "automation_enabled"})
        row.automation_enabled = body.automation_enabled; row.version += 1
        audit(db, user, "settings.publish", row.id); db.commit()
        return {"version": row.version}

    @app.get("/api/overview")
    def overview(user=Depends(current), db=Depends(db_session)):
        query = db.query(Conversation).filter_by(workspace_id=user.workspace_id)
        if user.role == "agent": query = query.filter((Conversation.owner_id == user.id) | Conversation.owner_id.is_(None))
        ids = select(Conversation.id).where(Conversation.workspace_id == user.workspace_id)
        if user.role == "agent": ids = ids.where((Conversation.owner_id == user.id) | Conversation.owner_id.is_(None))
        message_ids = select(Message.id).where(Message.conversation_id.in_(ids))
        return {"conversations": query.count(), "attention": query.filter_by(attention=True).count(),
                "unassigned": query.filter(Conversation.owner_id.is_(None)).count(), "open": query.filter_by(state="open").count(),
                "closed": query.filter_by(state="closed").count(),
                "delivery_unknown": db.query(Outbox).filter(Outbox.message_id.in_(message_ids), Outbox.state == "unknown").count(),
                "pending_sends": db.query(Outbox).filter(Outbox.message_id.in_(message_ids), Outbox.state == "pending").count()}

    @app.get("/api/audit")
    def audits(user=Depends(admin), db=Depends(db_session)):
        return [serialize(r, "id actor_id action entity_id created_at") for r in db.query(Audit).filter_by(workspace_id=user.workspace_id).order_by(Audit.created_at.desc()).limit(200).all()]

    @app.get("/webhooks/whatsapp/{channel_id}")
    def verify_webhook(channel_id: str, request: Request, db=Depends(db_session)):
        channel = db.get(Channel, channel_id)
        secret = os.environ.get(channel.config.get("verify_token_ref", ""), "") if channel else ""
        if channel and channel.provider == "whatsapp" and channel.enabled and secret and request.query_params.get("hub.mode") == "subscribe" and request.query_params.get("hub.verify_token") == secret:
            return Response(request.query_params.get("hub.challenge", ""), media_type="text/plain")
        raise HTTPException(403, "Verification failed")

    @app.post("/webhooks/whatsapp/{channel_id}")
    async def webhook(channel_id: str, request: Request, db=Depends(db_session)):
        channel = db.query(Channel).filter_by(id=channel_id).with_for_update().first()
        if not channel or channel.provider != "whatsapp" or not channel.enabled: raise HTTPException(404, "Channel unavailable")
        raw = await request.body()
        if len(raw) > config.max_body_bytes: raise HTTPException(413, "Request too large")
        secret = os.environ.get(channel.config.get("app_secret_ref", ""), "")
        if not signature_ok(raw, request.headers.get("x-hub-signature-256"), secret): raise HTTPException(403, "Invalid signature")
        try: payload = json.loads(raw)
        except (ValueError, TypeError): raise HTTPException(400, "Invalid event")
        if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account": raise HTTPException(400, "Invalid provider object")
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if str(value.get("metadata", {}).get("phone_number_id", "")) != channel.external_id:
                        raise HTTPException(403, "Wrong destination account")
                    for message in value.get("messages", []):
                        external_id = str(message["from"])
                        if not external_id.isdigit() or not message.get("id"): raise HTTPException(400, "Invalid message identity")
                        profile = next((c.get("profile", {}).get("name", "") for c in value.get("contacts", []) if c.get("wa_id") == external_id), "")
                        kind = message.get("type", "unsupported")
                        body = message.get("text", {}).get("body", "") if kind == "text" else "Customer sent " + kind + "; human review required."
                        accept_message(db, channel, str(message["id"]), external_id, profile[:120], body[:4000], kind, message.get("timestamp"))
                    for status in value.get("statuses", []):
                        status_key = "status:" + str(status.get("id", "")) + ":" + str(status.get("status", "")) + ":" + str(status.get("timestamp", ""))
                        if not db.query(Event).filter_by(channel_id=channel.id, provider_id=status_key).first():
                            db.add(Event(channel_id=channel.id, provider_id=status_key, kind="status", payload={"provider_id": status.get("id"), "status": status.get("status")}))
                        rank = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3}
                        for message in db.query(Message).join(Conversation).filter(Conversation.channel_id == channel.id, Message.provider_id == status.get("id")).all():
                            incoming = status.get("status")
                            if incoming == "failed" and message.delivery not in {"delivered", "read"}: message.delivery = "failed"
                            elif incoming in rank and rank[incoming] > rank.get(message.delivery, -1): message.delivery = incoming
            db.commit()
        except HTTPException: db.rollback(); raise
        except (KeyError, TypeError, ValueError): db.rollback(); raise HTTPException(400, "Malformed provider event")
        return {"accepted": True}

    def web_channel(db, channel_id, request):
        row = db.query(Channel).filter_by(id=channel_id, provider="web", enabled=True).first()
        if not row: raise HTTPException(404, "Widget unavailable")
        if request.headers.get("origin") not in row.config.get("origins", []): raise HTTPException(403, "Widget origin not allowed")
        return row

    def web_conversation(db, request):
        raw = request.headers.get("authorization", "")
        if not raw.startswith("Bearer "): raise HTTPException(401, "Web session required")
        session = db.get(WebSession, digest(raw[7:]))
        if not session or session.expires_at <= time.time(): raise HTTPException(401, "Web session expired")
        row = db.get(Conversation, session.conversation_id)
        web_channel(db, row.channel_id, request)
        return row

    @app.post("/web/{channel_id}/sessions", status_code=201)
    def start_web(channel_id: str, body: WebStart, request: Request, db=Depends(db_session)):
        channel = web_channel(db, channel_id, request)
        throttle(db, "web-start:" + channel.id + ":" + (request.client.host if request.client else "unknown"), 20, 3600)
        raw = token(); contact = Contact(workspace_id=channel.workspace_id, channel_id=channel.id, external_id=uid(), name=body.name)
        db.add(contact); db.flush()
        workspace = db.get(Workspace, channel.workspace_id)
        row = Conversation(workspace_id=channel.workspace_id, channel_id=channel.id, contact_id=contact.id,
                           mode="ai" if workspace.automation_enabled else "human")
        db.add(row); db.flush(); db.add(WebSession(token_hash=digest(raw), conversation_id=row.id, expires_at=time.time() + 86400))
        db.commit(); return {"token": raw, "conversation_id": row.id}

    @app.get("/web/messages")
    def web_messages(request: Request, db=Depends(db_session)):
        row = web_conversation(db, request)
        messages = db.query(Message).filter_by(conversation_id=row.id).order_by(Message.created_at.desc()).limit(200).all()
        return [serialize(m, "id direction sender body delivery created_at") for m in reversed(messages)]

    @app.post("/web/messages", status_code=202)
    def web_send(body: Body, request: Request, db=Depends(db_session)):
        row = web_conversation(db, request)
        throttle(db, "web-message:" + row.id, 60, 3600)
        channel = db.query(Channel).filter_by(id=row.channel_id).with_for_update().one()
        contact = db.get(Contact, row.contact_id)
        accept_message(db, channel, "web:" + row.id + ":" + body.request_id, contact.external_id, contact.name, body.body)
        db.commit(); return {"accepted": True}

    return app
