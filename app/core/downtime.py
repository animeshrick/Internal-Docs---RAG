"""
core/downtime.py - per-page planned maintenance, read fresh from the environment on every request.

Set DOWNTIME_PAGES in the .env file (locally) or the Render environment (in production) to a
comma-separated list of page keys to take those pages down everywhere - frontend and backend -
without a deploy. Example:

    DOWNTIME_PAGES=payslips,admin

A request to a blocked page's backend routes gets a plain 503 instead of reaching the route at
all (see the middleware in app/main.py). GET /system/downtime (public, no login needed) reports
the current list so the frontend can show a maintenance screen on the matching pages instead of
waiting for every request on them to fail one by one.

Some frontend pages share the exact same backend routes and so share one key here:
    leave_and_wfh   - the Leave page and the WFH page (both call /leave/* and /pending-actions/*)
    attendance      - the Attendance page and the Attendance-team page (both call /attendance/*)
A few pages have no backend routes of their own to gate (Home, and Approvals, which only calls
the leave/attendance routes above); their key only hides the frontend page, with nothing
enforced on this side. Change password has no key at all - it is never gated, on either side,
so a forced password change can never be locked out.

Unknown keys in the setting (a typo, for example) are silently dropped rather than rejected, so
a bad value can never accidentally take down more than intended.
"""
from app.core.config import get_setting

PAGE_KEYS = {
    "home", "chat", "documents", "leave_and_wfh", "payslips", "approvals",
    "attendance", "me", "admin", "superadmin", "messages",
}

# The first matching prefix wins, so the more specific ones are listed first.
_PAGE_KEY_BY_PATH_PREFIX = [
    ("/payroll", "payslips"),
    ("/pending-actions", "leave_and_wfh"),
    ("/leave", "leave_and_wfh"),
    ("/attendance", "attendance"),
    ("/celebrations", "home"),
    ("/documents", "documents"),
    ("/register", "admin"),
    ("/reset-password", "admin"),
    ("/admin", "admin"),
    ("/superadmin", "superadmin"),
    ("/chat", "chat"),
    ("/messages", "messages"),
    ("/me", "me"),
]

DOWNTIME_MESSAGE = "This page is down for planned maintenance right now. Please check back shortly."


def page_key_for_path(path):
    """Which downtime page key (if any) a request path belongs to, or None if it is never
    gated - /health, /login, /change-password, and /system/downtime itself always go through."""
    for prefix, page_key in _PAGE_KEY_BY_PATH_PREFIX:
        if path == prefix or path.startswith(prefix + "/"):
            return page_key
    return None


def get_down_pages():
    """The set of page keys currently marked down. Read fresh every call, with no caching, so a
    changed DOWNTIME_PAGES setting takes effect right away on platforms that reload env vars
    without a full restart."""
    raw = get_setting("DOWNTIME_PAGES", "") or ""
    keys = {item.strip() for item in raw.split(",") if item.strip()}
    return keys & PAGE_KEYS
