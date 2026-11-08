"""
Optional notifications (disabled by default).

Methods: "log" (writes to the SVPI log) or "email" (SMTP; password only from
the SVPI_SMTP_PASSWORD environment variable - never stored by SVPI).
Notifications only REQUEST an upload; SVPI never claims it can fetch MT5 data.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from modules.audit_logger import get_logger

log = get_logger("notify")


def send_notification(cfg, subject: str, body: str) -> str:
    n = cfg.notify
    if not n.enabled:
        log.info("Notification (disabled, logged only): %s", subject)
        return "DISABLED"
    if n.method == "email":
        pwd = os.environ.get("SVPI_SMTP_PASSWORD")
        if not (n.smtp_host and n.email_to and pwd):
            log.warning("Email notification not configured; logged instead: %s", subject)
            return "NOT_CONFIGURED"
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = n.smtp_user or n.email_to
        msg["To"] = n.email_to
        msg.set_content(body)
        try:
            with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=30) as s:
                s.starttls()
                if n.smtp_user:
                    s.login(n.smtp_user, pwd)
                s.send_message(msg)
            return "SENT"
        except Exception as exc:  # noqa: BLE001
            log.error("Email notification failed: %s", exc)
            return "FAILED"
    log.warning("NOTIFICATION: %s\n%s", subject, body)
    return "LOGGED"
