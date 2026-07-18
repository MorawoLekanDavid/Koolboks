from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from chatbot.config import DATABASE_URL, KNOWLEDGE_BASE, SYSTEM_PROMPT_TEMPLATE, log
from chatbot.models import init_db

db_engine = None
SessionLocal = None


def init_database():
    global db_engine, SessionLocal
    db_engine = init_db(DATABASE_URL)
    SessionLocal = sessionmaker(bind=db_engine)

    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS wamid VARCHAR(100)"))
            _c.commit()
    except Exception as _e:
        log.warning(f"wamid migration: {_e}")

    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS canned_responses (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(100),
                    content VARCHAR(2000),
                    created_by VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            _c.commit()
    except Exception as _e:
        log.warning(f"canned_responses migration: {_e}")

    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("ALTER TABLE products ADD COLUMN IF NOT EXISTS description VARCHAR"))
            _c.commit()
    except Exception as _e:
        log.warning(f"products.description migration: {_e}")

    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS whatsapp_phone VARCHAR(20)"))
            _c.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_leads_whatsapp_phone ON leads (whatsapp_phone)"))
            _c.execute(sa_text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'new'"))
            _c.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_leads_status ON leads (status)"))
            _c.execute(sa_text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS assigned_to VARCHAR(255)"))
            _c.commit()
    except Exception as _e:
        log.warning(f"leads columns migration: {_e}")

    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS handoff_events (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20),
                    agent_name VARCHAR(255),
                    event_type VARCHAR(20),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            _c.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_handoff_events_phone ON handoff_events (phone)"))
            _c.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_handoff_events_created_at ON handoff_events (created_at)"))
            _c.commit()
    except Exception as _e:
        log.warning(f"handoff_events migration: {_e}")

    # AI instructions + knowledge base tables, seeded from existing files on first run
    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS ai_instructions (
                    id SERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    status VARCHAR(20) DEFAULT 'live',
                    version INTEGER DEFAULT 1,
                    created_by VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            _c.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_ai_instructions_status ON ai_instructions (status)"))
            _c.commit()
            # Seed from system_prompt.txt if table is empty
            row = _c.execute(sa_text("SELECT COUNT(*) FROM ai_instructions")).scalar()
            if row == 0 and SYSTEM_PROMPT_TEMPLATE:
                _c.execute(
                    sa_text("INSERT INTO ai_instructions (content, status, version, created_by, created_at) VALUES (:c, 'live', 1, 'system', NOW())"),
                    {"c": SYSTEM_PROMPT_TEMPLATE},
                )
                _c.commit()
                log.info("Seeded ai_instructions from system_prompt.txt")
    except Exception as _e:
        log.warning(f"ai_instructions migration: {_e}")

    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS kb_documents (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255),
                    content TEXT NOT NULL,
                    file_type VARCHAR(20),
                    file_size INTEGER,
                    status VARCHAR(20) DEFAULT 'live',
                    created_by VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            _c.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_kb_documents_status ON kb_documents (status)"))
            _c.commit()
            # Seed from knowledge_base.txt if table is empty
            row = _c.execute(sa_text("SELECT COUNT(*) FROM kb_documents")).scalar()
            if row == 0 and KNOWLEDGE_BASE:
                _c.execute(
                    sa_text("INSERT INTO kb_documents (name, content, file_type, status, created_by, created_at) VALUES (:n, :c, 'txt', 'live', 'system', NOW())"),
                    {"n": "knowledge_base.txt", "c": KNOWLEDGE_BASE},
                )
                _c.commit()
                log.info("Seeded kb_documents from knowledge_base.txt")
    except Exception as _e:
        log.warning(f"kb_documents migration: {_e}")

    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS tags (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(50) NOT NULL UNIQUE,
                    color VARCHAR(20) NOT NULL DEFAULT '#6366f1',
                    created_by VARCHAR(100),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            _c.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS conversation_tags (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(30) NOT NULL,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    tagged_by VARCHAR(100),
                    created_at TIMESTAMP DEFAULT NOW(),
                    CONSTRAINT uq_conv_tag UNIQUE (phone, tag_id)
                )
            """))
            _c.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_conversation_tags_phone ON conversation_tags (phone)"))
            _c.commit()
            # Seed default tags if empty
            count = _c.execute(sa_text("SELECT COUNT(*) FROM tags")).scalar()
            if count == 0:
                defaults = [
                    ("Hot Lead", "#ef4444"),
                    ("Warm Lead", "#f97316"),
                    ("Follow-up Needed", "#eab308"),
                    ("Paid", "#22c55e"),
                    ("Not Interested", "#6b7280"),
                ]
                for name, color in defaults:
                    _c.execute(
                        sa_text("INSERT INTO tags (name, color, created_by) VALUES (:n, :c, 'system')"),
                        {"n": name, "c": color},
                    )
                _c.commit()
                log.info("Seeded default tags")
    except Exception as _e:
        log.warning(f"tags migration: {_e}")

    try:
        with db_engine.connect() as _c:
            _c.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS lead_assignment_rules (
                    id SERIAL PRIMARY KEY,
                    condition_field VARCHAR(50) NOT NULL,
                    condition_operator VARCHAR(20) NOT NULL,
                    condition_value VARCHAR(255),
                    assign_to VARCHAR(255) NOT NULL,
                    priority INTEGER DEFAULT 0,
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            _c.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_lar_priority ON lead_assignment_rules (priority)"))
            _c.commit()
    except Exception as _e:
        log.warning(f"lead_assignment_rules migration: {_e}")

    log.info("Database initialized successfully")


def get_db():
    """Get database session"""
    return SessionLocal()


def dispose():
    if db_engine:
        db_engine.dispose()
        log.info("Database disconnected")
