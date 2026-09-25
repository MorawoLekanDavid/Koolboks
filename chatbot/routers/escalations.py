from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from chatbot.database import get_db
from chatbot.models import Agent, Escalation
from chatbot.routers.permissions import require_tab_permission
from chatbot.services.escalation_service import CATEGORIES, PRIORITIES

router = APIRouter(prefix="/admin/escalations", tags=["escalations"])


def _fmt(e: Escalation) -> dict:
    return {
        "id": e.id,
        "phone": e.phone,
        "customer_name": e.customer_name,
        "category": e.category,
        "priority": e.priority,
        "trigger": e.trigger,
        "status": e.status,
        "summary": e.summary,
        "assigned_agent_id": e.assigned_agent_id,
        "assigned_agent_name": e.assigned_agent.name if e.assigned_agent else None,
        "notified_at": e.notified_at.isoformat() if e.notified_at else None,
        "notification_status": e.notification_status,
        "notification_error": e.notification_error,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "assigned_at": e.assigned_at.isoformat() if e.assigned_at else None,
        "first_response_at": e.first_response_at.isoformat() if e.first_response_at else None,
        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
        "resolution": e.resolution,
        "reopen_count": e.reopen_count,
    }


@router.get("")
async def list_escalations(
    status: Optional[str] = Query(None, description="Comma-separated: open,assigned,in_progress,resolved,reopened"),
    priority: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    ctx: dict = Depends(require_tab_permission("escalations")),
):
    """Backs the "Needs Attention" queue. Defaults to everything still
    outstanding (open/assigned/in_progress/reopened) -- pass status=resolved
    explicitly to see closed ones, e.g. for a history view."""
    def _fetch():
        db = get_db()
        try:
            q = db.query(Escalation)
            statuses = [s.strip() for s in status.split(",")] if status else ["open", "assigned", "in_progress", "reopened"]
            q = q.filter(Escalation.status.in_(statuses))
            if priority:
                q = q.filter(Escalation.priority == priority)
            if category:
                q = q.filter(Escalation.category == category)
            rows = q.order_by(Escalation.priority.desc(), Escalation.created_at.asc()).all()
            return [_fmt(r) for r in rows]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


class EscalationUpdate(BaseModel):
    status: Optional[str] = None  # assigned | in_progress | resolved | reopened
    assigned_agent_id: Optional[int] = None
    resolution: Optional[str] = None


@router.patch("/{escalation_id}")
async def update_escalation(
    escalation_id: int, body: EscalationUpdate,
    ctx: dict = Depends(require_tab_permission("escalations")),
):
    def _update():
        db = get_db()
        try:
            e = db.query(Escalation).filter(Escalation.id == escalation_id).first()
            if not e:
                raise HTTPException(404, "Escalation not found")

            if body.assigned_agent_id is not None:
                agent = db.query(Agent).filter(Agent.id == body.assigned_agent_id).first()
                if not agent:
                    raise HTTPException(400, "Agent not found")
                e.assigned_agent_id = body.assigned_agent_id
                if not e.assigned_at:
                    e.assigned_at = datetime.utcnow()
                if e.status == "open":
                    e.status = "assigned"

            if body.status:
                if body.status not in ("open", "assigned", "in_progress", "resolved", "reopened"):
                    raise HTTPException(400, "Invalid status")
                if body.status == "resolved":
                    e.resolved_at = datetime.utcnow()
                    if body.resolution:
                        e.resolution = body.resolution
                elif body.status == "reopened":
                    e.reopen_count = (e.reopen_count or 0) + 1
                    e.resolved_at = None
                e.status = body.status
            elif body.resolution:
                e.resolution = body.resolution

            db.commit()
            db.refresh(e)
            return _fmt(e)
        finally:
            db.close()
    return await run_in_threadpool(_update)
