"""Rotating diagnostic log in %LOCALAPPDATA%\\AHelperWForever\\logs and crash capture.

A windowed build has no console, so unhandled exceptions (main thread, worker threads and
Tk callbacks) are written here; the file is what a user sends when reporting a problem.
"""

import logging
import platform
import sys
import threading
from logging.handlers import RotatingFileHandler

from . import __version__
from .paths import logs_dir

log = logging.getLogger('ahelperw_fishing')


def setup(level=logging.INFO):
    """Configure file logging once; returns the log file path (None if it cannot be created)."""
    if log.handlers:
        return getattr(log, 'file', None)
    log.setLevel(level)
    path = None
    try:
        directory = logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'fishing.log'
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(threadName)s %(message)s'))
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())
    log.file = path
    log.info(
        'start %s, Python %s, %s %s',
        __version__,
        platform.python_version(),
        platform.system(),
        platform.version(),
    )
    sys.excepthook = _excepthook
    threading.excepthook = lambda args: log.error(
        'unhandled exception in thread %s',
        args.thread.name if args.thread else '?',
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    return path


def _excepthook(exc_type, exc, tb):
    log.critical('unhandled exception', exc_info=(exc_type, exc, tb))
    sys.__excepthook__(exc_type, exc, tb)
