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
    source = Column(String(20), default="bot", index=True)  # bot | manual | import
    outreach_stage = Column(String(50), default="not_contacted")  # contacts-only: name of a ContactStage row
    assigned_to = Column(String(255), nullable=True)  # contact "owner" — agent responsible for outreach
    created_by = Column(String(255), nullable=True)
    updated_by = Column(String(255), nullable=True)
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
    role = Column(String(50), default="agent")  # agent | customer_success_agent | telesales_agent | sales_agent | admin | super_admin
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


class LeadAssignmentRule(Base):
    """Rule for automatically assigning leads to agents"""
    __tablename__ = "lead_assignment_rules"

    id = Column(Integer, primary_key=True, index=True)
    condition_field = Column(String(50), nullable=False)    # product_interest | business | status | any
    condition_operator = Column(String(20), nullable=False) # contains | equals | is_empty | is_not_empty | any
    condition_value = Column(String(255), nullable=True)
    assign_to = Column(String(255), nullable=False)         # agent name
    priority = Column(Integer, default=0, index=True)       # lower = evaluated first
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationOwner(Base):
    """Tracks which agent initiated / owns a conversation (e.g. via outreach)"""
    __tablename__ = "conversation_owners"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(30), unique=True, index=True)
    owner_name = Column(String(255), nullable=True)
    owner_email = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BroadcastCampaign(Base):
    __tablename__ = "broadcast_campaigns"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(String(64), unique=True, index=True)
    template_name = Column(String(255))
    language = Column(String(20))
    variables = Column(String(1000), nullable=True)  # JSON-encoded list
    total = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    status = Column(String(20), default="running")  # running | done
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)

    recipients = relationship("BroadcastRecipient", back_populates="campaign", cascade="all, delete-orphan")


class BroadcastRecipient(Base):
    __tablename__ = "broadcast_recipients"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    phone = Column(String(30), index=True)
    wamid = Column(String(100), nullable=True, index=True)
    delivery_status = Column(String(20), default="sent")  # sent | delivered | read | failed
    responded = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    campaign = relationship("BroadcastCampaign", back_populates="recipients")


class RolePermission(Base):
    """Configurable per-role tab visibility — admin can toggle from the Team tab"""
    __tablename__ = "role_permissions"

    id = Column(Integer, primary_key=True, index=True)
    role = Column(String(50), nullable=False, index=True)
    tab = Column(String(50), nullable=False)
    allowed = Column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("role", "tab", name="uq_role_tab"),)


class ContactStage(Base):
    """Customizable outreach pipeline stage for the Contacts tab"""
    __tablename__ = "contact_stages"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    color = Column(String(20), default="#6366f1")
    sort_order = Column(Integer, default=0, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationScore(Base):
    """AI-judged response-quality score for a chunk of conversation, computed once
    it goes idle. Separate from Lead scoring — this rates how well the bot/agent
    handled the customer, not how qualified the customer is."""
    __tablename__ = "conversation_scores"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(30), index=True, nullable=False)
    quality_score = Column(Integer, nullable=False)  # 1-10
    likely_lost_customer = Column(Boolean, default=False, index=True)
    responder_type = Column(String(20), nullable=True)  # bot | agent | mixed
    issues = Column(String(500), nullable=True)  # comma-separated tags from a fixed vocabulary
    reasoning = Column(String(500), nullable=True)
    message_count = Column(Integer, default=0)
    scored_through = Column(DateTime, nullable=False, index=True)  # last message timestamp included in this window
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ApiUsageLog(Base):
    """Per-call token usage for every Groq request, so cost can be tracked and
    broken down by what the call was for."""
    __tablename__ = "api_usage_log"

    id = Column(Integer, primary_key=True, index=True)
    purpose = Column(String(50), nullable=False, index=True)  # chat_reply | lead_extraction | conversation_scoring | follow_up | auto_tag | test_chat
    model = Column(String(100), nullable=False)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    estimated_cost_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


def init_db(database_url: str):
    """Initialize database tables"""
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(bind=engine)
    return engine
