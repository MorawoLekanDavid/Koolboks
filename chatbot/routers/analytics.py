from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, case, func, select, text

from chatbot.database import get_db
from chatbot.routers.permissions import require_tab_permission
from chatbot.models import Agent, ConversationScore, Department, HandoffEvent, Lead, Message
from chatbot.utils.phone import normalize_phone

router = APIRouter(prefix="/admin/analytics", tags=["analytics"])


@router.get("/conversations-handled")
async def conversations_handled(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_tab_permission("analytics")),
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
    ctx: dict = Depends(require_tab_permission("analytics")),
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
async def product_recommendations(ctx: dict = Depends(require_tab_permission("analytics"))):
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
    ctx: dict = Depends(require_tab_permission("analytics")),
):
    """Aggregate funnel across every template send — bulk broadcasts and
    one-off sends from a single conversation alike. Sourced from `messages`,
    same as /broadcast-by-template, so the two sections never disagree.

    Delivered/read/failed can only be known for messages with a captured
    wamid (delivery-status tracking was added retroactively — sends from
    before that have no wamid and no way to ever learn their real status).
    Counting an untracked message as "not delivered" is wrong — it can make
    delivered look far lower than responded, which is impossible if it's
    actually measuring real failures. So those rates are computed only over
    the trackable subset, and untracked count is surfaced separately instead
    of silently folded into "not delivered"."""
    def _fetch():
        db = get_db()
        try:
            where_clauses = ["m.direction = 'outbound'", "m.content LIKE '[Template:%'"]
            params: dict = {}
            if date_from:
                where_clauses.append("m.created_at >= :date_from")
                params["date_from"] = date_from
            if date_to:
                where_clauses.append("m.created_at <= :date_to")
                params["date_to"] = date_to + "T23:59:59"
            where_sql = "WHERE " + " AND ".join(where_clauses)
            row = db.execute(text(f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(m.wamid) AS trackable,
                    COUNT(CASE WHEN m.wamid IS NOT NULL AND m.delivery_status IN ('delivered','read') THEN 1 END) AS delivered,
                    COUNT(CASE WHEN m.wamid IS NOT NULL AND m.delivery_status = 'read' THEN 1 END) AS read_count,
                    COUNT(CASE WHEN EXISTS (
                        SELECT 1 FROM messages mi
                        WHERE mi.phone = m.phone AND mi.direction = 'inbound' AND mi.created_at > m.created_at
                    ) THEN 1 END) AS responded,
                    COUNT(CASE WHEN m.wamid IS NOT NULL AND m.delivery_status = 'failed' THEN 1 END) AS failed
                FROM messages m
                {where_sql}
            """), params).first()
            total = row.total or 0
            trackable = row.trackable or 0
            delivered = row.delivered or 0
            read_c = row.read_count or 0
            responded = row.responded or 0
            failed = row.failed or 0
            return {
                "total_sent": total,
                "trackable": trackable,
                "untracked": max(0, total - trackable),
                "delivered": delivered,
                "read": read_c,
                "responded": responded,
                "failed": failed,
                "pending": max(0, trackable - delivered - failed),
                "delivery_rate": round(delivered / trackable * 100) if trackable else None,
                "response_rate": round(responded / total * 100) if total else 0,
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/broadcast-campaigns")
async def broadcast_campaigns_list(ctx: dict = Depends(require_tab_permission("analytics"))):
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
async def broadcast_by_template(ctx: dict = Depends(require_tab_permission("analytics"))):
    """Response stats grouped by template name, across every send path — bulk
    broadcasts and one-off sends from a single conversation alike. Sourced from
    `messages` (not broadcast_recipients) so a template sent directly from a
    conversation shows up here too, not just campaigns.

    Delivery/read/failed rates are computed only over messages with a wamid
    (delivery tracking is only possible for those) — see broadcast_overview
    for why: folding untracked sends into "not delivered" produces a
    delivery rate that can look lower than the response rate, which is
    nonsensical when it's meant to represent actual failures."""
    def _fetch():
        db = get_db()
        try:
            rows = db.execute(text(r"""
                SELECT
                    substring(m.content from '\[Template: ([^\]]+)\]') AS template_name,
                    COUNT(*) AS total_sent,
                    COUNT(m.wamid) AS trackable,
                    COUNT(CASE WHEN m.wamid IS NOT NULL AND m.delivery_status IN ('delivered','read') THEN 1 END) AS delivered,
                    COUNT(CASE WHEN m.wamid IS NOT NULL AND m.delivery_status = 'read' THEN 1 END) AS read_count,
                    COUNT(CASE WHEN m.wamid IS NOT NULL AND m.delivery_status = 'failed' THEN 1 END) AS failed,
                    COUNT(CASE WHEN EXISTS (
                        SELECT 1 FROM messages mi
                        WHERE mi.phone = m.phone AND mi.direction = 'inbound' AND mi.created_at > m.created_at
                    ) THEN 1 END) AS responded
                FROM messages m
                WHERE m.direction = 'outbound' AND m.content LIKE '[Template:%'
                GROUP BY template_name
                ORDER BY COUNT(*) DESC
            """)).all()
            return [
                {
                    "template": r.template_name,
                    "total_sent": r.total_sent,
                    "trackable": r.trackable,
                    "delivered": r.delivered,
                    "read": r.read_count,
                    "responded": r.responded,
                    "failed": r.failed,
                    "response_rate": round(r.responded / r.total_sent * 100) if r.total_sent else 0,
                    "delivery_rate": round(r.delivered / r.trackable * 100) if r.trackable else None,
                }
                for r in rows
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/lead-funnel")
async def lead_funnel(ctx: dict = Depends(require_tab_permission("analytics"))):
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


@router.get("/conversation-quality")
async def conversation_quality(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_tab_permission("analytics")),
):
    def _fetch():
        db = get_db()
        try:
            # Filter by scored_through (when the underlying conversation actually
            # happened), not created_at (when the scoring job ran) — otherwise a
            # backfill run today makes every date filter show "today" regardless
            # of how old the real conversation is.
            q = db.query(ConversationScore)
            if date_from:
                q = q.filter(ConversationScore.scored_through >= datetime.fromisoformat(date_from))
            if date_to:
                q = q.filter(ConversationScore.scored_through <= datetime.fromisoformat(date_to + "T23:59:59"))
            rows = q.order_by(ConversationScore.scored_through.desc()).all()

            if not rows:
                return {"avg_score": None, "total_scored": 0, "lost_count": 0,
                        "issue_counts": {}, "flagged": [], "trend": [],
                        "auto_resolution_rate": None, "handoff_rate": None,
                        "trend_bot": [], "trend_human": []}

            avg_score = sum(r.quality_score for r in rows) / len(rows)
            lost_count = sum(1 for r in rows if r.likely_lost_customer)

            bot_count = sum(1 for r in rows if r.responder_type == "bot")
            handoff_count = sum(1 for r in rows if r.responder_type in ("agent", "mixed"))
            auto_resolution_rate = round(bot_count / len(rows) * 100, 1)
            handoff_rate = round(handoff_count / len(rows) * 100, 1)

            issue_counts: dict = {}
            for r in rows:
                for tag in (r.issues or "").split(","):
                    tag = tag.strip()
                    if tag:
                        issue_counts[tag] = issue_counts.get(tag, 0) + 1

            trend_map: dict = {}
            # "mixed" folds into "human" — an agent owned the outcome, which is
            # the more actionable signal for coaching than a strict bot/not-bot split.
            bot_trend_map: dict = {}
            human_trend_map: dict = {}
            for r in rows:
                day = r.scored_through.date().isoformat()
                trend_map.setdefault(day, []).append(r.quality_score)
                bucket = bot_trend_map if r.responder_type == "bot" else human_trend_map
                bucket.setdefault(day, []).append(r.quality_score)
            trend = [
                {"date": d, "avg_score": round(sum(v) / len(v), 2), "count": len(v)}
                for d, v in sorted(trend_map.items())
            ]
            trend_bot = [
                {"date": d, "avg_score": round(sum(v) / len(v), 2), "count": len(v)}
                for d, v in sorted(bot_trend_map.items())
            ]
            trend_human = [
                {"date": d, "avg_score": round(sum(v) / len(v), 2), "count": len(v)}
                for d, v in sorted(human_trend_map.items())
            ]

            flagged_rows = [r for r in rows if r.likely_lost_customer][:50]
            flagged_phones = [r.phone for r in flagged_rows]
            name_map: dict = {}
            if flagged_phones:
                name_agg = func.max(case((Message.direction == "inbound", Message.name), else_=None))
                name_rows = db.execute(
                    select(Message.phone, name_agg.label("name"))
                    .where(Message.phone.in_(flagged_phones))
                    .group_by(Message.phone)
                ).all()
                name_map = {nr.phone: nr.name for nr in name_rows}

            flagged = [
                {
                    "phone": r.phone,
                    "name": name_map.get(r.phone),
                    "quality_score": r.quality_score,
                    "reasoning": r.reasoning,
                    "issues": [t for t in (r.issues or "").split(",") if t],
                    "responder_type": r.responder_type,
                    "scored_through": r.scored_through.isoformat() if r.scored_through else None,
                }
                for r in flagged_rows
            ]

            return {
                "avg_score": round(avg_score, 2),
                "total_scored": len(rows),
                "lost_count": lost_count,
                "issue_counts": issue_counts,
                "flagged": flagged,
                "trend": trend,
                "auto_resolution_rate": auto_resolution_rate,
                "handoff_rate": handoff_rate,
                "trend_bot": trend_bot,
                "trend_human": trend_human,
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/traffic-telemetry")
async def traffic_telemetry(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_tab_permission("analytics")),
):
    def _fetch():
        db = get_db()
        try:
            q = db.query(Message)
            if date_from:
                q = q.filter(Message.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                q = q.filter(Message.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            rows = q.all()

            if not rows:
                return {
                    "total_messages": 0, "inbound_count": 0, "outbound_count": 0,
                    "hourly": [], "webhook_success_rate": None,
                    "funnel": {"sent": 0, "delivered": 0, "read": 0},
                    "avg_latency_seconds": None, "latency_sample_size": 0,
                }

            inbound = [r for r in rows if r.direction == "inbound"]
            outbound = [r for r in rows if r.direction == "outbound"]

            hourly_map: dict = {}
            for r in rows:
                bucket = r.created_at.strftime("%Y-%m-%dT%H:00")
                h = hourly_map.setdefault(bucket, {"inbound": 0, "outbound": 0})
                h["inbound" if r.direction == "inbound" else "outbound"] += 1
            hourly = [
                {"hour_bucket": b, "inbound": v["inbound"], "outbound": v["outbound"]}
                for b, v in sorted(hourly_map.items())
            ]

            # Webhook success rate and the delivery funnel only cover messages we
            # can actually track (have a wamid) — same reasoning as
            # broadcast_overview: an untracked send isn't the same as a failed one.
            trackable_out = [r for r in outbound if r.wamid]
            webhook_success_rate = (
                round(sum(1 for r in trackable_out if r.delivery_status != "failed") / len(trackable_out) * 100, 1)
                if trackable_out else None
            )
            sent = len(trackable_out)
            delivered = sum(1 for r in trackable_out if r.delivery_status in ("delivered", "read"))
            read_c = sum(1 for r in trackable_out if r.delivery_status == "read")

            # delivered_at only populates going forward (added alongside this
            # endpoint) — messages sent before that have no latency data, hence
            # the separate sample size so the frontend can flag a thin sample.
            latencies = [(r.delivered_at - r.created_at).total_seconds() for r in outbound if r.delivered_at]
            avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None

            return {
                "total_messages": len(rows),
                "inbound_count": len(inbound),
                "outbound_count": len(outbound),
                "hourly": hourly,
                "webhook_success_rate": webhook_success_rate,
                "funnel": {"sent": sent, "delivered": delivered, "read": read_c},
                "avg_latency_seconds": avg_latency,
                "latency_sample_size": len(latencies),
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/agent-performance")
async def agent_performance(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    agent_id: Optional[int] = Query(None),
    department_id: Optional[int] = Query(None),
    ctx: dict = Depends(require_tab_permission("analytics")),
):
    """AHT and resolution rate are real, computed directly from HandoffEvent
    pairs. "Speed to Active" (SLA) has no true "customer requested a human"
    signal in this schema, so it's approximated as first-takeover-for-a-phone
    minus first-inbound-message-for-that-phone — a real, defensible proxy,
    not a fabricated number. Conversion rate is separately sourced from
    Lead.status, since that's lead-qualification data, not chat resolution."""
    def _fetch():
        db = get_db()
        try:
            agents_by_name = {a.name: a for a in db.query(Agent).all()}
            allowed_names = None
            if agent_id:
                target = db.query(Agent).filter(Agent.id == agent_id).first()
                allowed_names = {target.name} if target else set()
            elif department_id:
                allowed_names = {a.name for a in db.query(Agent).filter(Agent.department_id == department_id).all()}

            hq = db.query(HandoffEvent)
            if date_from:
                hq = hq.filter(HandoffEvent.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                hq = hq.filter(HandoffEvent.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            events = hq.order_by(HandoffEvent.phone, HandoffEvent.created_at).all()
            if allowed_names is not None:
                events = [e for e in events if e.agent_name in allowed_names]

            # Pair each takeover with the next handback for the same phone, in
            # chronological order, to get a real AHT duration per chat.
            pairs = []  # (agent_name, phone, takeover_ts, handback_ts)
            open_by_phone = {}  # phone -> (agent_name, takeover_ts) — still open
            for ev in events:
                if ev.event_type == "takeover":
                    open_by_phone[ev.phone] = (ev.agent_name, ev.created_at)
                elif ev.event_type == "handback":
                    opened = open_by_phone.pop(ev.phone, None)
                    if opened:
                        pairs.append((opened[0], ev.phone, opened[1], ev.created_at))
                    # else: handback with no matching takeover in this window —
                    # the takeover happened before date_from, not enough info
                    # to compute a duration for it.
            open_pairs = [(name, phone) for phone, (name, _ts) in open_by_phone.items()]

            agent_stats: dict = {}

            def _entry(name):
                return agent_stats.setdefault(name, {
                    "takeovers": 0, "handbacks": 0, "durations": [], "open_chats": 0, "phones": set(),
                })

            for ev in events:
                e = _entry(ev.agent_name)
                if ev.event_type == "takeover":
                    e["takeovers"] += 1
                    e["phones"].add(ev.phone)
                else:
                    e["handbacks"] += 1
            for name, _phone, t_ts, h_ts in pairs:
                agent_stats[name]["durations"].append((h_ts - t_ts).total_seconds())
            for name, _phone in open_pairs:
                agent_stats[name]["open_chats"] += 1

            # SLA proxy: first takeover per phone vs. first inbound message for
            # that phone — see docstring above.
            first_takeover_by_phone: dict = {}
            for ev in events:
                if ev.event_type == "takeover" and ev.phone not in first_takeover_by_phone:
                    first_takeover_by_phone[ev.phone] = (ev.agent_name, ev.created_at)
            sla_by_agent: dict = {}
            if first_takeover_by_phone:
                first_inbound_rows = db.execute(
                    select(Message.phone, func.min(Message.created_at))
                    .where(Message.phone.in_(list(first_takeover_by_phone.keys())), Message.direction == "inbound")
                    .group_by(Message.phone)
                ).all()
                first_inbound_by_phone = {r[0]: r[1] for r in first_inbound_rows}
                for phone, (agent_name, takeover_ts) in first_takeover_by_phone.items():
                    first_inbound = first_inbound_by_phone.get(phone)
                    if first_inbound and takeover_ts > first_inbound:
                        sla_by_agent.setdefault(agent_name, []).append((takeover_ts - first_inbound).total_seconds())

            # Total conversations per agent — same aggregation /conversations-handled
            # already does, reused in-process rather than re-derived.
            conv_filters = [Message.direction == "outbound", Message.name != "KoolBot",
                             Message.name.isnot(None), Message.name != ""]
            if date_from:
                conv_filters.append(Message.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                conv_filters.append(Message.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            conv_rows = db.execute(
                select(Message.name, func.count(func.distinct(Message.phone)).label("count"))
                .where(and_(*conv_filters))
                .group_by(Message.name)
            ).all()
            conv_by_agent = {r.name: r.count for r in conv_rows}

            # Lead conversion rate — distinct phones an agent took over that are
            # also a converted Lead, over distinct phones they took over. Kept
            # separate from resolution_rate since it reflects lead-qualification
            # outcome, not whether the chat itself was handed back cleanly.
            converted_norm = set()
            for lp, lwp, status in db.query(Lead.phone, Lead.whatsapp_phone, Lead.status).all():
                if status == "converted":
                    if lp:
                        converted_norm.add(normalize_phone(lp))
                    if lwp:
                        converted_norm.add(normalize_phone(lwp))
            conversion_by_agent = {}
            for name, stats in agent_stats.items():
                phones = stats["phones"]
                if phones:
                    conv_count = sum(1 for p in phones if normalize_phone(p) in converted_norm)
                    conversion_by_agent[name] = round(conv_count / len(phones) * 100, 1)

            dept_names = {d.id: d.name for d in db.query(Department).all()}

            all_names = set(agent_stats.keys()) | set(conv_by_agent.keys())
            if allowed_names is not None:
                all_names &= allowed_names

            leaderboard = []
            for name in all_names:
                stats = agent_stats.get(name) or {"takeovers": 0, "handbacks": 0, "durations": [], "open_chats": 0, "phones": set()}
                agent_obj = agents_by_name.get(name)
                durations = stats["durations"]
                slas = sla_by_agent.get(name, [])
                leaderboard.append({
                    "agent": name,
                    "department_id": agent_obj.department_id if agent_obj else None,
                    "department": dept_names.get(agent_obj.department_id) if agent_obj and agent_obj.department_id else None,
                    "open_chats": stats["open_chats"],
                    "aht_minutes": round(sum(durations) / len(durations) / 60, 1) if durations else None,
                    "sla_seconds": round(sum(slas) / len(slas), 1) if slas else None,
                    "resolution_rate": round(stats["handbacks"] / stats["takeovers"] * 100, 1) if stats["takeovers"] else None,
                    "conversion_rate": conversion_by_agent.get(name),
                    "total_conversations": conv_by_agent.get(name, 0),
                })
            leaderboard.sort(key=lambda r: -r["total_conversations"])

            all_durations = [d for s in agent_stats.values() for d in s["durations"]]
            all_slas = [s for lst in sla_by_agent.values() for s in lst]
            total_closures = sum(s["handbacks"] for name, s in agent_stats.items() if name in all_names)

            return {
                "kpis": {
                    "avg_aht_minutes": round(sum(all_durations) / len(all_durations) / 60, 1) if all_durations else None,
                    "avg_sla_seconds": round(sum(all_slas) / len(all_slas), 1) if all_slas else None,
                    "total_closures": total_closures,
                },
                "leaderboard": leaderboard,
                "chart": [
                    {"agent": r["agent"], "total_conversations": r["total_conversations"], "avg_aht_minutes": r["aht_minutes"]}
                    for r in leaderboard
                ],
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)
