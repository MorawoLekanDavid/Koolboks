import time
import uuid

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base

Base = declarative_base()

def uid():
    return str(uuid.uuid4())

class Workspace(Base):
    __tablename__ = "v2_workspaces"
    id = Column(String(36), primary_key=True, default=uid)
    name = Column(String(120), nullable=False)
    automation_enabled = Column(Boolean, nullable=False, default=False)
    settings = Column(JSON, nullable=False, default=dict)
    version = Column(Integer, nullable=False, default=1)

class User(Base):
    __tablename__ = "v2_users"
    id = Column(String(36), primary_key=True, default=uid)
    workspace_id = Column(String(36), ForeignKey("v2_workspaces.id"), nullable=False, index=True)
    email = Column(String(254), unique=True, nullable=False)
    name = Column(String(120), nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(20), nullable=False, default="agent")
    active = Column(Boolean, nullable=False, default=True)
    heartbeat_at = Column(Float, nullable=True)
    away = Column(Boolean, nullable=False, default=False)

class Session(Base):
    __tablename__ = "v2_sessions"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(String(36), ForeignKey("v2_users.id"), nullable=False, index=True)
    csrf_hash = Column(String(64), nullable=False)
    expires_at = Column(Float, nullable=False)

class Channel(Base):
    __tablename__ = "v2_channels"
    id = Column(String(36), primary_key=True, default=uid)
    workspace_id = Column(String(36), ForeignKey("v2_workspaces.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    provider = Column(String(20), nullable=False)
    external_id = Column(String(100), nullable=True)
    enabled = Column(Boolean, nullable=False, default=False)
    config = Column(JSON, nullable=False, default=dict)
    __table_args__ = (UniqueConstraint("workspace_id", "provider", "external_id"),)

class Contact(Base):
    __tablename__ = "v2_contacts"
    id = Column(String(36), primary_key=True, default=uid)
    workspace_id = Column(String(36), ForeignKey("v2_workspaces.id"), nullable=False, index=True)
    channel_id = Column(String(36), ForeignKey("v2_channels.id"), nullable=False)
    external_id = Column(String(120), nullable=False)
    name = Column(String(120), nullable=False, default="")
    stage = Column(String(30), nullable=False, default="new")
    attributes = Column(JSON, nullable=False, default=dict)
    opted_out = Column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint("channel_id", "external_id"),)

class Conversation(Base):
    __tablename__ = "v2_conversations"
    id = Column(String(36), primary_key=True, default=uid)
    workspace_id = Column(String(36), ForeignKey("v2_workspaces.id"), nullable=False, index=True)
    channel_id = Column(String(36), ForeignKey("v2_channels.id"), nullable=False, index=True)
    contact_id = Column(String(36), ForeignKey("v2_contacts.id"), nullable=False, unique=True)
    owner_id = Column(String(36), ForeignKey("v2_users.id"), nullable=True)
    state = Column(String(20), nullable=False, default="open")
    mode = Column(String(20), nullable=False, default="human")
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(Float, nullable=False, default=time.time, index=True)
    last_inbound_at = Column(Float, nullable=True)
    inbound_seq = Column(Integer, nullable=False, default=0)
    attention = Column(Boolean, nullable=False, default=True)

class Message(Base):
    __tablename__ = "v2_messages"
    id = Column(String(36), primary_key=True, default=uid)
    conversation_id = Column(String(36), ForeignKey("v2_conversations.id"), nullable=False, index=True)
    direction = Column(String(10), nullable=False)
    sender = Column(String(120), nullable=False)
    body = Column(Text, nullable=False)
    kind = Column(String(20), nullable=False, default="text")
    provider_id = Column(String(150), nullable=True, index=True)
    delivery = Column(String(20), nullable=False, default="received")
    created_at = Column(Float, nullable=False, default=time.time)

class Event(Base):
    __tablename__ = "v2_events"
    id = Column(String(36), primary_key=True, default=uid)
    channel_id = Column(String(36), ForeignKey("v2_channels.id"), nullable=False)
    provider_id = Column(String(180), nullable=False)
    conversation_id = Column(String(36), ForeignKey("v2_conversations.id"), nullable=True)
    kind = Column(String(20), nullable=False)
    payload = Column(JSON, nullable=False)
    state = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    lease_token = Column(String(36), nullable=True)
    lease_until = Column(Float, nullable=True)
    available_at = Column(Float, nullable=False, default=time.time)
    __table_args__ = (UniqueConstraint("channel_id", "provider_id"),)

class Outbox(Base):
    __tablename__ = "v2_outbox"
    id = Column(String(36), primary_key=True, default=uid)
    dedup_key = Column(String(180), unique=True, nullable=False)
    message_id = Column(String(36), ForeignKey("v2_messages.id"), nullable=False, unique=True)
    state = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    lease_token = Column(String(36), nullable=True)
    lease_until = Column(Float, nullable=True)
    available_at = Column(Float, nullable=False, default=time.time)
    error = Column(String(100), nullable=True)
    automated = Column(Boolean, nullable=False, default=False)
    author_id = Column(String(36), ForeignKey("v2_users.id"), nullable=True)
    permission_version = Column(Integer, nullable=False)
    target_seq = Column(Integer, nullable=True)
    purpose = Column(String(20), nullable=False, default="reply")

class WebSession(Base):
    __tablename__ = "v2_web_sessions"
    token_hash = Column(String(64), primary_key=True)
    conversation_id = Column(String(36), ForeignKey("v2_conversations.id"), nullable=False)
    expires_at = Column(Float, nullable=False)

class Product(Base):
    __tablename__ = "v2_products"
    id = Column(String(36), primary_key=True, default=uid)
    workspace_id = Column(String(36), ForeignKey("v2_workspaces.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    price = Column(Numeric(18, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="NGN")
    description = Column(Text, nullable=False, default="")
    active = Column(Boolean, nullable=False, default=True)

class Note(Base):
    __tablename__ = "v2_notes"
    id = Column(String(36), primary_key=True, default=uid)
    conversation_id = Column(String(36), ForeignKey("v2_conversations.id"), nullable=False, index=True)
    author_id = Column(String(36), ForeignKey("v2_users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(Float, nullable=False, default=time.time)

class Audit(Base):
    __tablename__ = "v2_audit"
    id = Column(String(36), primary_key=True, default=uid)
    workspace_id = Column(String(36), nullable=False, index=True)
    actor_id = Column(String(36), nullable=False)
    action = Column(String(80), nullable=False)
    entity_id = Column(String(100), nullable=False)
    created_at = Column(Float, nullable=False, default=time.time)

class RateLimit(Base):
    __tablename__ = "v2_rate_limits"
    key = Column(String(64), primary_key=True)
    count = Column(Integer, nullable=False)
    expires_at = Column(Float, nullable=False)
