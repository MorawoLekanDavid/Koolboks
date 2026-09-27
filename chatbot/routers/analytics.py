from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, case, func, or_, select, text

from chatbot.config import BOT_SENDER_NAMES
from chatbot.database import get_db
from chatbot.routers.permissions import get_analytics_scope, require_org_wide_analytics, require_tab_permission
from chatbot.models import Agent, AgentHeartbeatLog, AgentLoginEvent, ConversationScore, Department, Escalation, HandoffEvent, Lead, Message
from chatbot.services.presence_service import HEARTBEAT_TTL
from chatbot.utils.phone import normalize_phone

router = APIRouter(prefix="/admin/analytics", tags=["analytics"])


@router.get("/conversations-handled")
async def conversations_handled(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_org_wide_analytics),
):
    def _fetch():
        db = get_db()
        try:
            filters = [
                Message.direction == "outbound",
                Message.name.notin_(BOT_SENDER_NAMES),
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
    ctx: dict = Depends(require_org_wide_analytics),
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
async def product_recommendations(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    ctx: dict = Depends(require_org_wide_analytics),
):
    """Was hard-capped at the top 10 products ever recommended — fine while
    the catalogue was small, but it silently hid the long tail as more
    products (and product-name variants) accumulated. Now paginated instead,
    same {items, total, page, page_size} envelope as everywhere else; `pct`
    is still computed against the TRUE grand total across every product, not
    just whichever page is showing, so the percentages stay meaningful no
    matter which page you're looking at."""
    def _fetch():
        db = get_db()
        try:
            base = (
                select(Lead.product_interest, func.count(Lead.id).label("count"))
                .where(Lead.product_interest != None, Lead.product_interest != "")
                .group_by(Lead.product_interest)
            )
            all_rows = db.execute(base).all()
            grand_total = sum(r.count for r in all_rows)
            total_products = len(all_rows)

            rows = db.execute(
                base.order_by(func.count(Lead.id).desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            items = [
                {"product": r.product_interest, "count": r.count,
                 "pct": round(r.count / grand_total * 100) if grand_total else 0}
                for r in rows
            ]
            return {"items": items, "total": total_products, "page": page, "page_size": page_size}
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/broadcast-overview")
async def broadcast_overview(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_org_wide_analytics),
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
async def broadcast_campaigns_list(ctx: dict = Depends(require_org_wide_analytics)):
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
async def broadcast_by_template(ctx: dict = Depends(require_org_wide_analytics)):
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
async def lead_funnel(ctx: dict = Depends(require_org_wide_analytics)):
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
    flagged_page: int = Query(1, ge=1),
    flagged_page_size: int = Query(20, ge=1, le=200),
    ctx: dict = Depends(require_org_wide_analytics),
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
                        "issue_counts": {},
                        "flagged": {"items": [], "total": 0, "page": flagged_page, "page_size": flagged_page_size},
                        "trend": [],
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

            # Was hard-capped at the 50 most recent flagged conversations --
            # fine as a stopgap, but it silently hid anything older than that
            # with no way to see the rest. Paginated instead, same envelope
            # as everywhere else; avg_score/trend/issue_counts above are
            # unaffected since they're computed from the full `rows`, not
            # this list.
            all_flagged_rows = [r for r in rows if r.likely_lost_customer]
            flagged_total = len(all_flagged_rows)
            fp_start = (flagged_page - 1) * flagged_page_size
            flagged_rows = all_flagged_rows[fp_start:fp_start + flagged_page_size]
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

            flagged_items = [
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
                "flagged": {
                    "items": flagged_items,
                    "total": flagged_total,
                    "page": flagged_page,
                    "page_size": flagged_page_size,
                },
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
    ctx: dict = Depends(require_org_wide_analytics),
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
    leaderboard_page: int = Query(1, ge=1),
    leaderboard_page_size: int = Query(20, ge=1, le=200),
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
            scope = get_analytics_scope(db, ctx)
            agents_by_name = {a.name: a for a in db.query(Agent).all()}
            allowed_names = None
            if agent_id:
                target = db.query(Agent).filter(Agent.id == agent_id).first()
                allowed_names = {target.name} if target else set()
            elif department_id:
                allowed_names = {a.name for a in db.query(Agent).filter(Agent.department_id == department_id).all()}

            if not scope["unrestricted"]:
                # Hard server-side boundary — query params can only narrow
                # within the caller's own scope, never escape it.
                allowed_names = scope["agent_names"] if allowed_names is None else (allowed_names & scope["agent_names"])

            hq = db.query(HandoffEvent)
            if date_from:
                hq = hq.filter(HandoffEvent.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                hq = hq.filter(HandoffEvent.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            events = hq.order_by(HandoffEvent.phone, HandoffEvent.created_at).all()

            # HandoffEvent.agent_name is really "whatever label silenced the
            # bot" — a real agent's name when a human takes over, but also a
            # system-generated placeholder for a non-agent handoff: the
            # escalation engine stamps a unique "Escalation #N (category)"
            # label per ticket (see escalation_service.set_handoff), and an
            # older media-handoff path used a fixed "Awaiting agent (media)"
            # label. None of those are a real agent, so left unfiltered they
            # show up as one bogus "agent" row apiece — confirmed live, a
            # leaderboard with a dozen "Escalation #N" rows — and their
            # durations skew the org-wide AHT/SLA averages since those
            # tickets can legitimately sit open for hours before a human
            # actually engages, which has nothing to do with any agent's
            # handling speed. A deleted agent's historical events are an
            # accepted, minor loss from this same filter — preferable to
            # counting fictitious ones.
            events = [e for e in events if e.agent_name in agents_by_name]
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
            conv_filters = [Message.direction == "outbound", Message.name.notin_(BOT_SENDER_NAMES),
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

            # Same non-agent-label guard as the HandoffEvent filter above --
            # conv_by_agent is sourced independently from Message.name, so it
            # needs its own real-agent check rather than trusting it just
            # because agent_stats (already filtered) happens to agree.
            all_names = (set(agent_stats.keys()) | set(conv_by_agent.keys())) & set(agents_by_name.keys())
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
                    # Paired takeover->handback count, not the raw handback
                    # count -- a handback whose takeover happened before
                    # date_from still increments "handbacks" with no matching
                    # takeover in this window, which could push the old
                    # handbacks/takeovers formula over 100%. len(durations) is
                    # bounded by takeovers by construction (one pair consumes
                    # exactly one in-window takeover), so this can't happen.
                    "resolution_rate": round(len(durations) / stats["takeovers"] * 100, 1) if stats["takeovers"] else None,
                    "conversion_rate": conversion_by_agent.get(name),
                    "total_conversations": conv_by_agent.get(name, 0),
                })
            leaderboard.sort(key=lambda r: -r["total_conversations"])

            all_durations = [d for s in agent_stats.values() for d in s["durations"]]
            all_slas = [s for lst in sla_by_agent.values() for s in lst]
            total_closures = sum(s["handbacks"] for name, s in agent_stats.items() if name in all_names)

            # Chart reflects every real agent regardless of page — it's a
            # different view of the same data, not a continuation of the list.
            chart = [
                {"agent": r["agent"], "total_conversations": r["total_conversations"], "avg_aht_minutes": r["aht_minutes"]}
                for r in leaderboard
            ]
            leaderboard_total = len(leaderboard)
            lb_start = (leaderboard_page - 1) * leaderboard_page_size
            leaderboard_page_items = leaderboard[lb_start:lb_start + leaderboard_page_size]

            return {
                "kpis": {
                    "avg_aht_minutes": round(sum(all_durations) / len(all_durations) / 60, 1) if all_durations else None,
                    "avg_sla_seconds": round(sum(all_slas) / len(all_slas), 1) if all_slas else None,
                    "total_closures": total_closures,
                },
                "leaderboard": {
                    "items": leaderboard_page_items,
                    "total": leaderboard_total,
                    "page": leaderboard_page,
                    "page_size": leaderboard_page_size,
                },
                "chart": chart,
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/escalations")
async def escalation_analytics(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    agent_id: Optional[int] = Query(None),
    department_id: Optional[int] = Query(None),
    leaderboard_page: int = Query(1, ge=1),
    leaderboard_page_size: int = Query(20, ge=1, le=200),
    ctx: dict = Depends(require_tab_permission("analytics")),
):
    """Business/data-analyst view of the Human Escalation Engine (see
    chatbot/services/escalation_service.py): volume trend, response and
    resolution speed, category/priority/status mix, and a per-agent
    resolution leaderboard. Filtered by created_at (when the ticket was
    opened) — same date-range convention as every other Analytics Console
    tab. Scoped the same way Agent Performance is: a restricted role only
    sees escalations assigned to agents within their own scope."""
    def _fetch():
        db = get_db()
        try:
            scope = get_analytics_scope(db, ctx)
            agents_by_id = {a.id: a for a in db.query(Agent).all()}
            allowed_ids = None
            if agent_id:
                allowed_ids = {agent_id}
            elif department_id:
                allowed_ids = {a.id for a in agents_by_id.values() if a.department_id == department_id}
            if not scope["unrestricted"]:
                allowed_ids = scope["agent_ids"] if allowed_ids is None else (allowed_ids & scope["agent_ids"])

            q = db.query(Escalation)
            if date_from:
                q = q.filter(Escalation.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                q = q.filter(Escalation.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            if allowed_ids is not None:
                q = q.filter(Escalation.assigned_agent_id.in_(allowed_ids))
            rows = q.order_by(Escalation.created_at.desc()).all()

            empty_leaderboard = {"items": [], "total": 0, "page": leaderboard_page, "page_size": leaderboard_page_size}
            if not rows:
                return {
                    "kpis": {"total": 0, "open_count": 0, "avg_first_response_minutes": None,
                             "avg_resolution_minutes": None, "resolution_rate_pct": None,
                             "sla_breach_rate_pct": None},
                    "trend": [], "by_category": [], "by_priority": [], "by_status": [],
                    "leaderboard": empty_leaderboard,
                }

            total = len(rows)
            OPEN_STATUSES = ("open", "assigned", "in_progress", "reopened")
            open_count = sum(1 for r in rows if r.status in OPEN_STATUSES)
            resolved_rows = [r for r in rows if r.status == "resolved"]

            first_response_minutes = [
                (r.first_response_at - r.created_at).total_seconds() / 60
                for r in rows if r.first_response_at
            ]
            resolution_minutes = [
                (r.resolved_at - r.created_at).total_seconds() / 60
                for r in resolved_rows if r.resolved_at
            ]
            # SLA breach = the watchdog had to page the routing fallback agent
            # because nobody responded in time (see check_sla_breaches() in
            # escalation_service.py) — a real, already-computed signal, not a
            # newly-invented threshold.
            sla_breaches = sum(1 for r in rows if r.sla_notified_at is not None)

            # Volume by the calendar day the ticket was opened, in Lagos time
            # (same convention as Shift Tracker — every agent reading this is
            # in Nigeria).
            trend_map: dict = {}
            for r in rows:
                day = (r.created_at + LAGOS_UTC_OFFSET).date().isoformat()
                trend_map[day] = trend_map.get(day, 0) + 1
            trend = [{"date": d, "count": c} for d, c in sorted(trend_map.items())]

            def _bucket(field):
                counts: dict = {}
                for r in rows:
                    val = getattr(r, field) or "other"
                    counts[val] = counts.get(val, 0) + 1
                return [{"key": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]

            # Per-agent resolution leaderboard — who's actually closing these
            # out and how fast. Unassigned tickets have nothing to attribute
            # this to, so they're excluded from this specific breakdown (they
            # still count fully in every KPI/chart above).
            agent_stats: dict = {}
            for r in rows:
                if not r.assigned_agent_id:
                    continue
                s = agent_stats.setdefault(r.assigned_agent_id, {"assigned": 0, "resolved": 0, "resolution_minutes": [], "reopens": 0})
                s["assigned"] += 1
                if r.status == "resolved":
                    s["resolved"] += 1
                    if r.resolved_at:
                        s["resolution_minutes"].append((r.resolved_at - r.created_at).total_seconds() / 60)
                s["reopens"] += r.reopen_count or 0

            leaderboard_full = []
            for aid, s in agent_stats.items():
                agent_obj = agents_by_id.get(aid)
                leaderboard_full.append({
                    "agent": agent_obj.name if agent_obj else f"Agent {aid}",
                    "assigned": s["assigned"],
                    "resolved": s["resolved"],
                    "avg_resolution_minutes": round(sum(s["resolution_minutes"]) / len(s["resolution_minutes"]), 1) if s["resolution_minutes"] else None,
                    "reopen_count": s["reopens"],
                })
            leaderboard_full.sort(key=lambda r: -r["assigned"])
            lb_start = (leaderboard_page - 1) * leaderboard_page_size

            return {
                "kpis": {
                    "total": total,
                    "open_count": open_count,
                    "avg_first_response_minutes": round(sum(first_response_minutes) / len(first_response_minutes), 1) if first_response_minutes else None,
                    "avg_resolution_minutes": round(sum(resolution_minutes) / len(resolution_minutes), 1) if resolution_minutes else None,
                    "resolution_rate_pct": round(len(resolved_rows) / total * 100, 1),
                    "sla_breach_rate_pct": round(sla_breaches / total * 100, 1),
                },
                "trend": trend,
                "by_category": _bucket("category"),
                "by_priority": _bucket("priority"),
                "by_status": _bucket("status"),
                "leaderboard": {
                    "items": leaderboard_full[lb_start:lb_start + leaderboard_page_size],
                    "total": len(leaderboard_full),
                    "page": leaderboard_page,
                    "page_size": leaderboard_page_size,
                },
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


_UA_OS_PATTERNS = [
    ("Windows", "Windows"), ("Mac OS", "macOS"), ("iPhone", "iOS"),
    ("iPad", "iOS"), ("Android", "Android"), ("Linux", "Linux"),
]


def _parse_device_os(user_agent: Optional[str]) -> str:
    if not user_agent:
        return "Unknown"
    for needle, label in _UA_OS_PATTERNS:
        if needle in user_agent:
            return label
    return "Unknown"


# Nigeria (WAT) never observes daylight saving, so a fixed offset is safe
# year-round — every agent using the Shift Tracker is on this timezone.
LAGOS_UTC_OFFSET = timedelta(hours=1)


def lagos_day_bounds(date_str: Optional[str], now: datetime) -> tuple:
    """Turns a LOCAL (WAT) calendar day -- either an explicit "YYYY-MM-DD"
    from the date picker, or "today" if none given -- into the UTC
    [day_start, day_end] window that actually bounds it in the database,
    since every stored timestamp is UTC. Returns (day, day_start, day_end,
    now_lagos_date) — the last one is what the "is this segment still open
    right now" check compares `day` against, for the same reason.

    Without this, a login at 23:52 UTC (00:52 WAT the next calendar day) was
    invisible when querying "today," because the UTC calendar day hadn't
    turned over yet even though the agent's own local day had."""
    now_lagos_date = (now + LAGOS_UTC_OFFSET).date()
    day = datetime.fromisoformat(date_str).date() if date_str else now_lagos_date
    day_start = datetime(day.year, day.month, day.day) - LAGOS_UTC_OFFSET
    day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)
    return day, day_start, day_end, now_lagos_date


def session_belongs_to_report_day(login_at: datetime, day_start: datetime, day_end: datetime, has_activity_that_day: bool) -> bool:
    """A login session belongs in a given day's audit if it started that
    day, OR — for a session that started earlier — if there's heartbeat
    evidence it was still active at some point during that day.

    Without the second clause, a session spanning midnight (logged in
    yesterday, never logged out, still open or closed sometime today) shows
    up in the Presence Timeline (built straight from today's heartbeats) but
    never in the audit table (which only ever looked at login_at) — same
    session, inconsistent report. Confirmed live: an agent's ongoing session
    appeared in the Gantt chart's agent list but the audit table below it
    said "No logins recorded for this day".

    Without the FIRST clause (i.e. requiring activity for every login
    regardless of when it started), a login that crashed before its first
    heartbeat would vanish from its own day's report instead of correctly
    showing as Dropped."""
    started_that_day = day_start <= login_at <= day_end
    return started_that_day or has_activity_that_day


def last_heartbeat_in_session(heartbeats: list, login_at: datetime, next_login_at: Optional[datetime]):
    """Returns the last heartbeat row (anything with a .logged_at, in
    ascending order) whose timestamp falls strictly within ONE login
    session's window -- after that login, before the agent's next login (or
    unbounded if it was their last login of the day). None if nothing falls
    in the window.

    This is what scopes a heartbeat to a single session. Without it, an
    agent's single most-recent heartbeat of the whole day gets applied to
    EVERY one of their open logins that day — confirmed live: an agent with
    three same-day logins (two of them hours-old and long since superseded)
    showed all three as "Active" simultaneously, because the old code just
    grabbed the day's last heartbeat once per agent, not once per session."""
    window = [
        h for h in heartbeats
        if h.logged_at > login_at and (next_login_at is None or h.logged_at < next_login_at)
    ]
    return window[-1] if window else None


def classify_session_status(
    logout_at: Optional[datetime],
    last_heartbeat_status: Optional[str],
    last_heartbeat_at: Optional[datetime],
    now: datetime,
) -> str:
    """Turns one AgentLoginEvent + that agent's most recent heartbeat row into
    the three states the Login & Device Audit table shows. Pulled out as its
    own pure function (no DB/Redis access) specifically so this — the exact
    logic distinguishing a confirmed logout from a dropped/expired session —
    has direct unit test coverage instead of only being exercised inline
    inside a query handler. See tests/test_shift_tracker.py.

    - "Ended": logout_at is set. Only presence.py's POST /admin/presence/logout
      ever sets it (a real, confirmed Sign Out) — /admin/presence/offline
      (fired by sendBeacon on refresh/tab-close/crash/network loss) deliberately
      never does, so this can no longer be a false positive from a page just
      going away.
    - "Active": no logout_at, but the last heartbeat is recent and non-offline
      — the tab is still genuinely open.
    - "Dropped": no logout_at, and the heartbeat has gone stale or offline —
      presence disappeared without ever confirming a logout (crash, network
      loss, tab closed, laptop shut, browser suspended). This is the case
      that used to be miscounted as "Ended" before this fix.
    """
    if logout_at:
        return "Ended"
    if (
        last_heartbeat_status
        and last_heartbeat_status != "offline"
        and last_heartbeat_at
        and (now - last_heartbeat_at).total_seconds() < HEARTBEAT_TTL + 60
    ):
        return "Active"
    return "Dropped"


@router.get("/shift-tracker")
async def shift_tracker(
    date: Optional[str] = Query(None),
    agent_id: Optional[int] = Query(None),
    department_id: Optional[int] = Query(None),
    audit_page: int = Query(1, ge=1),
    audit_page_size: int = Query(20, ge=1, le=200),
    ctx: dict = Depends(require_tab_permission("analytics")),
):
    """Single-day view — the Gantt is inherently per-day, unlike every other
    Analytics Console tab which uses the global date-range filter. Built
    entirely from AgentHeartbeatLog/AgentLoginEvent, which only exist from
    the point this feature shipped — earlier dates return empty, not wrong.

    See lagos_day_bounds() for why `date` (a browser <input type="date">
    value) isn't treated as a UTC day. The Login & Device Audit list is
    paginated (`audit_page`/`audit_page_size`) since it only grows as agents
    log in and out — the Gantt/KPIs above it are unaffected, they're
    day-scoped aggregates, not a list."""
    def _fetch():
        db = get_db()
        try:
            now = datetime.utcnow()
            day, day_start, day_end, now_lagos_date = lagos_day_bounds(date, now)

            scope = get_analytics_scope(db, ctx)
            agents_by_id = {a.id: a for a in db.query(Agent).all()}
            allowed_ids = None
            if agent_id:
                allowed_ids = {agent_id}
            elif department_id:
                allowed_ids = {a.id for a in agents_by_id.values() if a.department_id == department_id}

            if not scope["unrestricted"]:
                # Hard server-side boundary — query params can only narrow
                # within the caller's own scope, never escape it.
                allowed_ids = scope["agent_ids"] if allowed_ids is None else (allowed_ids & scope["agent_ids"])

            hb_q = db.query(AgentHeartbeatLog).filter(
                AgentHeartbeatLog.logged_at >= day_start, AgentHeartbeatLog.logged_at <= day_end
            )
            if allowed_ids is not None:
                hb_q = hb_q.filter(AgentHeartbeatLog.agent_id.in_(allowed_ids))
            hb_rows = hb_q.order_by(AgentHeartbeatLog.agent_id, AgentHeartbeatLog.logged_at).all()

            rows_by_agent: dict = {}
            for r in hb_rows:
                rows_by_agent.setdefault(r.agent_id, []).append(r)

            # Run-length-encode consecutive heartbeat rows into timeline segments.
            segments_by_agent: dict = {}
            for aid, rows in rows_by_agent.items():
                segs = []
                for i, r in enumerate(rows):
                    start = r.logged_at
                    if i + 1 < len(rows):
                        end = rows[i + 1].logged_at
                    elif r.status == "offline":
                        end = start
                    else:
                        # No closing row — the heartbeat silently expired
                        # (crash/network drop never fires the offline beacon).
                        # Cap the open segment at one TTL window past the last
                        # ping rather than dragging it out to "now".
                        cap = start + timedelta(seconds=HEARTBEAT_TTL)
                        end = min(cap, now) if day == now_lagos_date else cap
                    segs.append({"status": r.status, "start": start.isoformat(), "end": end.isoformat()})
                segments_by_agent[aid] = segs

            total_online_agents = sum(
                1 for segs in segments_by_agent.values() if any(s["status"] == "online" for s in segs)
            )

            shift_minutes, idle_rates = [], []
            for aid, rows in rows_by_agent.items():
                non_offline = [r for r in rows if r.status != "offline"]
                if not non_offline:
                    continue
                segs = segments_by_agent[aid]
                first_ts = non_offline[0].logged_at
                last_ts = max(datetime.fromisoformat(s["end"]) for s in segs if s["status"] != "offline")
                shift_minutes.append((last_ts - first_ts).total_seconds() / 60)

                online_secs = sum(
                    (datetime.fromisoformat(s["end"]) - datetime.fromisoformat(s["start"])).total_seconds()
                    for s in segs if s["status"] == "online"
                )
                away_secs = sum(
                    (datetime.fromisoformat(s["end"]) - datetime.fromisoformat(s["start"])).total_seconds()
                    for s in segs if s["status"] == "away"
                )
                if online_secs + away_secs > 0:
                    # Idle rate = share of *working* time (online+away) spent
                    # away — offline time isn't "idle while working", it's not
                    # working at all, so it's excluded from the denominator.
                    idle_rates.append(away_secs / (online_secs + away_secs) * 100)

            timeline = [
                {"agent_id": aid, "agent": agents_by_id[aid].name if aid in agents_by_id else f"Agent {aid}", "segments": segs}
                for aid, segs in segments_by_agent.items()
            ]
            timeline.sort(key=lambda t: t["agent"])

            # A session doesn't reset just because the calendar does — an agent
            # who logged in yesterday and never logged out (still working past
            # midnight, or dropped without a clean close) has heartbeats today
            # that need a login row to attach to. Confirmed live: "customer
            # success" showed up in the Presence Timeline (built from today's
            # heartbeats) but not in the audit table below (which only looked
            # at login_at falling inside today) — same agent, same session,
            # inconsistent report. So the query is widened to any login that
            # could plausibly still be running today (started at or before
            # day_end, and not already closed before today began); which of
            # those are ACTUALLY relevant to today gets decided below by
            # whether real activity happened today, not by this query alone.
            # CROSS_DAY_LOOKBACK just keeps that widened query from scanning
            # the entire table's history for how far back to check.
            CROSS_DAY_LOOKBACK = timedelta(days=7)
            login_q = db.query(AgentLoginEvent).filter(
                AgentLoginEvent.login_at <= day_end,
                AgentLoginEvent.login_at >= day_start - CROSS_DAY_LOOKBACK,
                or_(AgentLoginEvent.logout_at.is_(None), AgentLoginEvent.logout_at >= day_start),
            )
            if allowed_ids is not None:
                login_q = login_q.filter(AgentLoginEvent.agent_id.in_(allowed_ids))
            candidate_logins = login_q.order_by(AgentLoginEvent.login_at.desc()).all()

            # Boundaries for scoping a heartbeat to ONE specific login session --
            # an agent can have several logins in this window (multiple
            # devices, logging back in after a dropped session, or a session
            # spanning midnight), and only a heartbeat strictly between a
            # login and that agent's NEXT login could possibly belong to it.
            login_rows_by_agent: dict = {}
            for ev in candidate_logins:
                login_rows_by_agent.setdefault(ev.agent_id, []).append(ev)
            for evs in login_rows_by_agent.values():
                evs.sort(key=lambda e: e.login_at)

            audit = []
            for ev in candidate_logins:
                agent_obj = agents_by_id.get(ev.agent_id)
                agent_logins = login_rows_by_agent[ev.agent_id]
                idx = agent_logins.index(ev)
                next_login_at = agent_logins[idx + 1].login_at if idx + 1 < len(agent_logins) else None

                last_hb = last_heartbeat_in_session(rows_by_agent.get(ev.agent_id, []), ev.login_at, next_login_at)
                if not session_belongs_to_report_day(ev.login_at, day_start, day_end, has_activity_that_day=last_hb is not None):
                    continue

                session_status = classify_session_status(
                    ev.logout_at,
                    last_hb.status if last_hb else None,
                    last_hb.logged_at if last_hb else None,
                    now,
                )
                audit.append({
                    "agent": agent_obj.name if agent_obj else f"Agent {ev.agent_id}",
                    "login_at": ev.login_at.isoformat() if ev.login_at else None,
                    "logout_at": ev.logout_at.isoformat() if ev.logout_at else None,
                    "device_os": _parse_device_os(ev.user_agent),
                    "ip_address": ev.ip_address,
                    "session_status": session_status,
                })

            audit_total = len(audit)
            audit_start = (audit_page - 1) * audit_page_size
            audit_page_items = audit[audit_start:audit_start + audit_page_size]

            return {
                "date": day.isoformat(),
                "kpis": {
                    "total_online_agents": total_online_agents,
                    "avg_shift_minutes": round(sum(shift_minutes) / len(shift_minutes), 1) if shift_minutes else None,
                    "avg_idle_rate_pct": round(sum(idle_rates) / len(idle_rates), 1) if idle_rates else None,
                },
                "timeline": timeline,
                "audit": {
                    "items": audit_page_items,
                    "total": audit_total,
                    "page": audit_page,
                    "page_size": audit_page_size,
                },
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


# A gap of this many hours between two inbound messages from the same phone
# marks the start of a new "session" — chosen to match FOLLOW_UP_HOURS, the
# other place this app already draws the line between "still talking" and
# "came back later," rather than the much shorter Redis CHAT_TTL (1h), which
# is a technical cache expiry, not a meaningful customer-behavior boundary.
RETURNING_CUSTOMER_GAP_HOURS = 24


@router.get("/customer-retention")
async def customer_retention(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    ctx: dict = Depends(require_org_wide_analytics),
):
    """A phone's first-ever session is "first-time"; every session after a
    RETURNING_CUSTOMER_GAP_HOURS-hour gap is "returning". Telling whether an
    in-range session is truly that phone's first requires their full inbound
    history, not just the filtered range, so this scans all inbound messages
    rather than a date-bounded query."""
    def _fetch():
        db = get_db()
        try:
            rows = db.execute(
                select(Message.phone, Message.created_at)
                .where(Message.direction == "inbound")
                .order_by(Message.phone, Message.created_at)
            ).all()

            range_start = datetime.fromisoformat(date_from) if date_from else None
            range_end = datetime.fromisoformat(date_to + "T23:59:59") if date_to else None

            def _in_range(ts):
                return (range_start is None or ts >= range_start) and (range_end is None or ts <= range_end)

            by_phone: dict = {}
            for r in rows:
                by_phone.setdefault(r.phone, []).append(r.created_at)

            gap = timedelta(hours=RETURNING_CUSTOMER_GAP_HOURS)
            first_time_count = 0
            returning_count = 0
            active_phones_in_range = set()
            trend_map: dict = {}

            for phone, timestamps in by_phone.items():
                if any(_in_range(t) for t in timestamps):
                    active_phones_in_range.add(phone)

                session_starts = [timestamps[0]]
                for prev, cur in zip(timestamps, timestamps[1:]):
                    if cur - prev >= gap:
                        session_starts.append(cur)

                for i, start in enumerate(session_starts):
                    if not _in_range(start):
                        continue
                    day = start.date().isoformat()
                    bucket = trend_map.setdefault(day, {"first_time": 0, "returning": 0})
                    if i == 0:
                        first_time_count += 1
                        bucket["first_time"] += 1
                    else:
                        returning_count += 1
                        bucket["returning"] += 1

            trend = [
                {"date": d, "first_time": v["first_time"], "returning": v["returning"]}
                for d, v in sorted(trend_map.items())
            ]

            return {
                "kpis": {
                    "first_time": first_time_count,
                    "returning": returning_count,
                    "total_unique_customers": len(active_phones_in_range),
                },
                "trend": trend,
            }
        finally:
            db.close()
    return await run_in_threadpool(_fetch)
