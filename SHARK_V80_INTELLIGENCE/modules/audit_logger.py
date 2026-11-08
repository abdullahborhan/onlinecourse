"""
Audit logging: a rotating file log plus a durable audit trail in SQLite.

Every import, analysis run, report generation, configuration change and
destructive operation is recorded so that any number in a report can be traced
back to the files it came from.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler

import config

_LOGGER_NAME = "svpi"


def get_logger(name: str = "") -> logging.Logger:
    root = logging.getLogger(_LOGGER_NAME)
    if not root.handlers:
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
        try:
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(config.LOG_DIR / f"svpi_{config.ENVIRONMENT}.log",
                                     maxBytes=5_000_000, backupCount=5, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:  # pragma: no cover - read-only FS
            pass
        sh = logging.StreamHandler()
        sh.setLevel(logging.WARNING)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    return root.getChild(name) if name else root


def audit(action: str, detail: dict | str | None = None, actor: str = "system",
          db=None) -> None:
    """Write an audit event to the log file and, if available, the database."""
    log = get_logger("audit")
    payload = detail if isinstance(detail, str) else json.dumps(detail or {}, default=str)
    log.info("%s | %s | %s", actor, action, payload)
    if db is not None:
        try:
            db.add_audit_event(action=action, detail=payload, actor=actor)
        except Exception as exc:  # pragma: no cover - never let audit break the app
            log.error("Failed to persist audit event %s: %s", action, exc)
