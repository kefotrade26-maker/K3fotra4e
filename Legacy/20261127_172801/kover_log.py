"""
kover_log.py — Unified logging for Kover Bridge.

Usage:
    from kover_log import get_logger
    log = get_logger("multi_runner")
    log.info("Connected")
    log.warning("Reconnecting…")
    log.error("Auth failed")

Control log level via environment variable:
    export KOVER_LOG_LEVEL=DEBUG   # DEBUG|INFO|WARNING|ERROR

Default: INFO
"""
import logging
import os
import sys

_LEVEL = os.environ.get("KOVER_LOG_LEVEL", "INFO").upper()
_FORMAT = "%(asctime)s │ %(levelname)-7s │ %(name)-14s │ %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False


def _configure_once():
    global _configured
    if _configured:
        return
    _configured = True

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))

    root = logging.getLogger("kover")
    root.setLevel(getattr(logging, _LEVEL, logging.INFO))
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the 'kover' namespace."""
    _configure_once()
    return logging.getLogger(f"kover.{name}")
