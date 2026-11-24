"""Treasury settings — env-driven, same conventions as mazapay.config.

Kept in its own module so the treasury package never edits the bridge's
config.py (shared-file discipline; see docs/CONTRACT-treasury.md §3).
"""
import os

from .. import config as _bridge


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) in ("1", "true", "yes", "on")


# The treasury shares the bridge's database file by default: disbursements are
# recorded in the bridge's `orders` table and the statements join them.
DB = os.environ.get("MAZAPAY_TR_DB") or getattr(_bridge, "DB_PATH", "") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mazapay.sqlite3"
)

HOST = os.environ.get("MAZAPAY_TR_HOST", "127.0.0.1")
PORT = _int("MAZAPAY_TR_PORT", 18501)

SESSION_TTL = _int("MAZAPAY_TR_SESSION_TTL", 8 * 3600)
MAGIC_TTL = _int("MAZAPAY_TR_MAGIC_TTL", 900)
COOKIE_SECURE = _flag("MAZAPAY_TR_COOKIE_SECURE", "1")
BASE_URL = os.environ.get("MAZAPAY_TR_BASE_URL", f"http://{HOST}:{PORT}").rstrip("/")
DEMO_MODE = _flag("MAZAPAY_TR_DEMO_MODE", "0")

SESSION_COOKIE = "mazapay_tr"
CSRF_COOKIE = "mazapay_tr_csrf"

# Name shown in the UI chrome; the demo seed sets a sample organisation.
ORG_NAME = os.environ.get("MAZAPAY_TR_ORG_NAME", "Sample Tribal Nation")

# --- magic-link mail delivery (stdlib smtplib; empty host = disabled) -------
SMTP_HOST = os.environ.get("MAZAPAY_TR_SMTP_HOST", "")
SMTP_PORT = _int("MAZAPAY_TR_SMTP_PORT", 587)
SMTP_USER = os.environ.get("MAZAPAY_TR_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("MAZAPAY_TR_SMTP_PASSWORD", "")
SMTP_STARTTLS = _flag("MAZAPAY_TR_SMTP_STARTTLS", "1")
SMTP_TIMEOUT = _int("MAZAPAY_TR_SMTP_TIMEOUT", 10)
SMTP_FROM = os.environ.get("MAZAPAY_TR_SMTP_FROM", f"no-reply@{os.environ.get('HOSTNAME', 'mazapay')}")
