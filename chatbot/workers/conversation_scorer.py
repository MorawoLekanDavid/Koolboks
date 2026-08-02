import asyncio
from datetime import datetime, timedelta

from sqlalchemy import func, select

from chatbot.config import CONVERSATION_SCORE_IDLE_MINUTES, CONVERSATION_SCORING_ENABLED, log
from chatbot.database import get_db
from chatbot.models import ConversationScore, Message
from chatbot.services.conversation_scoring import score_conversation


async def run_conversation_scoring():
    """Score every conversation that has new messages since its last-scored point
    and has gone idle (no message in CONVERSATION_SCORE_IDLE_MINUTES)."""
    cutoff = datetime.utcnow() - timedelta(minutes=CONVERSATION_SCORE_IDLE_MINUTES)
    db = get_db()
    try:
        last_score_map = {
            r.phone: r.scored_through
            for r in db.execute(
                select(ConversationScore.phone,
                       func.max(ConversationScore.scored_through).label("scored_through"))
                .group_by(ConversationScore.phone)
            ).all()
        }

        idle_phones = db.execute(
            select(Message.phone, func.max(Message.created_at).label("last_msg"))
            .group_by(Message.phone)
            .having(func.max(Message.created_at) < cutoff)
        ).all()

        scored = 0
        for row in idle_phones:
            phone = row.phone
            since = last_score_map.get(phone)
            q = select(Message).where(Message.phone == phone)
            if since:
                q = q.where(Message.created_at > since)
            q = q.order_by(Message.created_at.asc())
            msgs = db.execute(q).scalars().all()
            if not msgs:
                continue

            has_outbound = any(m.direction == "outbound" for m in msgs)
            if not has_outbound:
                # Customer messaged and got zero reply before going idle — a clear,
                # deterministic "lost customer" signal, no LLM call needed to detect it.
                db.add(ConversationScore(
                    phone=phone, quality_score=1, likely_lost_customer=True,
                    responder_type=None, issues="no_response_at_all",
                    reasoning="No bot or agent reply before the conversation went idle.",
                    message_count=len(msgs), scored_through=msgs[-1].created_at,
                ))
                db.commit()
                scored += 1
                continue

            result = await score_conversation(msgs)
            if not result:
                continue

            db.add(ConversationScore(
                phone=phone,
                quality_score=result["quality_score"],
                likely_lost_customer=result["likely_lost_customer"],
                responder_type=result["responder_type"],
                issues=",".join(result["issues"]),
                reasoning=result["reasoning"],
                message_count=len(msgs),
                scored_through=msgs[-1].created_at,
            ))
            db.commit()
            scored += 1

        if scored:
            log.info(f"Conversation scoring run complete: {scored} conversation(s) scored")
    except Exception as e:
        log.error(f"Conversation scoring run error: {e}")
    finally:
        db.close()


async def conversation_scoring_worker():
    """Periodic background task that scores idle conversations."""
    log.info(f"Conversation scoring worker started (enabled={CONVERSATION_SCORING_ENABLED}, "
              f"idle_minutes={CONVERSATION_SCORE_IDLE_MINUTES})")
    while True:
        try:
            await asyncio.sleep(600)  # check every 10 minutes
            if CONVERSATION_SCORING_ENABLED:
                await run_conversation_scoring()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Conversation scoring worker error: {e}")
