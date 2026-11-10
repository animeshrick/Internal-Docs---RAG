"""
logs.py - one place that decides where log messages go.

Rule used across this project:

  * Every module does  logger = logging.getLogger(__name__)  and logs its
    diagnostics (progress, retries, skipped files). It never adds a handler,
    so a module stays silent until an entry point asks for output.
  * Only entry points call setup_logging(): app/main.py and the __main__ block
    of each command. That keeps the decision about format and level in one place.
  * Diagnostics go to the logger (stderr). What the user actually asked to see,
    such as the answer or a score table, stays on print (stdout), so
    "python -m ... > report.txt" still captures the report and not the chatter.

Level comes from LOG_LEVEL in .env (default INFO), or pass one in.
"""
import logging
import os
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
TIME_FORMAT = "%H:%M:%S"
_ALREADY_SET_UP = False


def setup_logging(level=None, stream=sys.stderr):
    """Send log messages to stderr. Safe to call more than once; only the first call counts."""
    global _ALREADY_SET_UP
    if _ALREADY_SET_UP:
        return logging.getLogger("app")

    level_name = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, TIME_FORMAT))

    # Attach to the "app" logger, not the root one, so a library we import
    # cannot flood the output and uvicorn's own handlers stay untouched.
    #
    # "__main__" gets the same treatment: a module started with "python -m" runs
    # under the name __main__, so its own logging.getLogger(__name__) is NOT under
    # "app" and would otherwise miss this handler entirely.
    for logger_name in ("app", "__main__"):
        named_logger = logging.getLogger(logger_name)
        named_logger.setLevel(level_name)
        named_logger.addHandler(handler)
        named_logger.propagate = False

    _ALREADY_SET_UP = True
    return logging.getLogger("app")
