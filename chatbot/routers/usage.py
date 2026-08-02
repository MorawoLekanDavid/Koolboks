from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from chatbot.database import get_db
from chatbot.models import ApiUsageLog, Message
from chatbot.routers.permissions import require_tab_permission

router = APIRouter(prefix="/admin/usage", tags=["usage"])


@router.get("/groq")
async def groq_usage(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_tab_permission("usage")),
):
    def _fetch():
        db = get_db()
        try:
            q = db.query(ApiUsageLog)
            if date_from:
                q = q.filter(ApiUsageLog.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                q = q.filter(ApiUsageLog.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            rows = q.order_by(ApiUsageLog.created_at.desc()).all()

            if not rows:
                return {
                    "total_requests": 0, "total_prompt_tokens": 0, "total_completion_tokens": 0,
                    "total_tokens": 0, "total_cost_usd": 0.0, "by_purpose": [], "trend": [],
                }

            total_prompt = sum(r.prompt_tokens or 0 for r in rows)
            total_completion = sum(r.completion_tokens or 0 for r in rows)
            total_tokens = sum(r.total_tokens or 0 for r in rows)
            total_cost = sum(r.estimated_cost_usd or 0.0 for r in rows)

            purpose_map: dict = {}
            for r in rows:
                p = purpose_map.setdefault(r.purpose, {"requests": 0, "tokens": 0, "cost_usd": 0.0})
                p["requests"] += 1
                p["tokens"] += r.total_tokens or 0
                p["cost_usd"] += r.estimated_cost_usd or 0.0
            by_purpose = [
                {"purpose": p, "requests": v["requests"], "tokens": v["tokens"], "cost_usd": round(v["cost_usd"], 4)}
                for p, v in sorted(purpose_map.items(), key=lambda kv: -kv[1]["cost_usd"])
            ]

            trend_map: dict = {}
            for r in rows:
                day = r.created_at.date().isoformat()
                t = trend_map.setdefault(day, {"tokens": 0, "cost_usd": 0.0, "requests": 0})
                t["tokens"] += r.total_tokens or 0
                t["cost_usd"] += r.estimated_cost_usd or 0.0
                t["requests"] += 1
            trend = [
                {"date": d, "tokens": v["tokens"], "cost_usd": round(v["cost_usd"], 4), "requests": v["requests"]}
                for d, v in sorted(trend_map.items())
            ]

            return {
                "total_requests": len(rows),
                "total_prompt_tokens": total_prompt,
                "total_completion_tokens": total_completion,
                "total_tokens": total_tokens,
                "total_cost_usd": round(total_cost, 4),
                "by_purpose": by_purpose,
                "trend": trend,
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/whatsapp")
async def whatsapp_usage(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_tab_permission("usage")),
):
    """Message volume only — WhatsApp Cloud API doesn't return per-message cost,
    and per-conversation pricing depends on category + country rates this
    account hasn't supplied, so no cost estimate is shown."""
    def _fetch():
        db = get_db()
        try:
            q = db.query(Message)
            if date_from:
                q = q.filter(Message.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                q = q.filter(Message.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            rows = q.all()

            sent_bot = sum(1 for m in rows if m.direction == "outbound" and m.name == "KoolBot")
            sent_agent = sum(1 for m in rows if m.direction == "outbound" and m.name != "KoolBot")
            received = sum(1 for m in rows if m.direction == "inbound")

            trend_map: dict = {}
            for m in rows:
                if not m.created_at:
                    continue
                day = m.created_at.date().isoformat()
                t = trend_map.setdefault(day, {"sent": 0, "received": 0})
                if m.direction == "outbound":
                    t["sent"] += 1
                else:
                    t["received"] += 1
            trend = [{"date": d, **v} for d, v in sorted(trend_map.items())]

            return {
                "sent_by_bot": sent_bot,
                "sent_by_agent": sent_agent,
                "total_sent": sent_bot + sent_agent,
                "total_received": received,
                "trend": trend,
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)
