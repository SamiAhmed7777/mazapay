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
LORE_TICKER_URL = os.environ.get("MAZAPAY_LORE_URL", "")
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

# --- storage / export ------------------------------------------------------
DB_PATH = os.environ.get("MAZAPAY_DB", "/tmp/mazapay-dev.sqlite3")
MIP_EXPORT_DIR = os.environ.get("MAZAPAY_MIP_EXPORT_DIR", "/tmp/mazapay-mip-out")

# --- http server -----------------------------------------------------------
LISTEN_HOST = os.environ.get("MAZAPAY_HOST", "127.0.0.1")
LISTEN_PORT = _int("MAZAPAY_PORT", 8443)
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
