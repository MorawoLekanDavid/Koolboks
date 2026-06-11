from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from chatbot.config import DATABASE_URL, log
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

    log.info("Database initialized successfully")


def get_db():
    """Get database session"""
    return SessionLocal()


def dispose():
    if db_engine:
        db_engine.dispose()
        log.info("Database disconnected")
