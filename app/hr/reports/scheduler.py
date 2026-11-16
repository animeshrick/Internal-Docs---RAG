"""
hr/reports/scheduler.py - starts the twice-daily WhatsApp HR report job (11am and 5pm India time).

Runs inside the FastAPI process itself - no separate cron infrastructure. One BackgroundScheduler,
started once on app startup (see app/main.py) and stopped on shutdown. Set
DISABLE_WHATSAPP_REPORT_SCHEDULER=1 in the environment to turn this off entirely (for example for a
one-off script that imports app.main, or if the report ever moves to an external scheduler instead).
"""
import logging

from app.core.config import get_setting
from app.hr.leave.service import COMPANY_TIME_ZONE

logger = logging.getLogger(__name__)

REPORT_HOURS = (11, 17)   # 11am and 5pm, India time

_scheduler = None


def run_report_job():
    """What each scheduled firing does. A separate function (not a lambda) so a test can call it directly."""
    from app.hr.reports import service as reports_service       # imported here, not at module load, to avoid a circular import
    results = reports_service.send_daily_report()
    failed = [result for result in results if not result["ok"]]
    if failed:
        logger.error("WhatsApp HR report: %d of %d number(s) failed: %s", len(failed), len(results), failed)
    elif results:
        logger.info("WhatsApp HR report sent to %d number(s).", len(results))
    else:
        logger.info("WhatsApp HR report: no numbers are configured, nothing sent.")


def start():
    """Idempotent: calling this twice (for example under --reload) does not add the job twice."""
    global _scheduler
    if get_setting("DISABLE_WHATSAPP_REPORT_SCHEDULER"):
        logger.info("WhatsApp HR report scheduler disabled (DISABLE_WHATSAPP_REPORT_SCHEDULER is set).")
        return
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone=COMPANY_TIME_ZONE)
    for hour in REPORT_HOURS:
        scheduler.add_job(run_report_job, CronTrigger(hour=hour, minute=0), id=f"whatsapp_report_{hour}", replace_existing=True)
    scheduler.start()
    _scheduler = scheduler
    logger.info("WhatsApp HR report scheduler started (%s).", ", ".join(f"{hour}:00" for hour in REPORT_HOURS))


def stop():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
