"""SMTP delivery for treasury sign-in links.

Covers the three paths that matter: nothing configured (log only), delivery over
a fake SMTP server (content + audit), and a transport failure (same answer to the
caller, an audit row, no exception). The token must never reach a log line.
"""
import logging
import unittest
import uuid
from unittest import mock

try:
    import _sandbox as _env
except ImportError:
    from tests import _sandbox as _env  # type: ignore

from mazapay.treasury import auth, mailer, settings  # noqa: E402
from mazapay.treasury.service import TreasuryError  # noqa: E402

requires_writable = _env.requires_writable


class FakeSMTP:
    """Minimal smtplib.SMTP stand-in that records what was sent."""

    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.called = []
        self.messages = []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.called.append("ehlo")

    def starttls(self, context=None):
        self.called.append("starttls")

    def login(self, user, password):
        self.called.append(f"login:{user}")

    def send_message(self, msg):
        self.called.append("send")
        self.messages.append(msg)

    def quit(self):
        self.called.append("quit")

    def close(self):
        self.called.append("close")


class FailingSMTP(FakeSMTP):
    def send_message(self, msg):
        import smtplib

        raise smtplib.SMTPServerDisconnected("relay down")


@requires_writable
class TestMagicLinkMail(unittest.TestCase):
    def setUp(self):
        auth.init_db()
        settings.DEMO_MODE = False
        self._saved = {
            k: getattr(settings, k)
            for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                      "SMTP_STARTTLS", "SMTP_FROM", "MAGIC_TTL", "ORG_NAME", "DEMO_MODE")
        }
        settings.SMTP_HOST = "smtp.example.test"
        settings.SMTP_PORT = 587
        settings.SMTP_USER = "treasury"
        settings.SMTP_PASSWORD = "hunter2"
        settings.SMTP_STARTTLS = True
        settings.SMTP_FROM = "Treasury <no-reply@example.test>"
        settings.ORG_NAME = "Sample Tribal Nation"
        FakeSMTP.instances.clear()

    def tearDown(self):
        settings.__dict__.update(self._saved)
        auth.reset_rate_limits()

    def user(self):
        return auth.bootstrap_admin(f"mail-{uuid.uuid4().hex[:8]}@example.test", "Mail Tester")

    def actions(self, user_id):
        with __import__("mazapay.treasury.store", fromlist=["x"]).db() as conn:
            rows = conn.execute(
                "SELECT action FROM tr_audit WHERE object_id=? ORDER BY id", (user_id,)
            ).fetchall()
        return [r["action"] for r in rows]

    def test_delivers_the_link_over_smtp(self):
        user = self.user()
        with mock.patch.object(mailer.smtplib, "SMTP", FakeSMTP):
            result = auth.request_magic_link(user["email"], "https://treasury.example.test")
        self.assertEqual(result, {"sent": True}, "production mode never returns the link")
        client = FakeSMTP.instances[-1]
        self.assertIn("ehlo", client.called)
        self.assertIn("starttls", client.called)
        self.assertIn("login:treasury", client.called)
        self.assertEqual(len(client.messages), 1)
        msg = client.messages[0]
        body = msg.get_content()
        self.assertIn("/treasury/auth/", body)
        self.assertIn("Sample Tribal Nation", msg["Subject"])
        self.assertEqual(msg["To"], user["email"])
        self.assertIn("expires in", body)
        self.assertIn("login_emailed", self.actions(user["id"]))

    def test_transport_failure_is_audited_not_raised(self):
        user = self.user()
        with mock.patch.object(mailer.smtplib, "SMTP", FailingSMTP):
            result = auth.request_magic_link(user["email"], "https://treasury.example.test")
        self.assertEqual(result, {"sent": True})
        self.assertIn("login_email_failed", self.actions(user["id"]))

    def test_unknown_address_sends_nothing_but_answers_identically(self):
        with mock.patch.object(mailer.smtplib, "SMTP", FakeSMTP):
            result = auth.request_magic_link(
                f"nobody-{uuid.uuid4().hex[:6]}@example.test", "https://treasury.example.test"
            )
        self.assertEqual(result, {"sent": True})
        self.assertEqual(FakeSMTP.instances, [], "no SMTP session for an unknown address")

    def test_without_smtp_config_the_link_is_logged_not_mailed(self):
        settings.SMTP_HOST = ""
        user = self.user()
        with self.assertLogs("mazapay.treasury.auth", level="INFO") as captured:
            result = auth.request_magic_link(user["email"], "https://treasury.example.test")
        self.assertEqual(result, {"sent": True})
        self.assertIn("login_link_logged", self.actions(user["id"]))
        joined = "\n".join(captured.output)
        self.assertIn("/treasury/auth/", joined)
        # The raw token is redacted in the log line.
        token = joined.split("/treasury/auth/")[1].split()[0]
        self.assertIn("*", token)

    def test_message_has_no_secret_beyond_the_link(self):
        user = self.user()
        with mock.patch.object(mailer.smtplib, "SMTP", FakeSMTP):
            auth.request_magic_link(user["email"], "https://treasury.example.test")
        raw = FakeSMTP.instances[-1].messages[0].as_string()
        self.assertNotIn("hunter2", raw, "SMTP password must never appear in the mail")
        self.assertEqual(raw.count("/treasury/auth/"), 1)


