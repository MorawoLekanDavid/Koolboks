from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from chatbot.config import log
from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx
from chatbot.models import AgentHeartbeatLog, AgentLoginEvent
from chatbot.services import presence_service

router = APIRouter(prefix="/admin/presence", tags=["presence"])

# In-process throttle cache (agent_id -> (last_logged_status, last_logged_at)).
# Not shared across worker processes/restarts — worst case that logs a few
# extra rows, which is harmless; it just avoids a DB write on every 20s ping.
_HEARTBEAT_LOG_THROTTLE_SECONDS = 60
_last_logged: dict[int, tuple[str, datetime]] = {}


def _write_heartbeat_log(agent_id: int, status: str) -> None:
    db = get_db()
    try:
        db.add(AgentHeartbeatLog(agent_id=agent_id, status=status))
        db.commit()
    except Exception as e:
        log.warning(f"Failed to log heartbeat for agent {agent_id}: {e}")
    finally:
        db.close()


async def _maybe_log_status(agent_id: int, status: str, force: bool = False) -> None:
    """Writes a new AgentHeartbeatLog row unless the status is unchanged and
    still within the throttle window — force=True (away toggle, going
    offline) always writes immediately so those transitions don't visibly
    lag on the Gantt timeline."""
    prev = _last_logged.get(agent_id)
    now = datetime.utcnow()
    if not force and prev and prev[0] == status and (now - prev[1]).total_seconds() < _HEARTBEAT_LOG_THROTTLE_SECONDS:
        return
    _last_logged[agent_id] = (status, now)
    await run_in_threadpool(_write_heartbeat_log, agent_id, status)


def _close_open_login(agent_id: int) -> None:
    db = get_db()
    try:
        row = (
            db.query(AgentLoginEvent)
            .filter(AgentLoginEvent.agent_id == agent_id, AgentLoginEvent.logout_at.is_(None))
            .order_by(AgentLoginEvent.login_at.desc())
            .first()
        )
        if row:
            row.logout_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        log.warning(f"Failed to close login event for agent {agent_id}: {e}")
    finally:
        db.close()


@router.post("/heartbeat")
async def heartbeat(ctx: dict = Depends(get_admin_ctx)):
    """Pinged every 20s while the dashboard tab is open and visible. Refreshes
    the 45s liveness TTL — missing a couple of these (tab crash, network
    drop) self-heals to offline without needing an explicit signal."""
    agent_id = ctx.get("agent_id")
    if agent_id:
        await presence_service.heartbeat(agent_id)
        status = await presence_service.get_status(agent_id)
        await _maybe_log_status(agent_id, status)
    return {"ok": True}


@router.post("/offline")
async def go_offline(ctx: dict = Depends(get_admin_ctx)):
    """Fired via navigator.sendBeacon() on tab close/refresh/navigation —
    goes offline immediately instead of waiting out the heartbeat TTL."""
    agent_id = ctx.get("agent_id")
    if agent_id:
        await presence_service.clear_heartbeat(agent_id)
        await _maybe_log_status(agent_id, "offline", force=True)
        await run_in_threadpool(_close_open_login, agent_id)
    return {"ok": True}


class AwayIn(BaseModel):
    away: bool


@router.put("/away")
async def set_away(body: AwayIn, ctx: dict = Depends(get_admin_ctx)):
    """Manual override — 'here but stepped out' without closing the tab.
    Layered on top of the heartbeat: still requires an alive heartbeat to
    show as anything other than offline."""
    agent_id = ctx.get("agent_id")
    if not agent_id:
        raise HTTPException(400, "Super admin sessions via the master key don't have a presence status.")
    await presence_service.set_away(agent_id, body.away)
    await _maybe_log_status(agent_id, "away" if body.away else "online", force=True)
    return {"away": body.away}
