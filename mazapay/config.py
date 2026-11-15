"""MazaPay bridge configuration — env-driven, no secrets in code.

All knobs can be set via environment variables; defaults are safe for a
local prototype run (mock chain, fixed price source, ephemeral-ish paths).
"""
import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


# --- price feed -----------------------------------------------------------
# paprika: https://api.coinpaprika.com/v1/tickers/maza-maza  (no key needed)
# lore:    https://api.coinlore.net/api/ticker/?id=<coinlore id>  (configurable)
PRICE_SOURCE = os.environ.get("MAZAPAY_PRICE_SOURCE", "paprika")  # paprika|lore|fixed
PAPRIKA_TICKER_URL = os.environ.get(
    "MAZAPAY_PAPRIKA_URL", "https://api.coinpaprika.com/v1/tickers/maza-maza"
)
# Automatic live failover source. CoinLore MAZA id 184745 verified live
# 2026-11-15 (it carried the first real E2E quote when paprika 403'd from a
# datacenter host, UA-independent). Set MAZAPAY_LORE_URL="" to disable the
# failover source; the primary source default is unchanged (paprika).
LORE_TICKER_URL = os.environ.get(
    "MAZAPAY_LORE_URL", "https://api.coinlore.net/api/ticker/?id=184745"
)
FIXED_USD_PRICE = _float("MAZAPAY_FIXED_USD_PRICE", 0.002)
PRICE_TIMEOUT_SECONDS = _int("MAZAPAY_PRICE_TIMEOUT", 8)
# Auditable operating mode: when the primary live price source fails, fall
# back to the synthetic fixed price ONLY if this is enabled explicitly. The
# resulting quotes are labeled rate_source='fixed-fallback' in the store,
# audit trail, and MIP export descriptions.
ALLOW_FIXED_PRICE = os.environ.get("MAZAPAY_ALLOW_FIXED_PRICE", "0") in ("1", "true", "yes")

# --- auth ------------------------------------------------------------------
# When set, all SPENDING/ADMIN endpoints require 'Authorization: Bearer <token>'.
# Public without a token: GET / (checkout page), GET /health, and the
# member collect routes POST /orders + GET /orders/{id} (kind='in' only) —
# they create watch addresses and lock quotes, no money moves.
# REQUIRED (fail-closed) when a real wallet RPC is configured; optional in
# mock/demo mode so the offline prototype stays runnable.
API_TOKEN = os.environ.get("MAZAPAY_API_TOKEN", "")

# --- quoting / rate lock ---------------------------------------------------
RATE_LOCK_SECONDS = _int("MAZAPAY_RATE_LOCK_SECONDS", 900)  # 15 min quote expiry
MAZA_DECIMALS = _int("MAZAPAY_MAZA_DECIMALS", 2)  # quote granularity (round DOWN)

# --- chain -----------------------------------------------------------------
CONFIRMATIONS_REQUIRED = _int("MAZAPAY_CONFIRMATIONS", 6)
WATCH_INTERVAL_SECONDS = _int("MAZAPAY_WATCH_INTERVAL", 10)
# Depth of the listtransactions scan used to bind a broadcast to its intent.
# A dedicated bridge wallet keeps this cheap; the residual limit (a broadcast
# older than the window) is disclosed in the README.
TX_SCAN_LIMIT = _int("MAZAPAY_TX_SCAN_LIMIT", 1000)
# Empty MAZAPAY_MAZAD_RPC => MockChain (prototype/demo mode). Real mode points at
# the canonical maza wallet RPC (MazaCoin/maza v0.16.3, Bitcoin Core fork) —
# use the rpcport from the wallet's maza.conf.
MAZAD_RPC_URL = os.environ.get("MAZAPAY_MAZAD_RPC", "")
MAZAD_RPC_USER = os.environ.get("MAZAPAY_MAZAD_RPC_USER", "")
MAZAD_RPC_PASSWORD = os.environ.get("MAZAPAY_MAZAD_RPC_PASSWORD", "")
MAZAD_RPC_TIMEOUT = _int("MAZAPAY_MAZAD_RPC_TIMEOUT", 10)

# --- watcher alerting --------------------------------------------------------
# Deposit/confirmation alerts for inbound payments. Two kinds fire on an
# order's watch address: deposit_detected (funds covering the quote are
# visible at ALERT_DEPOSIT_MINCONF) and deposit_confirmed (the paid/late_paid
# flip at CONFIRMATIONS_REQUIRED). Channels are stdlib-only: the log notifier
# writes to the mazapay.alerts logger; the webhook POSTs the alert as JSON
# (optional bearer token). Idempotency lives in the store: the deposit alert
# is marked only after delivery (undelivered ones retry every poll), the
# confirm alert is marked atomically with the paid flip.
ALERTS_ENABLED = os.environ.get("MAZAPAY_ALERTS", "1") in ("1", "true", "yes")
ALERT_LOG_ENABLED = os.environ.get("MAZAPAY_ALERT_LOG", "1") in ("1", "true", "yes")
ALERT_WEBHOOK_URL = os.environ.get("MAZAPAY_ALERT_WEBHOOK_URL", "")
ALERT_WEBHOOK_TOKEN = os.environ.get("MAZAPAY_ALERT_WEBHOOK_TOKEN", "")
ALERT_WEBHOOK_TIMEOUT = _int("MAZAPAY_ALERT_WEBHOOK_TIMEOUT", 5)
ALERT_DEPOSIT_MINCONF = _int("MAZAPAY_ALERT_DEPOSIT_MINCONF", 1)

