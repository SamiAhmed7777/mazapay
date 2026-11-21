"""Merchant-shareable payment links (PAYLINK).

A link is a FRONT DOOR to the existing order machinery, not a parallel
pipeline: it stores a USD amount + memo, and a buyer checkout creates a
REAL order through the existing create_order path, so the watcher, the
receipts UI, and the MIP export need zero changes.

- link ids are unguessable (secrets.token_urlsafe(16), 22 url-safe chars);
- money is stored as a Decimal string (never float);
- the public /pay page reuses the checkout page's palette, escaping rules
  and security headers, and leaks nothing beyond the link id itself;
- settlement is observed, never driven: a status-sync helper maps the
  bound order's existing status onto the link (paid/settled -> paid) and
  flips active links past their TTL to expired. watcher.py is untouched.
"""
import html
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import store

# Link lifetime: created_at + MAZAPAY_LINK_TTL_DAYS (UTC ISO timestamps, so
# expiry checks are plain string comparisons of same-format ISO values).
LINK_TTL_DAYS = int(os.environ.get("MAZAPAY_LINK_TTL_DAYS", "30"))

MAX_MEMO_CHARS = 200

# Order statuses that mean "the money is settled" for the linked order.
PAID_ORDER_STATUSES = {"paid", "settled"}

_USD_CENTS = Decimal("0.01")

# The order `ref` rides into MIP export cells, so the memo is copied into it
# ONLY when it already satisfies the existing readable-ref grammar; anything
# else gets a neutral tag. Same character class as the order intake.
_REF_SAFE = re.compile(r"[\w .:@/-]{0,120}")
_NEUTRAL_REF = "paylink"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_plus_days(iso: str, days: int) -> str:
    return (datetime.fromisoformat(iso) + timedelta(days=days)).isoformat(timespec="seconds")


def _fmt_usd(usd_amount: str) -> str:
    return f"{Decimal(usd_amount).quantize(_USD_CENTS):,.2f}"


# --- link lifecycle ------------------------------------------------------------


def create_link(
    usd: Decimal, memo: str, idempotency_key: Optional[str]
) -> Tuple[Dict[str, Any], bool]:
    """Create a link; returns (link, created).

    A repeated idempotency_key returns the SAME link with created=False
    (the API answers 200 instead of 201). The UNIQUE constraint plus the
    IntegrityError fallback makes concurrent same-key creates collapse onto
    one row.
    """
    if idempotency_key:
        existing = store.get_link_by_idempotency_key(idempotency_key)
        if existing is not None:
            return sync_link_status(existing), False
    now = _utc_now_iso()
    link: Dict[str, Any] = {
        "id": secrets.token_urlsafe(16),
        "usd_amount": str(usd),
        "memo": memo,
        "status": "active",
        "order_id": None,
        "created_at": now,
        "expires_at": _utc_plus_days(now, LINK_TTL_DAYS),
        "idempotency_key": idempotency_key,
    }
    try:
        store.insert_link(link)
    except sqlite3.IntegrityError:
        if idempotency_key:
            existing = store.get_link_by_idempotency_key(idempotency_key)
            if existing is not None:
                return sync_link_status(existing), False
        raise
    return link, True


def sync_link_status(link: Dict[str, Any]) -> Dict[str, Any]:
    """Status-sync hook: fold the world into the link's status.

    Called on every link-status read (never from the watcher): an active
    link whose bound order settled becomes paid; an active link past its
    TTL becomes expired. Flips are compare-and-sets, so a concurrent
    deactivation can never be clobbered.
    """
    if link["status"] != "active":
        return link
    order_id = link.get("order_id")
    if order_id:
        order = store.get_order(order_id)
        if order is not None and order["status"] in PAID_ORDER_STATUSES:
            if not store.flip_link_status(link["id"], ("active",), "paid", event="order_settled"):
                fresh = store.get_link(link["id"])
                if fresh:
                    link.update(fresh)
            else:
                link["status"] = "paid"
            return link
    if link["expires_at"] <= _utc_now_iso():
        if not store.flip_link_status(link["id"], ("active",), "expired", event="link_expired"):
            fresh = store.get_link(link["id"])
            if fresh:
                link.update(fresh)
        else:
            link["status"] = "expired"
    return link


