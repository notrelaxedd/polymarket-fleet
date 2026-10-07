"""Outbound senders run by the host for approved actions: `log` and `email`.

A sender gets the action row and the workload's host-only secrets; the workload's own
container never sees those secrets, which is how approval is enforced.
"""
from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlparse

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)
ADDRESS_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+$")
MAX_RECIPIENTS = 10
SMTP_TIMEOUT_S = 30


class Sender(Protocol):
    """send() performs the action and returns a small JSON-able result, or raises."""

    def send(self, action: dict[str, Any], host_secrets: dict[str, str]) -> dict[str, Any]: ...


class LogSender:
    """Record the approved action in the log and, when it has a job, in the job's events."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def send(self, action: dict[str, Any], host_secrets: dict[str, str]) -> dict[str, Any]:
        log.info("outbound log action %s from workload %s", action["id"], action["workload"])
        if action.get("job_id"):
            self.conn.execute(
                "INSERT INTO workload_job_events (job_id, machine_id, event, detail) VALUES (%s, %s, 'outbound_sent', %s)",
                (action["job_id"], action.get("machine_id"), Jsonb({"action": str(action["id"]), "kind": "log"})),
            )
        return {"logged": True}


def _recipients(payload: dict[str, Any]) -> list[str]:
    to = payload.get("to")
    items = [to] if isinstance(to, str) else to
    if not isinstance(items, list) or not items or len(items) > MAX_RECIPIENTS:
        raise ValueError(f"payload.to must be an address or a list of 1..{MAX_RECIPIENTS} addresses")
    if not all(isinstance(i, str) and ADDRESS_RE.match(i) for i in items):
        raise ValueError("payload.to holds an invalid address")
    return items


class EmailSender:
    """Send {to, subject, body} over SMTP using the host-only secrets SMTP_URL and EMAIL_FROM.

    SMTP_URL looks like smtps://user:pass@host:465 (implicit TLS) or smtp://user:pass@host:587
    (STARTTLS when the server offers it). The connection classes are injectable for tests.
    """

    def __init__(self, smtp_ssl: Callable[..., Any] = smtplib.SMTP_SSL, smtp: Callable[..., Any] = smtplib.SMTP) -> None:
        self.smtp_ssl = smtp_ssl
        self.smtp = smtp

    def send(self, action: dict[str, Any], host_secrets: dict[str, str]) -> dict[str, Any]:
        url, sender = host_secrets.get("SMTP_URL"), host_secrets.get("EMAIL_FROM")
        if not url or not sender:
            raise ValueError("host-only secrets SMTP_URL and EMAIL_FROM must both be set")
        payload = action["payload"]
        recipients = _recipients(payload)
        subject, body = payload.get("subject"), payload.get("body")
        if not isinstance(subject, str) or not isinstance(body, str) or "\n" in subject or "\r" in subject:
            raise ValueError("payload.subject (one line) and payload.body must be strings")
        if not ADDRESS_RE.match(sender):
            raise ValueError("EMAIL_FROM is not a valid address")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(recipients), subject
        msg.set_content(body)
        parsed = urlparse(url)
        if parsed.scheme not in ("smtp", "smtps") or not parsed.hostname:
            raise ValueError("SMTP_URL must look like smtps://user:pass@host:465")
        secure = parsed.scheme == "smtps"
        port = parsed.port or (465 if secure else 587)
        factory = self.smtp_ssl if secure else self.smtp
        with factory(parsed.hostname, port, timeout=SMTP_TIMEOUT_S) as server:
            if not secure:
                try:
                    server.starttls()
                except smtplib.SMTPNotSupportedError:
                    # Never send credentials in clear text (an on-path attacker can strip STARTTLS).
                    if parsed.username:
                        raise ValueError("the SMTP server offers no STARTTLS; refusing to log in without TLS") from None
            if parsed.username:
                server.login(unquote(parsed.username), unquote(parsed.password or ""))
            server.send_message(msg)
        return {"sent_to": recipients}


def default_senders(conn: psycopg.Connection) -> dict[str, Sender]:
    """The real senders, keyed by action kind."""
    return {"log": LogSender(conn), "email": EmailSender()}
