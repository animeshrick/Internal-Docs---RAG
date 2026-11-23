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
so a forced password change can never be locked out. The government ID routes are not under /me
for the same reason: they are /government-id, so marking the "me" page down can never stop an
employee from finishing that mandatory step either.

Unknown keys in the setting (a typo, for example) are silently dropped rather than rejected, so
a bad value can never accidentally take down more than intended.

The same endpoint also carries EMERGENCY_BROADCAST, a short message the website shows at the top
of every page. See get_emergency_broadcast at the bottom of this file.
"""
from dotenv import dotenv_values

from app.core.config import get_setting
from app.core.paths import PROJECT_ROOT

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


# ------------------------------------------------------------------ the emergency broadcast
# A short message shown at the top of every page of the website, login page included. It is read
# from EMERGENCY_BROADCAST in the .env file and sent out by GET /system/downtime, which the
# frontend already polls once a minute. So changing the text needs a restart of this server and
# no rebuild of the frontend, and it reaches people who are already logged in within the minute.
#
# Unset or empty means no banner. This is only a notice: it blocks nothing, unlike DOWNTIME_PAGES.
EMERGENCY_BROADCAST_SETTING_NAME = "EMERGENCY_BROADCAST"
MAXIMUM_BROADCAST_LENGTH = 300      # a banner, not an article: longer text is cut off


def read_broadcast_from_env_file():
    """
    The message as the .env file has it RIGHT NOW, or None when the file has no entry for it.

    The .env file is normally read once, when the server starts, so a change to it would need a
    restart to take effect. An emergency notice is the one setting where that is the wrong
    behaviour: it has to be possible to put a message up, or take it down, on a server that is
    already running and serving people. So this one value is read from the file on every call.

    That is cheap: the frontend polls GET /system/downtime once a minute per open page, and this
    reads one small file. dotenv_values parses the file without touching the process environment,
    so nothing else in the app is affected by what it finds.
    """
    env_file_path = PROJECT_ROOT / ".env"
    if not env_file_path.exists():
        return None
    return dotenv_values(env_file_path).get(EMERGENCY_BROADCAST_SETTING_NAME)


def get_emergency_broadcast():
    """
    The message to show at the top of every page, or None when there is nothing to say.

    The .env file wins when it has the setting, so editing that file changes the notice straight
    away. Otherwise the process environment is used, which is how it would be set on a host that
    passes settings in directly instead of using a file.
    """
    from_file = read_broadcast_from_env_file()
    raw_message = from_file if from_file is not None else get_setting(EMERGENCY_BROADCAST_SETTING_NAME)
    message = (raw_message or "").strip()
    return message[:MAXIMUM_BROADCAST_LENGTH] if message else None