def get_link(link_id: str) -> Optional[Dict[str, Any]]:
    link = store.get_link(link_id)
    return sync_link_status(link) if link is not None else None


def list_links() -> List[Dict[str, Any]]:
    return [sync_link_status(link) for link in store.list_links()]


def attach_order(link_id: str, order_id: str) -> None:
    """Bind the first checkout's order to the link (status stays active
    until that order settles — the sync hook observes the flip)."""
    store.set_link_order(link_id, order_id)


def order_ref(link: Dict[str, Any]) -> str:
    """The ref the checkout order carries: the memo when it already passes
    the readable-ref grammar, else a neutral tag (never raw free-form text
    into the MIP export)."""
    memo = link["memo"]
    return memo if memo and _REF_SAFE.fullmatch(memo) else _NEUTRAL_REF


def link_public(link: Dict[str, Any], pay_url: str) -> Dict[str, Any]:
    """API-facing shape (never more than the link's own fields)."""
    return {
        "link_id": link["id"],
        "pay_url": pay_url,
        "usd_amount": link["usd_amount"],
        "memo": link["memo"],
        "status": link["status"],
        "order_id": link.get("order_id"),
        "created_at": link["created_at"],
        "expires_at": link["expires_at"],
    }


# --- public /pay page -----------------------------------------------------------
#
# One document, three server-rendered states (active / paid / unavailable).
# Every interpolated value goes through html.escape. The page carries no
# merchant identity, no tokens, no order ids and no /admin data; the inline
# script (active state only) follows the checkout page's fetch+poll flow
# against the SAME public collect endpoints.


def pay_page_html(link: Dict[str, Any]) -> str:
    status = link["status"]
    memo_text = link["memo"] or "You've been sent a payment request through MazaPay."
    amount = _fmt_usd(link["usd_amount"])
    if status == "paid":
        body, script = _PAID_BODY, ""
    elif status in ("deactivated", "expired"):
        body, script = _UNAVAILABLE_BODY, ""
    else:
        body, script = _ACTIVE_BODY, _ACTIVE_SCRIPT
    page = _DOC.replace("__STYLE__", _STYLE)
    page = page.replace("__BODY__", _fill(body, link, amount, memo_text))
    page = page.replace("__SCRIPT__", _fill(script, link, amount, memo_text))
    return page


def _fill(template: str, link: Dict[str, Any], amount: str, memo_text: str) -> str:
    e = html.escape
    out = template.replace("__LINK_ID__", e(link["id"]))
    out = out.replace("__MEMO__", e(memo_text))
    out = out.replace("__AMOUNT__", e(amount))
    out = out.replace("__EXPIRES__", e(str(link["expires_at"])[:10]))
    return out


_DOC = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0b0d12">
<title>Pay with MAZA · MazaPay payment link</title>
<link rel="icon" type="image/png" href="/static/mazacoin-180.png">
<link rel="apple-touch-icon" href="/static/mazacoin-180.png">
__STYLE__
</head>
<body>
<main class="shell">
  <div class="masthead">
    <img class="badge-accepted" src="/static/mazacoin-accepted.png" alt="MazaCoin — MAZA accepted here" width="168" height="111">
  </div>
  <div class="card">
    <div class="chip" id="mock-banner">Demo mode · mock chain</div>
__BODY__
  </div>
  <footer class="fine">
    <p class="tagline">Powered by MazaCoin <span class="dot">·</span> settled in USD on the books</p>
    <p>Payments are watched on-chain and credited after <span id="conf">6</span> confirmations. Your receipt records the exact USD value at the locked rate.</p>
  </footer>
