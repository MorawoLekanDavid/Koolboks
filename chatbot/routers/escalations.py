from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from chatbot.database import get_db
from chatbot.models import Agent, Escalation
from chatbot.routers.permissions import require_tab_permission
from chatbot.services.escalation_service import escalation_to_dict, retry_notification

router = APIRouter(prefix="/admin/escalations", tags=["escalations"])


@router.get("")
async def list_escalations(
    status: Optional[str] = Query(None, description="Comma-separated: open,assigned,in_progress,resolved,reopened"),
    priority: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    assigned_agent_id: Optional[int] = Query(None),
    unassigned: Optional[bool] = Query(None, description="true = only escalations with no assigned agent"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD, inclusive, filters on created_at"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD, inclusive, filters on created_at"),
    ctx: dict = Depends(require_tab_permission("escalations")),
):
    """Backs the "Needs Attention" queue. Defaults to everything still
    outstanding (open/assigned/in_progress/reopened) -- pass status=resolved
    explicitly to see closed ones, e.g. for a history view. The agent/date
    filters exist so a long-running queue stays searchable instead of
    devolving into endless scrolling to find an older ticket."""
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
            if unassigned:
                q = q.filter(Escalation.assigned_agent_id.is_(None))
            elif assigned_agent_id:
                q = q.filter(Escalation.assigned_agent_id == assigned_agent_id)
            if date_from:
                try:
                    q = q.filter(Escalation.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
                except ValueError:
                    pass
            if date_to:
                try:
                    q = q.filter(Escalation.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
                except ValueError:
                    pass
            rows = q.order_by(Escalation.priority.desc(), Escalation.created_at.asc()).all()
            return [escalation_to_dict(r) for r in rows]
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
            return escalation_to_dict(e)
        finally:
            db.close()
    return await run_in_threadpool(_update)


@router.post("/{escalation_id}/notify")
async def resend_escalation_notification(
    escalation_id: int,
    ctx: dict = Depends(require_tab_permission("escalations")),
):
    """Re-attempts the WhatsApp alert for an escalation whose notification
    was previously skipped or failed -- e.g. once an admin has fixed the
    routing fallback agent or given the assigned agent a phone number."""
    result = await retry_notification(escalation_id)
    if result is None:
        raise HTTPException(404, "Escalation not found")
    return result
