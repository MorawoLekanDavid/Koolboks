from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from chatbot.config import log
from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx, invalidate_session
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
    """Stamps logout_at on an agent's most recent still-open login row.
    Called from EXACTLY ONE place: the confirmed /logout endpoint below.
    This is the whole fix for the "still working but analytics showed an
    earlier logout" bug — logout_at used to also get closed by /offline
    (a sendBeacon fired on ANY page-exit: refresh, tab close, crash,
    network loss, laptop shutdown, none of which mean the agent actually
    logged out), so a flaky wifi moment could look identical to a real
    end-of-shift. Now only a deliberate click on Sign Out can close this
    row — every other way a session ends leaves logout_at null, which
    shift_tracker's classify_session_status() (analytics.py) correctly
    reads as "Dropped" rather than "Ended"."""
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
    the 90s liveness TTL — missing a couple of these (tab crash, network
    drop) self-heals to offline without needing an explicit signal, and
    WITHOUT ever touching AgentLoginEvent.logout_at (see _close_open_login's
    docstring) — a heartbeat that simply stops is a dropped/expired
    presence, not a logout, and is reported as such."""
    agent_id = ctx.get("agent_id")
    if agent_id:
        await presence_service.heartbeat(agent_id)
        status = await presence_service.get_status(agent_id)
        await _maybe_log_status(agent_id, status)
    return {"ok": True}


@router.post("/offline")
async def go_offline(ctx: dict = Depends(get_admin_ctx)):
    """Best-effort "the page appears to have gone away" signal — fired via
    navigator.sendBeacon() on tab close, refresh, or navigation. sendBeacon
    fires for all of those indiscriminately (and sometimes not at all, on a
    crash or a yanked network cable), so it is NOT reliable evidence that
    the agent actually logged out; it only ever clears the live presence
    heartbeat so the dashboard doesn't wait out the full TTL to show them
    offline. It deliberately does NOT close the AgentLoginEvent row — that
    is reserved for the confirmed /logout endpoint below. Also clears any
    stale "away" flag so a fresh session next login doesn't inherit it."""
    agent_id = ctx.get("agent_id")
    if agent_id:
        await presence_service.clear_heartbeat(agent_id)
        await presence_service.set_away(agent_id, False)
        await _maybe_log_status(agent_id, "offline", force=True)
    return {"ok": True}


@router.post("/logout")
async def logout(key: str = Query(...), ctx: dict = Depends(get_admin_ctx)):
    """The ONLY confirmed, explicit logout — bound to the actual Sign Out
    button, called as a real awaited request (not a sendBeacon) since it's
    a deliberate user action, not a page-teardown race. Distinct from
    /offline in exactly the way that endpoint's docstring describes:  this
    is the one place allowed to close AgentLoginEvent.logout_at, and it
    also tears down the auth session itself (nothing else in this codebase
    ever did — a copied session token used to stay valid via get_admin_ctx's
    sliding TTL for up to 24h after clicking Sign Out)."""
    agent_id = ctx.get("agent_id")
    if agent_id:
        await presence_service.clear_heartbeat(agent_id)
        await presence_service.set_away(agent_id, False)
        await _maybe_log_status(agent_id, "offline", force=True)
        await run_in_threadpool(_close_open_login, agent_id)
    await invalidate_session(key)
    return {"ok": True, "session_closed": bool(agent_id)}


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
