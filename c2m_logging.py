#!/usr/bin/env python

"""Logging setup and process exit-reason handling for can2mqtt."""

from __future__ import annotations

import atexit
import logging
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


def setup_logging(
    bus_name: str,
    log_level: str = "INFO",
    log_format: Optional[str] = None,
    log_dir: Optional[Path] = None,
    log_verbose: bool = False,
) -> None:
    """
    Configure logging for the entire application.
    Logs are written to a log file in ~/log/can2mqtt by default, and optionally
    to stderr.

    Args:
        bus_name: CAN bus name, used in the log file name.
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format: Custom log format string. If None, uses a default format.
        log_dir: Directory for log files. If None, uses ~/log/can2mqtt.
        log_verbose: If True, also log to stderr.
    """
    if log_format is None:
        log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Convert string level to logging constant
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    # Create log directory if it doesn't exist (default: ~/log/can2mqtt)
    if log_dir is None:
        log_dir = Path.home() / "log" / "can2mqtt"
    else:
        log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create log filename with timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = log_dir / f"{timestamp}_c2m_{bus_name}.log"

    # Configure root logger with both file and console handlers
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()

    # Create formatter
    formatter = logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    # Console handler (stderr)
    if log_verbose:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Reduce verbosity of third-party libraries if needed
    logging.getLogger("paho").setLevel(logging.WARNING)

    # Log the log file location
    root_logger.info("Logging to file: %s", log_file.absolute())


def flush_log_handlers() -> None:
    """Flush all logging handlers to ensure buffered output is written."""
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


def install_exit_reason_handlers(logger: logging.Logger) -> None:
    """
    Install handlers to log exit reasons when the process terminates.
    Covers: SIGTERM/SIGHUP (so finally runs), unhandled exceptions,
    and atexit as a last-resort flush. Note: SIGKILL and segfaults cannot be caught.
    """
    _original_excepthook = sys.excepthook
    _original_thread_excepthook = getattr(threading, "excepthook", None)

    def _excepthook(exc_type, exc_value, exc_tb):
        logger.critical(
            "Unhandled exception in main thread: %s: %s",
            exc_type.__name__ if exc_type else "?", exc_value,
            exc_info=(exc_type, exc_value, exc_tb),
        )
        flush_log_handlers()
        _original_excepthook(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args):
        # Python 3.13+ uses exc_traceback; older versions use exc_tb
        exc_tb = getattr(args, "exc_traceback", getattr(args, "exc_tb", None))
        thread_name = getattr(args.thread, "name", None) or getattr(args.thread, "ident", "?") if args.thread else "?"
        logger.critical(
            "Unhandled exception in thread %s: %s: %s",
            thread_name,
            args.exc_type.__name__ if args.exc_type else "?",
            args.exc_value,
            exc_info=(args.exc_type, args.exc_value, exc_tb),
        )
        flush_log_handlers()
        if _original_thread_excepthook is not None:
            _original_thread_excepthook(args)

    def _signal_handler(signum: int, frame):
        logger.warning("Received signal %d, initiating shutdown", signum)
        flush_log_handlers()
        raise SystemExit(128 + signum)

    def _atexit_handler():
        logger.info("Process exiting (atexit)")
        flush_log_handlers()

    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook
    signal.signal(signal.SIGTERM, _signal_handler)
    atexit.register(_atexit_handler)
