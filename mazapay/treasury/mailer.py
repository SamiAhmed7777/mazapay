"""SMTP delivery for treasury sign-in links — stdlib only.

Configured entirely from the environment; when nothing is configured the
treasury keeps its previous behaviour (the link is logged with the token
redacted, and demo mode hands it back to the caller). Sending never raises into
the request path: a mail failure is audited and the caller still sees the same
`{"sent": True}` answer, because revealing delivery state would leak whether an
address exists.

Environment:

    MAZAPAY_TR_SMTP_HOST      SMTP server (empty = mail disabled)
    MAZAPAY_TR_SMTP_PORT      587 (STARTTLS) or 465 (implicit TLS)
    MAZAPAY_TR_SMTP_USER      login user (optional for a trusted relay)
    MAZAPAY_TR_SMTP_PASSWORD  login password
    MAZAPAY_TR_SMTP_STARTTLS  1 (default) — used when the port is not 465
    MAZAPAY_TR_SMTP_FROM      From: header (default no-reply@<hostname>)
    MAZAPAY_TR_SMTP_TIMEOUT   seconds (default 10)
"""
from __future__ import annotations

import email.message
import email.utils
import smtplib
import socket
import ssl

from . import settings

SUBJECT = "Your {org} sign-in link"


class MailError(Exception):
    """Raised only by send_magic_link's internal path; callers catch it."""


def configured() -> bool:
    return bool(settings.SMTP_HOST)


def _message(to_email: str, link: str, ttl_minutes: int) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    org = settings.ORG_NAME
    msg["Subject"] = SUBJECT.format(org=org)
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to_email
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="mazapay.local")
    msg.set_content(
        f"Hello,\n\n"
        f"Someone asked for a sign-in link to the {org} treasury workspace.\n\n"
        f"{link}\n\n"
        f"The link works once and expires in {ttl_minutes} minutes. If this was not\n"
        f"you, no action is needed and no one can sign in with it.\n\n"
        f"— {org} treasury\n"
    )
    return msg


def send_magic_link(to_email: str, link: str) -> None:
    """Send the link. Raises MailError (with a non-secret reason) on failure."""
    if not configured():
        raise MailError("smtp_not_configured")
    ttl_minutes = max(1, int(settings.MAGIC_TTL) // 60)
    msg = _message(to_email, link, ttl_minutes)
    host, port = settings.SMTP_HOST, settings.SMTP_PORT
    timeout = settings.SMTP_TIMEOUT
    try:
        if port == 465:
            client = smtplib.SMTP_SSL(
                host, port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        try:
            client.ehlo()
            if port != 465 and settings.SMTP_STARTTLS:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if settings.SMTP_USER:
                client.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            client.send_message(msg)
        finally:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                client.close()
    except (smtplib.SMTPException, OSError, socket.timeout) as exc:
        # Never echo the address or the link; the reason is a class name.
        raise MailError(type(exc).__name__) from None
