# Changelog

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
