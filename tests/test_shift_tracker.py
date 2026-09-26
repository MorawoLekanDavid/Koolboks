"""Regression tests for chatbot.routers.analytics's shift-tracker helpers.

Written after two real incidents on the same feature:

1. Agents reported still being online while the Shift Tracker's Login &
   Device Audit showed an earlier logout time. Root cause was architectural,
   not in classify_session_status() -- /admin/presence/offline (a sendBeacon
   fired on ANY page exit: refresh, tab close, crash, network loss) was
   closing AgentLoginEvent.logout_at, so a flaky wifi moment looked identical
   to a real end-of-shift click on Sign Out. Fixed by moving logout_at-
   closing to a new, confirmed-only endpoint (POST /admin/presence/logout).

2. After (1) was fixed, an agent with three same-day logins (two of them
   hours-old and long since superseded by a newer one) showed all three as
   "Active" simultaneously. Root cause: the audit loop used the agent's
   single most-recent heartbeat of the WHOLE DAY for every one of their
   login rows, instead of scoping each row to its own session window. Fixed
   by last_heartbeat_in_session().

Both functions are the read side of those fixes -- what turns raw
login/heartbeat rows into the "Active"/"Ended"/"Dropped" label the audit
table shows -- so both get direct coverage independent of the DB/Redis-
backed endpoint that calls them.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from chatbot.routers.analytics import classify_session_status, last_heartbeat_in_session
from chatbot.services.presence_service import HEARTBEAT_TTL

NOW = datetime(2026, 1, 1, 12, 0, 0)


@dataclass
class _Heartbeat:
    """Minimal stand-in for an AgentHeartbeatLog row -- last_heartbeat_in_session
    only ever reads .logged_at and (via the caller) .status, so a real ORM
    instance and DB session aren't needed to test it."""
    status: str
    logged_at: datetime


def test_explicit_logout_is_ended_even_with_a_stale_heartbeat():
    """logout_at set (only ever written by the confirmed /logout endpoint)
    means "Ended" regardless of what the heartbeat trail looks like -- a
    real logout still counts even if the tab had been idle for a while first."""
    stale_heartbeat = NOW - timedelta(hours=2)
    assert classify_session_status(NOW, "offline", stale_heartbeat, NOW) == "Ended"
    assert classify_session_status(NOW, None, None, NOW) == "Ended"


def test_no_logout_but_fresh_heartbeat_is_active():
    """Scenario 2/3 from the spec: refresh or a merely-quiet tab, heartbeat
    still landing recently and not offline -- still genuinely online, no
    logout_at, must read as Active, never as a logout."""
    fresh = NOW - timedelta(seconds=30)
    assert classify_session_status(None, "online", fresh, NOW) == "Active"
    assert classify_session_status(None, "away", fresh, NOW) == "Active"


def test_no_logout_and_heartbeat_gone_stale_is_dropped_not_ended():
    """Scenario 3/4 from the spec: tab closed or network died, no explicit
    logout ever happened, heartbeat has aged past HEARTBEAT_TTL+60s grace --
    this is exactly the case that used to be misreported as a logout. Must
    be "Dropped", and specifically must NOT be "Ended"."""
    long_gone = NOW - timedelta(seconds=HEARTBEAT_TTL + 61)
    result = classify_session_status(None, "online", long_gone, NOW)
    assert result == "Dropped"
    assert result != "Ended"


def test_no_logout_and_last_status_offline_is_dropped():
    """The heartbeat's own last recorded status was already "offline" (the
    presence-cleared-by-page-exit case) but no confirmed logout followed --
    still Dropped, not Ended."""
    recent = NOW - timedelta(seconds=5)
    assert classify_session_status(None, "offline", recent, NOW) == "Dropped"


def test_no_logout_and_no_heartbeat_history_at_all_is_dropped():
    """An agent with a login row but zero heartbeat rows for the day (e.g.
    logged in and the tab died before the first heartbeat fired) must not
    crash and must read as Dropped, not Active or Ended."""
    assert classify_session_status(None, None, None, NOW) == "Dropped"


def test_boundary_just_inside_and_just_outside_the_grace_window():
    """HEARTBEAT_TTL + 60s is the exact cutoff used in production — pin the
    boundary so a future refactor can't silently shift it."""
    just_inside = NOW - timedelta(seconds=HEARTBEAT_TTL + 59)
    just_outside = NOW - timedelta(seconds=HEARTBEAT_TTL + 61)
    assert classify_session_status(None, "online", just_inside, NOW) == "Active"
    assert classify_session_status(None, "online", just_outside, NOW) == "Dropped"


# ── last_heartbeat_in_session — one agent, several logins the same day ──────

def test_older_session_does_not_borrow_a_newer_sessions_heartbeat():
    """The exact incident: login A at 08:00 (long abandoned), login B at
    12:00 (genuinely active now, heartbeat at 12:01). A must NOT see B's
    12:01 heartbeat as its own — that's what made both read "Active"."""
    login_a = datetime(2026, 1, 1, 8, 0, 0)
    login_b = datetime(2026, 1, 1, 12, 0, 0)
    heartbeats = [
        _Heartbeat("online", datetime(2026, 1, 1, 8, 1, 0)),   # belongs to A
        _Heartbeat("online", datetime(2026, 1, 1, 12, 1, 0)),  # belongs to B
    ]
    hb_a = last_heartbeat_in_session(heartbeats, login_a, next_login_at=login_b)
    hb_b = last_heartbeat_in_session(heartbeats, login_b, next_login_at=None)
    assert hb_a is not None and hb_a.logged_at == datetime(2026, 1, 1, 8, 1, 0)
    assert hb_b is not None and hb_b.logged_at == datetime(2026, 1, 1, 12, 1, 0)


def test_last_login_of_the_day_has_no_upper_bound():
    """next_login_at=None (the agent's most recent login that day) means
    every later heartbeat can belong to it — there's nothing to cap it at."""
    login_at = datetime(2026, 1, 1, 9, 0, 0)
    heartbeats = [
        _Heartbeat("online", datetime(2026, 1, 1, 9, 1, 0)),
        _Heartbeat("online", datetime(2026, 1, 1, 17, 0, 0)),
    ]
    hb = last_heartbeat_in_session(heartbeats, login_at, next_login_at=None)
    assert hb.logged_at == datetime(2026, 1, 1, 17, 0, 0)


def test_no_heartbeats_in_window_returns_none():
    """A session with genuinely nothing recorded in its own window (died
    before the first heartbeat, or the only heartbeats belong to a
    different session) must return None, not the wrong row."""
    login_a = datetime(2026, 1, 1, 8, 0, 0)
    login_b = datetime(2026, 1, 1, 9, 0, 0)
    heartbeats = [_Heartbeat("online", datetime(2026, 1, 1, 9, 30, 0))]  # only belongs to B
    assert last_heartbeat_in_session(heartbeats, login_a, next_login_at=login_b) is None
