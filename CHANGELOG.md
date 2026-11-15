# Changelog

## 0.5.0 — 2026-11-15

**Operator dashboard.** New token-protected `GET /admin`: a
server-rendered, script-free HTML page showing status counts and a
newest-first order table (amounts, locked rate + source, txid, ref,
vendor), filterable by `?kind=`/`?status=` and bounded by `?limit=`
(default 100, ceiling 500 so a large store can never produce an
unbounded response). `?format=json` returns the same snapshot for
scripts and agents. Outbound order ids render as plain text — matching
the public-visibility rule that never leaks payment rows to
unauthenticated readers. Every interpolated field is HTML-escaped;
the page contains no JavaScript.

To make the dashboard usable from a plain browser, every tokened route
now also accepts HTTP **Basic** auth with the same token as the
password (username ignored) — same constant-time comparison, same
never-logged guarantee; Bearer remains the documented form for scripts.

## 0.4.0 — 2026-11-15

**Deposit and confirmation alerts.** The watcher now notifies on two
moments of every inbound payment: `deposit_detected` (funds covering the
quote are visible at `MAZAPAY_ALERT_DEPOSIT_MINCONF`, before the payment
is final) and `deposit_confirmed` (the `paid`/`late_paid` flip at
`MAZAPAY_CONFIRMATIONS_REQUIRED`). Channels are pluggable and
stdlib-only: a log notifier (on by default, `mazapay.alerts` logger) and
a JSON webhook (`MAZAPAY_ALERT_WEBHOOK_URL`, optional bearer token via
`MAZAPAY_ALERT_WEBHOOK_TOKEN`), master-switched by `MAZAPAY_ALERTS`.

Idempotency lives in the store, so restarts never duplicate an alert:
the deposit alert is marked on the order only after delivery and
undelivered ones retry every poll (a webhook outage degrades to delayed
delivery); the confirm alert's marker rides the same UPDATE as the paid
flip, making it exactly-once per order. `poll_once` counts now include
`deposit_alerts` / `confirm_alerts` (delivered this pass). New
`orders.deposit_alerted_ts` / `confirm_alerted_ts` columns are added by
the usual tolerant migration; alert bodies carry amounts and public
chain data only, never credentials.

## 0.3.1 — 2026-11-15

**Bring your own daemon, safely.** New `python3 -m mazapay.check_daemon`
self-test: auto-detects `maza.conf`, probes every RPC call the bridge
needs (including `validateaddress`), explains each failure in plain
English with its fix, verifies the send surface without moving funds
(probes with an intentionally invalid amount), warns on zero balance,
prints paste-ready bridge settings without displaying the password, and
emits JSON-only output in `--json` mode (configuration errors are JSON
error objects). Exit 0 = ready.

Fixed: RPC requests with credentials embedded in the URL (`user:pass@host`)
failed DNS resolution under Python 3.14's urllib — both the checker and
`MazadRPC` now strip userinfo from the request target and authenticate
exclusively via the Authorization header (regression tests against a real
local HTTP server). `MazadRPC` also honors URL-embedded credentials when
no separate user/password is configured. HTTP 401/403 responses are
classified as credential failures, not transport errors. The balance
check tolerates malformed daemon replies. Test suite shares one sandbox
module: with no writable temp dir every test skips and discovery exits 0.

## 0.3.0 — 2026-11-14

Member-facing checkout page at `GET /` (quote → address → confirmation,
auto-polls status; shows a demo-mode banner on the mock chain). Collect
routes (`POST /orders`, `GET /orders/{id}`) are public — they create a
watch address and lock a quote, no money moves — while every spending or
administrative endpoint requires the bearer token.

Crash-safe, concurrent MIP exports: three-phase claim/write/finalize with
lease timestamps, compare-and-set session adoption (one recovery worker
per crashed session, even across multiple-row sessions), deterministic
per-session filenames (a resumed session rewrites the same file), and
directory fsync after the atomic rename.

Durable refund linkage: refund payments reference their parent order;
reconciling a refund as sent settles the parent to `refunded`, proving it
never broadcast releases the parent to `late_paid` and supersedes the old
payment (terminal — the duplicate-refund path is closed).

Reconcile semantics hardened: a failed RPC scan keeps payments in
`send_uncertain`; only a completed no-match scan can mark `send_failed`.
Scan depth configurable (`MAZAPAY_TX_SCAN_LIMIT`, default 1000) and
disclosed. Refunds pay the original locked MAZA amount. Decimal money
math end to end. Runner-safe test degradation for read-only sandboxes
(`MAZAPAY_TEST_TMP`), CI workflow, systemd deployment unit.

## 0.2.0 — 2026-11-14

Bearer-token auth on all non-health routes (mandatory in real-wallet
mode); fail-closed startup guards (no tokenless real-wallet serving, no
synthetic fixed price against a real wallet); intent-first disbursements
with reconcile/retry; late-payment reconciliation (`late_paid` →
accept/refund); trigger-enforced append-only audit; Decimal quoting with
auditable fixed-price fallback policy.

## 0.1.0 — 2026-11-14

Initial prototype: USD-locked MAZA quoting (CoinPaprika), Bitcoin-RPC
watcher with in-memory MockChain, MIP CSV export sessions, vendor
registry, offline test suite.
