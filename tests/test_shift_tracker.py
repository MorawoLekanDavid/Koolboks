"""Regression tests for chatbot.routers.analytics.classify_session_status.

Written after a real incident: agents reported still being online while the
Shift Tracker's Login & Device Audit showed an earlier logout time. Root
cause was architectural, not in this function -- /admin/presence/offline (a
sendBeacon fired on ANY page exit: refresh, tab close, crash, network loss)
was closing AgentLoginEvent.logout_at, so a flaky wifi moment looked
identical to a real end-of-shift click on Sign Out.

The fix moved logout_at-closing to a new, confirmed-only endpoint
(POST /admin/presence/logout). This function is the read side of that fix --
it's what turns (logout_at, last heartbeat) into the "Active"/"Ended"/
"Dropped" label the audit table shows, and it's what actually needs to get
the distinction right, so it gets direct coverage independent of the
DB/Redis-backed endpoint that calls it.
"""
from datetime import datetime, timedelta

from chatbot.routers.analytics import classify_session_status
from chatbot.services.presence_service import HEARTBEAT_TTL

NOW = datetime(2026, 1, 1, 12, 0, 0)


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