@requires_writable
class TestSmtpVariants(unittest.TestCase):
    """Port 465 (implicit TLS) and a relay that needs no login."""

    def setUp(self):
        auth.init_db()
        self._saved = {
            k: getattr(settings, k)
            for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                      "SMTP_STARTTLS", "SMTP_FROM", "MAGIC_TTL", "ORG_NAME", "DEMO_MODE")
        }
        settings.DEMO_MODE = False
        settings.SMTP_HOST = "smtp.example.test"
        settings.SMTP_USER = ""
        settings.SMTP_PASSWORD = ""
        settings.SMTP_STARTTLS = True
        settings.SMTP_FROM = "Treasury <no-reply@example.test>"
        FakeSMTP.instances.clear()

    def tearDown(self):
        settings.__dict__.update(self._saved)
        auth.reset_rate_limits()

    def test_implicit_tls_on_465_uses_smtp_ssl_without_starttls(self):
        settings.SMTP_PORT = 465
        user = auth.bootstrap_admin(f"tls-{uuid.uuid4().hex[:8]}@example.test", "TLS User")
        with mock.patch.object(mailer.smtplib, "SMTP_SSL", FakeSMTP):
            result = auth.request_magic_link(user["email"], "https://treasury.example.test")
        self.assertEqual(result, {"sent": True})
        client = FakeSMTP.instances[-1]
        self.assertEqual(client.port, 465)
        self.assertIn("send", client.called)
        self.assertNotIn("starttls", client.called)

    def test_relay_without_login_is_supported(self):
        settings.SMTP_PORT = 25
        user = auth.bootstrap_admin(f"anon-{uuid.uuid4().hex[:8]}@example.test", "Relay User")
        with mock.patch.object(mailer.smtplib, "SMTP", FakeSMTP):
            auth.request_magic_link(user["email"], "https://treasury.example.test")
        client = FakeSMTP.instances[-1]
        self.assertFalse(any(c.startswith("login") for c in client.called))
        self.assertEqual(len(client.messages), 1)

    def test_failure_log_names_neither_address_nor_token(self):
        user = auth.bootstrap_admin(f"log-{uuid.uuid4().hex[:8]}@example.test", "Log User")
        with mock.patch.object(mailer.smtplib, "SMTP", FailingSMTP):
            with self.assertLogs("mazapay.treasury", level="WARNING") as captured:
                auth.request_magic_link(user["email"], "https://treasury.example.test")
        joined = "\n".join(captured.output)
        self.assertNotIn(user["email"], joined, "the address must not reach the log")
        self.assertNotIn("/treasury/auth/", joined, "the link must not reach the log")


if __name__ == "__main__":
    unittest.main()
