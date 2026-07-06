"""Agent 6: DevOps — Structured logging.

Replaces scattered print() calls with proper structured logging.
Supports:
- JSON format for production (parseable by log aggregators)
- Pretty format for development
- Log levels
- Context injection (request_id, user, etc.)
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional


LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "FATAL": 50}
_LEVEL = LEVELS.get(os.environ.get("LOG_LEVEL", "INFO").upper(), 20)
_FORMAT = os.environ.get("LOG_FORMAT", "pretty")  # "json" or "pretty"


def _emit(level: str, message: str, **kwargs):
    if LEVELS.get(level, 0) < _LEVEL:
        return
    
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "msg": message,
    }
    entry.update(kwargs)
    
    if _FORMAT == "json":
        print(json.dumps(entry, default=str), file=sys.stderr, flush=True)
    else:
        # Pretty format for dev
        colors = {
            "DEBUG": "\033[36m",  # cyan
            "INFO": "\033[32m",   # green
            "WARN": "\033[33m",   # yellow
            "ERROR": "\033[31m",  # red
            "FATAL": "\033[35m",  # magenta
        }
        reset = "\033[0m"
        c = colors.get(level, "")
        ts = datetime.now().strftime("%H:%M:%S")
        extra = ""
        if kwargs:
            extra = " " + " ".join(f"{k}={v}" for k, v in kwargs.items())
        print(f"{c}[{ts}] {level:5s}{reset} {message}{extra}", file=sys.stderr, flush=True)


def debug(msg: str, **kw): _emit("DEBUG", msg, **kw)
def info(msg: str, **kw): _emit("INFO", msg, **kw)
def warn(msg: str, **kw): _emit("WARN", msg, **kw)
def error(msg: str, **kw): _emit("ERROR", msg, **kw)
def fatal(msg: str, **kw): _emit("FATAL", msg, **kw)


def request_log(method: str, path: str, status: int, duration_ms: float, **kw):
    """Log an HTTP request."""
    level = "INFO" if status < 400 else "WARN" if status < 500 else "ERROR"
    _emit(level, f"{method} {path} {status} {duration_ms:.0f}ms", **kw)


def ai_call(provider: str, model: str, duration_ms: float, success: bool, **kw):
    """Log an AI API call."""
    level = "INFO" if success else "ERROR"
    _emit(level, f"AI call: {provider}/{model} {'OK' if success else 'FAIL'} {duration_ms:.0f}ms", **kw)
