from sqlalchemy import Column, String, Float, DateTime, Integer, Boolean, ForeignKey, UniqueConstraint, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()


class Product(Base):
    """Product inventory model"""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), index=True)
    price = Column(Float)
    image_url = Column(String(512), nullable=True)
    product_url = Column(String(512), nullable=True)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow)


class Lead(Base):
    """Lead/customer data model"""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=True)
    phone = Column(String(20), unique=True, index=True)
    whatsapp_phone = Column(String(20), nullable=True, index=True)
    business = Column(String(255), nullable=True)
    product_interest = Column(String(255), nullable=True)
    amount = Column(String(100), nullable=True)
    payment_plan = Column(String(255), nullable=True)
    pain_point = Column(String(512), nullable=True)
    power_type = Column(String(50), nullable=True)
    address = Column(String(512), nullable=True)
    active_duration = Column(String(50), nullable=True)
    status = Column(String(50), default="new", index=True)  # new, interested, follow_up, drop_off, converted
    assigned_to = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow)


class Message(Base):
    """WhatsApp conversation message"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), index=True)
    phone = Column(String(20), index=True)
    name = Column(String(255), nullable=True)
    direction = Column(String(10))  # inbound / outbound
    content = Column(String(4000))
    wamid = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Agent(Base):
    """Registered agent for admin dashboard login"""
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255))
    email = Column(String(255), unique=True, index=True)
    password_hash = Column(String(255), nullable=True)
    role = Column(String(20), default="agent")  # "agent" | "super_admin"
    created_at = Column(DateTime, default=datetime.utcnow)


class CannedResponse(Base):
    """Pre-written messages agents can send with one click"""
    __tablename__ = "canned_responses"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100))
    content = Column(String(2000))
    created_by = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)


class LeadNote(Base):
    """Notes added by agents/admins on a lead"""
    __tablename__ = "lead_notes"

    id = Column(Integer, primary_key=True, index=True)
    lead_phone = Column(String(25), index=True)
    content = Column(String(2000))
    created_by = Column(String(255))   # agent name or email
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class HandoffEvent(Base):
    """Persistent log of every agent takeover and handback"""
    __tablename__ = "handoff_events"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(20), index=True)
    agent_name = Column(String(255))
    event_type = Column(String(20))   # "takeover" | "handback"
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AIInstruction(Base):
    """Versioned system prompt / AI instruction set"""
    __tablename__ = "ai_instructions"

    id = Column(Integer, primary_key=True, index=True)
    content = Column(String, nullable=False)
    status = Column(String(20), default="draft", index=True)  # live | draft | archived
    version = Column(Integer, default=1)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class KBDocument(Base):
    """Knowledge base document — each file upload is a separate document"""
    __tablename__ = "kb_documents"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255))               # original filename
    content = Column(String, nullable=False) # extracted plain text
    file_type = Column(String(20))           # txt | md | pdf | docx | xlsx
    file_size = Column(Integer, nullable=True)
    status = Column(String(20), default="draft", index=True)  # live | draft | pending_trash | trashed
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Tag(Base):
    """Label that can be applied to conversations"""
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), nullable=False, unique=True)
    color = Column(String(20), nullable=False, default="#6366f1")
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation_tags = relationship("ConversationTag", back_populates="tag", cascade="all, delete-orphan")


class ConversationTag(Base):
    """Junction: a tag applied to a specific conversation (phone)"""
    __tablename__ = "conversation_tags"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(30), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)
    tagged_by = Column(String(100), nullable=True)  # agent name or "AI ✨"
    created_at = Column(DateTime, default=datetime.utcnow)

    tag = relationship("Tag", back_populates="conversation_tags")

    __table_args__ = (UniqueConstraint("phone", "tag_id", name="uq_conv_tag"),)


def init_db(database_url: str):
    """Initialize database tables"""
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(bind=engine)
    return engine