</main>
__SCRIPT__
</body>
</html>
"""

# Palette + components mirror templates/checkout.html (same brand tokens and
# contrast-measured pairs); only classes this page renders are declared.
_STYLE = """<style>
  :root {
    --bg:#0b0d12; --surface:#151b24; --surface-2:#0d1117; --line:#242c39;
    --line-strong:#5f6d8c; --text:#eef1f6; --muted:#9aa4b6;
    --amber:#f0a000; --amber-soft:#ffc45c; --ink:#101318;
    --red-text:#ff7d7d; --ok:#4ecb8a; --radius:18px;
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body {
    margin:0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background:var(--bg); color:var(--text); min-height:100vh;
    padding:20px 16px 32px; display:flex; flex-direction:column; align-items:center; gap:18px;
  }
  body::before {
    content:""; position:fixed; inset:0 0 auto 0; height:340px;
    background:radial-gradient(620px 250px at 50% -80px, rgba(240,160,0,.17), rgba(240,160,0,0) 72%);
    pointer-events:none; z-index:0;
  }
  .shell, .masthead, .card, .fine { position:relative; z-index:1; }
  .shell { width:100%; max-width:520px; display:flex; flex-direction:column; gap:14px; }
  .masthead { display:flex; align-items:center; justify-content:center; }
  .badge-accepted { display:block; width:168px; height:auto; border-radius:12px;
    box-shadow:0 6px 18px -10px rgba(0,0,0,.9); }
  .chip { display:none; align-items:center; gap:6px; font-size:10px; letter-spacing:.12em;
    text-transform:uppercase; color:var(--muted); border:1px solid var(--line);
    background:var(--surface-2); border-radius:999px; padding:4px 9px; margin:0 0 14px; }
  .chip::before { content:""; width:6px; height:6px; border-radius:50%; background:var(--amber); opacity:.75; }
  .card {
    background:linear-gradient(180deg, #19202a 0%, var(--surface) 42%, #141a23 100%);
    border:1px solid var(--line); border-radius:var(--radius); padding:22px 20px 24px;
    box-shadow:0 30px 70px -34px rgba(0,0,0,.95), inset 0 1px 0 rgba(255,255,255,.045);
  }
  .eyebrow { margin:0 0 7px; font-size:.68rem; font-weight:700; letter-spacing:.16em;
    text-transform:uppercase; color:var(--muted); }
  h1 { margin:0 0 6px; font-size:1.5rem; line-height:1.2; letter-spacing:-.01em; }
  .lede { margin:0 0 18px; color:var(--muted); font-size:.94rem; line-height:1.55;
    overflow-wrap:anywhere; }
  .amount { text-align:center; margin:4px 0 6px; font-size:clamp(2rem, 10vw, 2.8rem);
    font-weight:800; letter-spacing:-.01em; color:var(--amber-soft);
    font-variant-numeric:tabular-nums; }
  .amount .unit { font-size:.72rem; letter-spacing:.14em; color:var(--muted); font-weight:700; }
  .btn { display:inline-flex; align-items:center; justify-content:center; gap:8px; width:100%;
    min-height:52px; border-radius:12px; border:1px solid transparent; font:inherit;
    font-size:1rem; font-weight:700; cursor:pointer; transition:transform .12s ease,
    box-shadow .15s ease, background .15s ease, border-color .15s ease; }
  .btn:focus-visible { outline:2px solid var(--amber); outline-offset:2px; }
  .btn.primary { background:linear-gradient(180deg, var(--amber-soft) 0%, var(--amber) 62%);
    color:var(--ink); box-shadow:0 12px 24px -18px rgba(240,160,0,.55); }
  .btn.primary:hover { transform:translateY(-1px); box-shadow:0 16px 28px -18px rgba(240,160,0,.7); }
  .btn.primary:disabled { opacity:.55; cursor:default; transform:none; box-shadow:none; }
  .btn.ghost { background:transparent; border-color:var(--line-strong); color:var(--text);
    min-height:46px; font-size:.92rem; font-weight:600; }
  .btn.ghost:hover { border-color:var(--amber); color:var(--amber-soft); }
  .err { min-height:1.25em; margin:10px 0 0; color:var(--red-text); font-size:.86rem; line-height:1.4; }
  .quote { display:none; }
  .hero { text-align:center; margin:8px 0 6px; }
  .hero-amount { display:flex; align-items:baseline; justify-content:center; gap:10px; flex-wrap:wrap; }
  #maza { font-size:clamp(2.35rem, 12.5vw, 3.4rem); font-weight:800; letter-spacing:-.02em;
    line-height:1.05; color:var(--amber-soft); font-variant-numeric:tabular-nums; word-break:break-word; }
  .hero-unit { font-size:.76rem; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); font-weight:700; }
  .hero-sub { margin:9px 0 0; color:var(--muted); font-size:.86rem; line-height:1.5; }
  .hero-sub strong { color:var(--text); font-weight:700; }
  .ttl { display:inline-flex; align-items:center; gap:6px; color:var(--amber-soft); font-weight:700;
    font-variant-numeric:tabular-nums; background:rgba(240,160,0,.12); border:1px solid rgba(240,160,0,.4);
    border-radius:999px; padding:1px 8px 1px 7px; font-size:.82rem; }
  .ttl::before { content:""; width:6px; height:6px; border-radius:50%; background:var(--amber); }
  .rate-line { margin:0 0 12px; text-align:center; font-size:.8rem; color:var(--muted); line-height:1.5; }
  .rate-line > span { display:block; }
  .rate-line .rate-src { font-size:.74rem; margin-top:2px; }
  .rate-line .mono { color:var(--text); font-weight:600; }
  .status { display:flex; align-items:center; gap:9px; margin:0 0 4px; padding:11px 13px;
    background:var(--surface-2); border:1px solid var(--line); border-radius:11px;
    font-size:.85rem; color:var(--muted); line-height:1.45; }
  .status::before { content:""; flex:0 0 auto; width:8px; height:8px; border-radius:50%;
    background:var(--amber); animation:breathe 1.6s ease-in-out infinite; }
  .status.paid { color:var(--ok); border-color:rgba(78,203,138,.45); font-weight:600; }
  .status.paid::before { background:var(--ok); animation:none; }
  .status.warn { color:var(--amber-soft); border-color:rgba(240,160,0,.45); }
  .status.warn::before { background:var(--amber); animation:none; }
  .quote.is-closed .qr-tile, .quote.is-closed .addr-block, .quote.is-closed .rate-line { display:none; }
  .qr-tile { margin:12px 0 12px; padding:16px 14px 13px; background:var(--surface-2);
    border:1px solid var(--line); border-radius:14px; text-align:center; }
  .qr-frame { display:inline-block; padding:9px; background:#fff; border-radius:12px;
    box-shadow:0 14px 30px -20px rgba(0,0,0,.95); line-height:0; }
  #qr { display:block; width:184px; height:184px; image-rendering:pixelated; border-radius:2px; }
  .qr-missing { display:none; }
  .qr-missing.show { display:block; }
  .qr-cap { display:flex; align-items:center; justify-content:center; gap:7px; margin:12px 0 0;
    font-size:.86rem; color:var(--text); font-weight:600; }
  .qr-cap img { width:16px; height:16px; display:block; border-radius:50%; }
  .addr-label { display:block; font-size:.72rem; letter-spacing:.11em; text-transform:uppercase;
    color:var(--muted); font-weight:600; margin:0 0 8px; }
  .addr { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:.8rem;
    line-height:1.5; overflow-wrap:anywhere; word-break:break-word; color:#d3dae8;
    background:var(--surface-2); border:1px solid var(--line); border-radius:10px;
    padding:12px 13px; margin:0 0 10px; }
  .mono { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .receipt, .expired { display:none; margin:20px 0 0; padding:20px 15px 16px;
    background:var(--surface-2); border:1px solid var(--line); border-radius:14px; }
  .receipt[hidden], .expired[hidden] { display:none; }
  .receipt:not([hidden]), .expired:not([hidden]) { display:block; }
  .seal { width:54px; height:54px; border-radius:50%; display:grid; place-items:center;
    margin:0 auto 12px; background:rgba(78,203,138,.12); border:1.5px solid rgba(78,203,138,.65);
    color:var(--ok); font-size:1.5rem; font-weight:800; line-height:1; }
  .receipt h2, .expired h2 { margin:0 0 4px; font-size:1.16rem; text-align:center; letter-spacing:-.01em; }
  .receipt-sub, .expired p { margin:0 0 16px; text-align:center; color:var(--muted); font-size:.85rem; line-height:1.5; }
  .expired h2 { color:var(--red-text); }
  .receipt-rows { margin:0; padding:0; }
  .rrow { display:flex; align-items:baseline; justify-content:space-between; gap:14px;
    padding:9px 0; border-top:1px solid var(--line); }
  .rrow:first-child { border-top:none; }
  .rrow dt { margin:0; color:var(--muted); font-size:.8rem; }
  .rrow dd { margin:0; text-align:right; font-size:.86rem; font-weight:600; color:var(--text);
    font-variant-numeric:tabular-nums; word-break:break-word; }
  .rrow dd.mono { font-size:.79rem; font-weight:500; color:#d3dae8; }
  .receipt-hint { margin:14px 0 0; text-align:center; color:var(--muted); font-size:.76rem; }
  @media (max-width:379px) {
    .rrow { flex-direction:column; align-items:flex-start; gap:2px; }
    .rrow dd { text-align:left; }
  }
  .fine { width:100%; max-width:520px; text-align:center; color:var(--muted); font-size:.8rem; line-height:1.65; }
  .fine p { max-width:400px; margin:0 auto; }
  .fine .tagline { margin:0 auto 6px; color:var(--text); font-size:.84rem; font-weight:600; letter-spacing:.01em; }
  .fine .tagline .dot { color:var(--amber); }
  @keyframes breathe { 0%,100% { opacity:1; } 50% { opacity:.35; } }
  @media (min-height:740px) { body { justify-content:center; } }
  @media (prefers-reduced-motion: reduce) {
    * { animation:none !important; transition:none !important; }
  }
  @media (min-width:900px) { .shell, .fine { max-width:560px; } .card { padding:32px; } }
  @media (min-width:640px) {
    body { padding:44px 20px 40px; }
    .card { padding:30px 28px 30px; }
    .badge-accepted { width:184px; }
    .lede { font-size:.97rem; }
    #qr { width:208px; height:208px; }
  }
</style>"""

_REQUEST_HEAD = """    <p class="eyebrow">MazaPay payment link</p>
    <h1>Payment request</h1>
    <p class="lede">__MEMO__</p>
    <div class="amount">$__AMOUNT__ <span class="unit">USD</span></div>
"""

_ACTIVE_BODY = """    <section id="form-view">
__REQUEST_HEAD__    <p class="rate-line"><span>This link expires __EXPIRES__ (UTC)</span></p>
      <button class="btn primary" id="go" onclick="start()">Pay $__AMOUNT__ USD with MAZA</button>
      <p class="err" id="err" role="alert"></p>
    </section>

    <section class="quote" id="quote">
      <div class="hero">
        <div class="hero-amount"><span id="maza">—</span><span class="hero-unit">MAZA</span></div>
        <p class="hero-sub">for <strong id="usd-due">—</strong> <span id="hero-hold">· rate held <span class="ttl" id="ttl">—</span></span><span id="hero-state" hidden>· paid</span></p>
      </div>
      <p class="rate-line">
        <span>Locked rate <span class="mono" id="rate">—</span> USD/MAZA</span>
        <span class="rate-src">Source: <span id="src">—</span></span>
      </p>
      <p class="status" id="status" role="status" aria-live="polite">Watching the chain for your payment…</p>
      <div class="qr-tile">
        <div class="qr-frame">
          <div id="qr-box" style="display:none;margin:0 auto"><img id="qr" alt="QR code of the payment address" width="184" height="184"></div>
          <div class="qr-missing" id="qr-missing"><p style="margin:0">QR unavailable — copy the address below and paste it into your wallet.</p></div>
        </div>
        <p class="qr-cap"><img src="/static/mazacoin-64.png" alt="" width="16" height="16"> Scan with your MazaCoin wallet</p>
      </div>
      <div class="addr-block">
        <span class="addr-label">Send exactly this amount to</span>
        <div class="addr" id="addr">—</div>
        <button class="btn ghost" onclick="copyAddr(this)">Copy address</button>
      </div>
    </section>

    <section class="receipt" id="receipt" hidden>
      <div class="seal" aria-hidden="true">✓</div>
      <h2>Your receipt</h2>
      <p class="receipt-sub">Credited at your locked rate.</p>
      <dl class="receipt-rows">
        <div class="rrow"><dt>Amount</dt><dd id="r-usd">—</dd></div>
        <div class="rrow"><dt>MAZA received</dt><dd id="r-maza">—</dd></div>
        <div class="rrow"><dt>Locked rate</dt><dd id="r-rate">—</dd></div>
        <div class="rrow"><dt>Rate source</dt><dd id="r-src">—</dd></div>
        <div class="rrow"><dt>Confirmations</dt><dd id="r-conf">—</dd></div>
        <div class="rrow"><dt>Transaction</dt><dd id="r-tx" class="mono">—</dd></div>
        <div class="rrow"><dt>Time</dt><dd id="r-time">—</dd></div>
        <div class="rrow"><dt>Order</dt><dd id="r-order" class="mono">—</dd></div>
      </dl>
      <p class="receipt-hint">Screenshot this panel for your records.</p>
    </section>

    <section class="expired" id="expired" hidden>
      <h2 id="expired-title">This quote expired</h2>
      <p id="expired-text">The rate is only held for a short window. Return to the payment link to get a fresh quote.</p>
    </section>"""

_PAID_BODY = """    <section>
__REQUEST_HEAD__    <p class="status paid">This payment link has already been paid. Thank you!</p>
    </section>"""

_UNAVAILABLE_BODY = """    <section>
__REQUEST_HEAD__    <p class="status warn">This payment link is no longer available.</p>
    </section>"""

_ACTIVE_BODY = _ACTIVE_BODY.replace("__REQUEST_HEAD__", _REQUEST_HEAD)
_PAID_BODY = _PAID_BODY.replace("__REQUEST_HEAD__", _REQUEST_HEAD)
_UNAVAILABLE_BODY = _UNAVAILABLE_BODY.replace("__REQUEST_HEAD__", _REQUEST_HEAD)

# Buyer flow: create the order through the LINK's own checkout endpoint (the
# existing order machinery behind it), then poll the same public order read
# the checkout page uses. No wallet, no account.
_ACTIVE_SCRIPT = """<script>
const $ = (id) => document.getElementById(id);
let orderId = null, expiresAt = null, timer = null, poller = null;
let sawSeen = false, finished = false;

function fmtMaza(v) {
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: 8 });
}
function fmtUsd(v) {
  return '$' + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function shortTx(txid) {
  return txid ? txid.slice(0, 10) + '…' + txid.slice(-6) : '—';
}
function rateLabel(src) {
  const names = { paprika: 'CoinPaprika', lore: 'CoinLore', fixed: 'Fixed quote', 'fixed-fallback': 'Fixed fallback' };
  return names[src] || src || '—';
}

fetch('/health').then(r => r.json()).then(h => {
  $('conf').textContent = h.confirmations_required;
  if (h.chain_mode === 'mock') $('mock-banner').style.display = 'inline-flex';
}).catch(() => {});

async function start() {
  $('err').textContent = ''; $('go').disabled = true;
  try {
    const r = await fetch('/pay/__LINK_ID__/checkout', { method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: '{}' });
    const body = await r.json();
    if (!r.ok) { $('err').textContent = body.error || 'Could not start the payment.'; return; }
    orderId = body.id; expiresAt = body.expires_ts * 1000;
    $('maza').textContent = fmtMaza(body.maza_amount);
    $('usd-due').textContent = fmtUsd(body.usd_amount);
    $('rate').textContent = Number(body.rate).toFixed(6);
    $('src').textContent = rateLabel(body.rate_source);
    $('addr').textContent = body.address;
    sawSeen = false; finished = false;
    $('status').classList.remove('paid', 'warn');
    $('status').textContent = 'Watching the chain for your payment…';
    $('receipt').hidden = true; $('expired').hidden = true;
    $('quote').classList.remove('is-paid', 'is-closed');
    $('hero-hold').style.display = ''; $('hero-state').hidden = true;
    $('qr-box').style.display = 'none'; $('qr-missing').classList.remove('show');
    $('qr').alt = 'QR code of the MAZA payment address beginning ' + body.address.slice(0, 6);
    $('qr').onload = function () { $('qr-box').style.display = 'block'; };
    $('qr').onerror = function () { $('qr-box').style.display = 'none'; $('qr-missing').classList.add('show'); };
    $('qr').src = '/orders/' + orderId + '/qr.svg';
    $('form-view').style.display = 'none';
    $('quote').style.display = 'block';
    timer = setInterval(tick, 500);
    tick(); poller = setInterval(check, 4000); check();
  } finally { $('go').disabled = false; }
}

function tick() {
  const left = Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
  $('ttl').textContent = Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0');
}

async function check() {
  const r = await fetch('/orders/' + orderId);
  if (!r.ok) return;
  const o = await r.json();
  const seen = o.status === 'paid' || !!o.deposit_alerted_ts || Number(o.confirmations || 0) > 0;
  if (seen && !sawSeen) {
    sawSeen = true;
    $('status').textContent = 'Payment seen on-chain — waiting for confirmations.';
  }
  if (o.status === 'paid') {
    finishReceipt(o);
  } else if (o.status === 'expired' || o.status === 'late_paid') {
    expireQuote(o);
  }
}

function finishReceipt(o) {
  if (finished) return;
  finished = true;
  clearInterval(poller); clearInterval(timer);
  $('status').textContent = '✓ Payment confirmed — thank you!';
  $('status').classList.add('paid');
  $('quote').classList.add('is-paid', 'is-closed');
  $('hero-hold').style.display = 'none';
  $('hero-state').textContent = '· paid · thank you';
  $('hero-state').hidden = false;
  $('r-usd').textContent = fmtUsd(o.usd_amount);
  $('r-maza').textContent = fmtMaza(o.maza_amount) + ' MAZA';
  $('r-rate').textContent = Number(o.rate).toFixed(6) + ' USD/MAZA';
  $('r-src').textContent = rateLabel(o.rate_source);
  $('r-conf').textContent = Number(o.confirmations || 0) + ' of ' + $('conf').textContent;
  $('r-tx').textContent = shortTx(o.txid);
  if (o.txid) $('r-tx').title = o.txid;
  $('r-time').textContent = o.paid_ts ? new Date(o.paid_ts * 1000).toLocaleString() : '—';
  $('r-order').textContent = o.id;
  $('receipt').hidden = false;
  $('receipt').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function expireQuote(o) {
  clearInterval(poller); clearInterval(timer);
  $('quote').classList.add('is-closed');
  $('hero-hold').style.display = 'none';
  $('hero-state').textContent = o.status === 'late_paid' ? '· received late' : '· expired';
  $('hero-state').hidden = false;
  if (o.status === 'late_paid') {
    $('expired-title').textContent = 'Payment arrived after this quote expired';
    $('expired-text').textContent = 'It has been flagged for review and no action is needed from you — the operator will accept it or refund it.';
  }
  $('status').textContent = o.status === 'late_paid'
    ? 'Payment received late — flagged for review.'
    : 'Quote expired — return to the payment link for a fresh quote.';
  $('status').classList.add('warn');
  $('expired').hidden = false;
}

function copyAddr(btn) {
  navigator.clipboard.writeText($('addr').textContent);
  btn.textContent = 'Copied ✓';
  setTimeout(() => btn.textContent = 'Copy address', 1500);
}
</script>"""
