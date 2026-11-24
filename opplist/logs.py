r"""Rotating log file: data\opplist.log, 5 MB x 3.

Errors that reach the user as a message still belong in here with a
traceback, because the message on screen is deliberately short.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import tempfile
from pathlib import Path

from . import paths

LOGGER_NAME = "opplist"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

_configured = False


def setup(path=None, level=logging.INFO) -> logging.Logger:
    """Attach the rotating handler once per process."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger
    logger.setLevel(level)
    target = Path(path or paths.LOG_PATH)
    if path is None and "pytest" in sys.modules and _is_live(target):
        # a test never writes the live log, whatever it forgot to patch (13c)
        target = Path(tempfile.gettempdir()) / "opplist-test-logs" / "opplist.log"
    _attach(logger, target)
    _configured = True
    return logger


def _is_live(target: Path) -> bool:
    try:
        return target.resolve() == Path(paths.LIVE_LOG_PATH).resolve()
    except OSError:
        return False


def _attach(logger, target: Path) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        logger.addHandler(handler)
    except OSError:
        # A log we cannot write is not a reason to refuse to start.
        logger.addHandler(logging.NullHandler())


def redirect(path) -> None:
    """Write the log to `path` from now on (13c, 2026-09-30): an app or a
    probe server over a copy of the database logs next to that copy, never
    into the live data\\opplist.log."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    target = Path(path)
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            if Path(handler.baseFilename).resolve() == target.resolve():
                return
            logger.removeHandler(handler)
            handler.close()
        elif isinstance(handler, logging.NullHandler):
            logger.removeHandler(handler)
    logger.setLevel(logging.INFO)
    _attach(logger, target)
    _configured = True


def current_path():
    """The file the log is written to now, or None."""
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        if isinstance(handler, logging.FileHandler):
            return Path(handler.baseFilename)
    return None


def get(name: str = "") -> logging.Logger:
    setup()
    return logging.getLogger(
        "{}.{}".format(LOGGER_NAME, name) if name else LOGGER_NAME)
