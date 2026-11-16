"""
core/presence.py - a lightweight, best-effort "is this person using the app right now" signal,
for the green online dot in 1-to-1 messaging (app/hr/messages/).

There is no session table and no websockets anywhere in this app (see app/identity/auth.py -
login is a plain stateless JWT, checked fresh on every request). "Online" here is an
approximation, not a real presence system:
    - Every authenticated request marks that employee active (see get_current_employee() in
      app/main.py, the one chokepoint nearly every endpoint depends on).
    - The frontend's sidebar badge (src/components/Layout.tsx) already polls
      GET /messages/conversations every 20 seconds on EVERY page, so as long as someone's tab is
      open, their activity refreshes on its own without any dedicated "heartbeat" endpoint.
    - is_online() just checks whether we heard from them within ONLINE_WINDOW_SECONDS.

This is deliberately in-memory, not a database table:
    - It resets on every restart/deploy. Someone still using the app marks themselves active
      again within ONLINE_WINDOW_SECONDS of their very next request, so this self-heals.
    - It only gives a correct picture with ONE backend worker process. If this is ever deployed
      with more than one Render worker/replica, each process holds its own partial view and the
      dot could be wrong for some requests. Fine for this app's current single-worker setup -
      revisit (e.g. move to Redis or the database) if that ever changes.
"""
from datetime import datetime, timezone

ONLINE_WINDOW_SECONDS = 90

_last_seen_at: dict[int, datetime] = {}


def mark_active(employee_id):
    _last_seen_at[employee_id] = datetime.now(timezone.utc)


def is_online(employee_id):
    seen_at = _last_seen_at.get(employee_id)
    if seen_at is None:
        return False
    return (datetime.now(timezone.utc) - seen_at).total_seconds() <= ONLINE_WINDOW_SECONDS


def _reset_for_tests():
    """Only for tests - so one test's "online" employee doesn't leak into the next."""
    _last_seen_at.clear()
