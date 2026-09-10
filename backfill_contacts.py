#!/usr/bin/env python3
"""
One-time backfill: before send-template and bulk-broadcast started writing
to `leads`, an agent starting a conversation with a brand-new number (single
send from the conversation tray, or a recipient in a bulk broadcast) only
created a ConversationOwner claim and a Message row — never a Lead — so
those numbers never showed up as contacts.

A phone qualifies for backfill when its very first message ever is an
outbound "[Template: ...]" send (i.e. an agent initiated it, nobody
messaged in first) and it still has no Lead row today. Nothing here is
specific to any one phone number, agent, or count — it's derived entirely
from existing message/lead data at run time.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from chatbot.models import Lead

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://koolbuy:koolbuy_secure_password_2026@localhost:5432/koolbuy")

FIRST_MESSAGE_SQL = text("""
    SELECT fm.phone, fm.name, fm.created_at,
           EXISTS(SELECT 1 FROM messages m WHERE m.phone = fm.phone AND m.direction = 'inbound') AS responded
    FROM (
        SELECT DISTINCT ON (phone) phone, direction, content, name, created_at
        FROM messages
        ORDER BY phone, created_at ASC
    ) fm
    LEFT JOIN leads l ON l.phone = fm.phone
    WHERE fm.direction = 'outbound'
      AND fm.content LIKE '[Template:%'
      AND l.id IS NULL
    ORDER BY fm.created_at
""")


def run():
    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        rows = session.execute(FIRST_MESSAGE_SQL).fetchall()
        created = 0
        for phone, agent_name, created_at, responded in rows:
            session.add(Lead(
                phone=phone,
                source="manual",
                status="new",
                created_by=agent_name or "Agent",
                assigned_to=agent_name or None,  # the agent who started it owns it
                outreach_stage="responded" if responded else "contacted",
                created_at=created_at,  # preserve the real start date, not "now"
            ))
            created += 1
            print(f"  [contact] {phone} | started by {agent_name or 'Agent'} | {created_at} | {'responded' if responded else 'contacted'}")

        session.commit()
        print(f"\nDone. {created} contact(s) backfilled into leads.")
    except Exception as e:
        session.rollback()
        print(f"Backfill failed: {e}")
        raise
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    run()
