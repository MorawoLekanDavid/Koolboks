from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, func, select, text

from chatbot.database import get_db
from chatbot.dependencies import require_admin
from chatbot.models import HandoffEvent, Lead, Message
from chatbot.utils.phone import normalize_phone

router = APIRouter(prefix="/admin/analytics", tags=["analytics"])


@router.get("/conversations-handled")
async def conversations_handled(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_admin),
):
    def _fetch():
        db = get_db()
        try:
            filters = [
                Message.direction == "outbound",
                Message.name != "KoolBot",
                Message.name.isnot(None),
                Message.name != "",
            ]
            if date_from:
                filters.append(Message.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                filters.append(Message.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            rows = db.execute(
                select(
                    Message.name,
                    func.date(Message.created_at).label("date"),
                    func.count(func.distinct(Message.phone)).label("count"),
                )
                .where(and_(*filters))
                .group_by(Message.name, func.date(Message.created_at))
                .order_by(func.date(Message.created_at).desc())
            ).all()
            return [{"agent": r.name, "date": str(r.date), "conversations": r.count} for r in rows]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/agent-handoffs")
async def agent_handoffs(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_admin),
):
    def _fetch():
        db = get_db()
        try:
            q = db.query(HandoffEvent)
            if date_from:
                q = q.filter(HandoffEvent.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                q = q.filter(HandoffEvent.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            totals: dict = {}
            for ev in q.all():
                entry = totals.setdefault(ev.agent_name, {"takeovers": 0, "handbacks": 0})
                if ev.event_type == "takeover":
                    entry["takeovers"] += 1
                else:
                    entry["handbacks"] += 1
            return [
                {"agent": name, "takeovers": stats["takeovers"], "handbacks": stats["handbacks"]}
                for name, stats in sorted(totals.items(), key=lambda x: x[1]["takeovers"], reverse=True)
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/product-recommendations")
async def product_recommendations(ctx: dict = Depends(require_admin)):
    def _fetch():
        db = get_db()
        try:
            rows = db.execute(
                select(Lead.product_interest, func.count(Lead.id).label("count"))
                .where(Lead.product_interest != None, Lead.product_interest != "")
                .group_by(Lead.product_interest)
                .order_by(func.count(Lead.id).desc())
                .limit(10)
            ).all()
            total = sum(r.count for r in rows)
            return [
                {"product": r.product_interest, "count": r.count,
                 "pct": round(r.count / total * 100) if total else 0}
                for r in rows
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/broadcast-overview")
async def broadcast_overview(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_admin),
):
    def _fetch():
        db = get_db()
        try:
            where_clauses = []
            params: dict = {}
            if date_from:
                where_clauses.append("br.created_at >= :date_from")
                params["date_from"] = date_from
            if date_to:
                where_clauses.append("br.created_at <= :date_to")
                params["date_to"] = date_to + "T23:59:59"
            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            row = db.execute(text(f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(CASE WHEN delivery_status IN ('delivered','read') THEN 1 END) AS delivered,
                    COUNT(CASE WHEN delivery_status = 'read' THEN 1 END) AS read_count,
                    COUNT(CASE WHEN responded = true THEN 1 END) AS responded,
                    COUNT(CASE WHEN delivery_status = 'failed' THEN 1 END) AS failed
                FROM broadcast_recipients br
                {where_sql}
            """), params).first()
            total = row.total or 0
            delivered = row.delivered or 0
            read_c = row.read_count or 0
            responded = row.responded or 0
            failed = row.failed or 0
            return {
                "total_sent": total,
                "delivered": delivered,
                "read": read_c,
                "responded": responded,
                "failed": failed,
                "pending": max(0, total - delivered - failed),
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/broadcast-campaigns")
async def broadcast_campaigns_list(ctx: dict = Depends(require_admin)):
    def _fetch():
        db = get_db()
        try:
            rows = db.execute(text("""
                SELECT
                    bc.id, bc.job_id, bc.template_name, bc.language, bc.created_by,
                    bc.status, bc.total, bc.created_at, bc.finished_at,
                    COUNT(br.id) AS recipients,
                    COUNT(CASE WHEN br.delivery_status IN ('delivered','read') THEN 1 END) AS delivered,
                    COUNT(CASE WHEN br.delivery_status = 'read' THEN 1 END) AS read_count,
                    COUNT(CASE WHEN br.responded = true THEN 1 END) AS responded,
                    COUNT(CASE WHEN br.delivery_status = 'failed' THEN 1 END) AS failed
                FROM broadcast_campaigns bc
                LEFT JOIN broadcast_recipients br ON br.campaign_id = bc.id
                GROUP BY bc.id
                ORDER BY bc.created_at DESC
                LIMIT 100
            """)).all()
            return [
                {
                    "id": r.id,
                    "template_name": r.template_name,
                    "language": r.language,
                    "created_by": r.created_by,
                    "status": r.status,
                    "total": r.total,
                    "sent": r.recipients,
                    "delivered": r.delivered,
                    "read": r.read_count,
                    "responded": r.responded,
                    "failed": r.failed,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/broadcast-by-template")
async def broadcast_by_template(ctx: dict = Depends(require_admin)):
    def _fetch():
        db = get_db()
        try:
            rows = db.execute(text("""
                SELECT
                    bc.template_name,
                    COUNT(DISTINCT bc.id) AS campaigns,
                    COUNT(br.id) AS total_sent,
                    COUNT(CASE WHEN br.delivery_status IN ('delivered','read') THEN 1 END) AS delivered,
                    COUNT(CASE WHEN br.delivery_status = 'read' THEN 1 END) AS read_count,
                    COUNT(CASE WHEN br.responded = true THEN 1 END) AS responded,
                    COUNT(CASE WHEN br.delivery_status = 'failed' THEN 1 END) AS failed
                FROM broadcast_campaigns bc
                LEFT JOIN broadcast_recipients br ON br.campaign_id = bc.id
                GROUP BY bc.template_name
                ORDER BY COUNT(br.id) DESC
            """)).all()
            return [
                {
                    "template": r.template_name,
                    "campaigns": r.campaigns,
                    "total_sent": r.total_sent,
                    "delivered": r.delivered,
                    "read": r.read_count,
                    "responded": r.responded,
                    "failed": r.failed,
                    "response_rate": round(r.responded / r.total_sent * 100) if r.total_sent else 0,
                    "delivery_rate": round(r.delivered / r.total_sent * 100) if r.total_sent else 0,
                }
                for r in rows
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/lead-funnel")
async def lead_funnel(ctx: dict = Depends(require_admin)):
    def _fetch():
        db = get_db()
        try:
            msg_phones = {
                r.phone for r in db.execute(
                    select(Message.phone).where(Message.direction == "inbound").distinct()
                ).all()
            }
            lead_phones_norm = set()
            for r in db.query(Lead.phone, Lead.whatsapp_phone).all():
                if r.phone:
                    lead_phones_norm.add(normalize_phone(r.phone))
                if r.whatsapp_phone:
                    lead_phones_norm.add(normalize_phone(r.whatsapp_phone))
            total_leads = db.query(Lead).filter(Lead.phone != None, Lead.phone != "").count()
            drop_off = sum(1 for p in msg_phones if normalize_phone(p) not in lead_phones_norm)
            total_convs = drop_off + total_leads
            return {
                "funnel": [
                    {"stage": "Conversations Started", "count": total_convs},
                    {"stage": "Phone Captured", "count": total_leads},
                    {"stage": "Drop-off (no phone given)", "count": drop_off},
                ]
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)