# --- abuse caps on the public surface --------------------------------------
# Public order intake (POST /orders is unauthenticated) is capped at three
# layers: a per-order USD ceiling, a cap on simultaneously-awaiting orders per
# client IP, and a global awaiting cap (client IP = X-Forwarded-For's first
# hop when behind the loopback-bound proxy, else the peer address). All three
# answer 429/400 per route; none apply to tokened admin routes.
MAX_ORDER_USD = _float("MAZAPAY_MAX_ORDER_USD", 10000.0)  # ~$10k per order
MAX_DISBURSE_USD = _float("MAZAPAY_MAX_DISBURSE_USD", 10000.0)  # per /payments
MAX_AWAITING_ORDERS_PER_IP = _int("MAZAPAY_MAX_AWAITING_PER_IP", 20)
MAX_AWAITING_ORDERS_GLOBAL = _int("MAZAPAY_MAX_AWAITING_GLOBAL", 200)
ORDER_RETRY_AFTER_SECONDS = _int("MAZAPAY_RETRY_AFTER_SECONDS", 60)

# --- storage / export ------------------------------------------------------
DB_PATH = os.environ.get("MAZAPAY_DB", "/tmp/mazapay-dev.sqlite3")
MIP_EXPORT_DIR = os.environ.get("MAZAPAY_MIP_EXPORT_DIR", "/tmp/mazapay-mip-out")

# --- admin dashboard ---------------------------------------------------------
# GET /admin (token-gated): read-only orders dashboard for non-CLI operators —
# status summary, newest-first order table, links to order detail + the MIP
# export sessions. The page size is bounded so a large store can never
# produce an unbounded response; ?format=json serves scripts/agents with the
# same payload shape.
ADMIN_PAGE_LIMIT = _int("MAZAPAY_ADMIN_PAGE_LIMIT", 100)  # rows per page (default)
ADMIN_MAX_LIMIT = _int("MAZAPAY_ADMIN_MAX_LIMIT", 500)  # hard ceiling on ?limit=

# --- http server ------------------------------------------------------------
LISTEN_HOST = os.environ.get("MAZAPAY_HOST", "127.0.0.1")
LISTEN_PORT = _int("MAZAPAY_PORT", 8443)
# Socket timeout for handler reads/writes (slowloris: a client dribbling a
# request body can no longer hold a thread forever; a timed-out read answers
# 408 and closes). Honored by socketserver via Handler.timeout.
HANDLER_TIMEOUT_SECONDS = _int("MAZAPAY_HANDLER_TIMEOUT_SECONDS", 30)
# Bounded concurrent connections: handlers beyond this many simultaneously
# ACTIVE connections are shed with 503 + Retry-After instead of growing the
# thread count unbounded (ThreadingHTTPServer spawns one thread per connect).
MAX_CONCURRENT_CONNECTIONS = _int("MAZAPAY_MAX_CONCURRENT_CONNECTIONS", 64)
# Export crash recovery: a claimed-but-unwritten export session is adopted
# by a later run only after its lease is older than this many seconds — a
# live exporter's fresh lease is never stolen by a concurrent run.
MIP_CLAIM_ADOPT_SECONDS = _int("MAZAPAY_MIP_CLAIM_ADOPT_SECONDS", 120)

# --- MIP import field map --------------------------------------------------
# MIP Transaction Import consumes CSV "sessions" mapped to the org's import
# definition (the column layout varies per deployment; consultants map
# customer CSVs onto MIP's standard definitions). These defaults define a
# sane receipt/disbursement shape; override via env as a comma-separated list.
CASH_RECEIPT_COLUMNS = [
    col
    for col in os.environ.get(
        "MAZAPAY_MIP_RECEIPT_COLUMNS",
        "SESSION_ID,TRX_DATE,BANK_CODE,PAYOR,REFERENCE,GL_ACCOUNT,AMOUNT,CURRENCY,FX_RATE,DESCRIPTION",
    ).split(",")
    if col
]
CASH_DISBURSEMENT_COLUMNS = [
    col
    for col in os.environ.get(
        "MAZAPAY_MIP_DISBURSEMENT_COLUMNS",
        "SESSION_ID,TRX_DATE,BANK_CODE,VENDOR,REFERENCE,GL_ACCOUNT,AMOUNT,CURRENCY,FX_RATE,DESCRIPTION",
    ).split(",")
    if col
]
MIP_DEFAULT_BANK_CODE = os.environ.get("MAZAPAY_MIP_BANK_CODE", "CMAZA")
MIP_DEFAULT_GL_ACCOUNT = os.environ.get("MAZAPAY_MIP_GL_ACCOUNT", "10100")
MIP_CURRENCY_CODE = os.environ.get("MAZAPAY_MIP_CURRENCY", "USD")
