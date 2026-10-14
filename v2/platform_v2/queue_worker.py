"""Bounded Vercel queue subscriber around the existing database worker."""
import time
from datetime import timedelta
from sqlalchemy import func, or_
from vercel.queue import send, subscribe
from .db import Config, factory
from .models import Event, Outbox, Message, Conversation, Workspace
from .worker import Worker

TOPIC = "itura-jobs"

def next_due(sessions):
    with sessions() as db:
        event_due = db.query(func.min(Event.available_at)).filter(Event.state == "pending").scalar()
        lease_due = db.query(func.min(Event.lease_until)).filter(Event.state == "processing").scalar()
        send_lease = db.query(func.min(Outbox.lease_until)).filter(Outbox.state == "sending").scalar()
        outbox_due = db.query(func.min(Outbox.available_at)).join(Message, Outbox.message_id == Message.id).join(Conversation, Message.conversation_id == Conversation.id).join(Workspace, Conversation.workspace_id == Workspace.id).filter(Outbox.state == "pending", or_(Outbox.automated == False, Workspace.automation_enabled == True)).scalar()
        values = [v for v in (event_due, lease_due, send_lease, outbox_due) if v is not None]
        return min(values) if values else None

async def wake_if_needed(sessions, minimum_delay=0):
    due = next_due(sessions)
    if due is not None:
        await send(TOPIC, {"action": "drain"}, delay=timedelta(seconds=max(minimum_delay, min(900, int(due - time.time()) + 1))), retention=timedelta(hours=24))

@subscribe(topic=TOPIC, retry_after=60, max_concurrency=2, max_attempts=20)
async def drain(message):
    if message != {"action": "drain"}:
        raise ValueError("Invalid job wakeup")
    engine, sessions = factory(Config.from_env())
    try:
        worker = Worker(sessions)
        worker.recover()
        for _ in range(4):
            worked = await worker.ai_once()
            worked = await worker.send_once() or worked
            if not worked:
                break
        await wake_if_needed(sessions, minimum_delay=30)
    finally:
        engine.dispose()
