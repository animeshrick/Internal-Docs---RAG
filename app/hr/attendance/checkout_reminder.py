"""
hr/attendance/checkout_reminder.py - starts the daily 7:30pm "do you want to check out?" chat prompt.

Same shape as hr/reports/scheduler.py: one BackgroundScheduler, started once on app startup (see
app/main.py) and stopped on shutdown. Set DISABLE_CHECKOUT_REMINDER_SCHEDULER=1 in the environment
to turn this off entirely.

What the job does, every day at 7:30pm India time:
    1. Find everyone who checked in today and has not checked out yet (attendance_service.find_checkout_candidates).
       Someone who already checked out ("already logged out") is skipped - nothing is shown to them.
    2. For each of them, if they are not already in the middle of something else in the chat (an apply_leave
       or cancel_leave waiting for "yes"), save a pending_action of type checkout_prompt and a matching
       chat message asking the question.
    3. The employee can then reply "yes" (checks them out, right there in the chat - see
       rag/answer/assistant.py's handle_confirmation_words) or "no" (does nothing), or leave it unanswered:
       the prompt is not urgent, so it is given until the end of the day to expire, not the usual 10 minutes.
"""
import logging
from datetime import datetime, timedelta

from app.core.config import get_setting
from app.hr.leave.service import COMPANY_TIME_ZONE

logger = logging.getLogger(__name__)

REMINDER_HOUR = 19
REMINDER_MINUTE = 30

CHECKOUT_PROMPT_TEXT = ("It's 7:30pm. Do you want to check out now?\nReply yes to check out, or no to stay checked in.")

_scheduler = None


def end_of_day_expiry(today):
    """Midnight at the start of tomorrow, India time - so the prompt stays answerable for the rest of today
    rather than the usual 10 minutes (see leave/store.py's create_pending_action)."""
    midnight_local = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=COMPANY_TIME_ZONE)
    return midnight_local.isoformat()


def run_checkout_reminder_job():
    """What each scheduled firing does. A separate function (not a lambda) so a test can call it directly."""
    # Imported here, not at module load, to avoid a circular import (same reason as reports/scheduler.py).
    from app.chat import store as chat_store
    from app.hr.attendance import service as attendance_service
    from app.hr.leave import store as leave_store

    today = attendance_service.get_today()
    candidates = attendance_service.find_checkout_candidates(today)
    asked, skipped_busy = 0, 0
    for person in candidates:
        # Don't step on something else already waiting for "yes" from this person (for example a leave
        # they are in the middle of applying for) - the reminder is not important enough to cancel that.
        if leave_store.get_waiting_action(person["id"]) is not None:
            skipped_busy += 1
            continue
        leave_store.create_pending_action(
            person["id"], attendance_service.CHECKOUT_PROMPT_ACTION, {"work_date": today.isoformat()},
            "Do you want to check out now?", expires_at=end_of_day_expiry(today))
        try:
            chat_store.save_assistant_message(person["id"], CHECKOUT_PROMPT_TEXT, attendance_service.get_now())
        except Exception:
            logger.exception("Could not save the checkout reminder chat message for employee %s", person["emp_id"])
        asked += 1
    logger.info("Checkout reminder: asked %d of %d checked-in-and-not-out people (%d skipped, already mid-chat).",
                asked, len(candidates), skipped_busy)


def start():
    """Idempotent: calling this twice (for example under --reload) does not add the job twice."""
    global _scheduler
    if get_setting("DISABLE_CHECKOUT_REMINDER_SCHEDULER"):
        logger.info("Checkout reminder scheduler disabled (DISABLE_CHECKOUT_REMINDER_SCHEDULER is set).")
        return
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone=COMPANY_TIME_ZONE)
    scheduler.add_job(run_checkout_reminder_job, CronTrigger(hour=REMINDER_HOUR, minute=REMINDER_MINUTE),
                      id="checkout_reminder", replace_existing=True)
    scheduler.start()
    _scheduler = scheduler
    logger.info("Checkout reminder scheduler started (%02d:%02d).", REMINDER_HOUR, REMINDER_MINUTE)


def stop():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
