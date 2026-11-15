# MazaPay Bridge

**Accept and disburse MazaCoin (MAZA) inside the software tribal nations
already run** — starting with MIP Fund Accounting (Momentive Software), the
fund-accounting system used by tribes across the US.

The bridge is a small, self-hosted service that:

1. **Quotes MAZA against USD with a locked rate** — price pulled live from
   CoinPaprika (no API key), rate frozen per order with an expiry window, so
   every receipt recognizes the exact USD value (IRS Notice 2014-21 /
   ASU 2023-08 treatment). Money math is Decimal end to end.
2. **Watches the chain** — unique address per order, confirmations counted,
   order flips to `paid` with txid + audit entry. Receives via the wallet's
   standard Bitcoin-style JSON-RPC (`getnewaddress`,
   `getreceivedbyaddress`); no third-party processor involved. Late
   payments on expired quotes land in a `late_paid` reconciliation state
   (accept at the original quote, or refund) instead of being dropped.
3. **Pays vendors in MAZA, intent-first** — the payment intent is durably
   recorded (with a unique nonce) *before* the broadcast; an RPC timeout
   after broadcast lands in `send_uncertain` and `/payments/{id}/reconcile`
   matches the broadcast via the nonce; retry exists only for payments
   proven not to have broadcast, and a duplicate send for a payment that
   already has a txid is refused.
4. **Reconciles into MIP** — writes CSV transaction-import sessions (cash
   receipts + cash disbursements) with the full audit trail (MAZA quantity,
   locked rate, rate source, txid) embedded in each row. Exports are
   idempotent and crash-safe: rows are claimed atomically (lease +
   compare-and-set adoption, so exactly one recovery worker completes a
   crashed session), and the session id IS the filename — a resumed session
   rewrites the same file, so a crash can never leave two importable CSVs
   for the same rows. When an org has the
   [MIP Cloud API](https://apims-mip-01.developer.azure-api.net/) enabled,
   the same payloads can post directly (planned poster module).
5. **Keeps an enforceable audit trail** — every state transition lands in
   an append-only SQLite audit table; UPDATE/DELETE on it are rejected by
   database triggers, not caller discipline.
6. **Fails closed on dangerous configurations** — a real wallet RPC refuses
   to serve without `MAZAPAY_API_TOKEN`, and a synthetic fixed price is
   refused in real-wallet mode unless explicitly allowed
   (`MAZAPAY_ALLOW_FIXED_PRICE=1`; such quotes are labeled
   `fixed-fallback` everywhere).

## Why this exists

Momentive's products are closed-source SaaS/on-prem; none of them speak
cryptocurrency. But MIP has three sanctioned hooks that make a bridge
possible **without touching MIP itself**:

| Hook | What it gives us |
|---|---|
| Transaction Import (CSV) | Universal — every MIP deployment has it; NightShift can schedule runs |
| MIP Cloud API (Azure APIM) | Official REST posting path for orgs with API access |
| Multicurrency module | MAZA can be registered as a custom currency; holdings revalue with automatic gain/loss |

`vendor/` + `orders/` records in this bridge mirror the procure-to-pay
objects (suppliers, invoices, payments) so the CSV maps cleanly onto MIP's
import definitions. Column layouts are env-configurable per deployment
(`MAZAPAY_MIP_RECEIPT_COLUMNS`, `MAZAPAY_MIP_DISBURSEMENT_COLUMNS`) because
import definitions differ by org.

## Quickstart (no node required)

```bash
python3 -m unittest discover -s tests -v       # full offline test suite
python3 -m mazapay.api                          # serve on 127.0.0.1:8443 (mock chain)
```

Open **http://127.0.0.1:8443/** in a browser for the member-facing
checkout page — enter a USD amount, get a locked quote and a MAZA address,
watch it confirm. (On the mock chain a demo banner shows; no real funds
move.) CLI equivalents:

```bash
curl -s localhost:8443/health
curl -s -X POST localhost:8443/vendors -d '{"name":"Test Supplier","code":"TSUP","maza_address":"MADDR88"}'
curl -s -X POST localhost:8443/orders -d '{"usd_amount":20.0,"ref":"invoice-42"}'
curl -s -X POST localhost:8443/orders/<id>/simulate_payment -d '{"confirms":6}'
curl -s -X POST localhost:8443/watch
curl -s "localhost:8443/export/mip?kind=in"
```

## Going live with a real wallet

Install the canonical MAZA wallet
([MazaCoin/maza, release v0.16.3](https://github.com/MazaCoin/maza/releases/tag/v0.16.3) —
a Bitcoin Core 0.16.3 fork, so it speaks standard Bitcoin JSON-RPC),
enable RPC in `maza.conf` (set `rpcuser`, `rpcpassword`, `rpcport`),
then point the bridge at it. **A token is mandatory in real mode** (the
service refuses to start otherwise, because POST /payments spends from the
wallet):

```bash
export MAZAPAY_MAZAD_RPC="http://127.0.0.1:<rpcport-from-maza.conf>"
export MAZAPAY_MAZAD_RPC_USER="mazapay"
export MAZAPAY_MAZAD_RPC_PASSWORD="..."
export MAZAPAY_API_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export MAZAPAY_PRICE_SOURCE="paprika"
```

When MAZAPAY_API_TOKEN is set, every request to spending/admin endpoints
carries `Authorization: Bearer <token>`. Member-facing collect routes
(`POST /orders`, `GET /orders/{id}`, the checkout page, `/health`) stay
public — they create watch addresses and lock quotes, nothing spends.
`/orders/*/simulate_payment` refuses with 403 against a real wallet.
Receiving is watch-compatible; sending (`/payments`) spends from the
wallet, so run it on a machine whose wallet you back up.

### Deployment

A hardened systemd unit ships in `deploy/mazapay.service` (dedicated user,
`ProtectSystem=strict`, env secrets via `EnvironmentFile=/etc/mazapay.env`
mode 0600). CI runs the suite on Python 3.10–3.13 on every push.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `MAZAPAY_MAZAD_RPC` | *(empty → mock)* | maza wallet JSON-RPC URL |
| `MAZAPAY_API_TOKEN` | *(empty → open mode)* | bearer token; REQUIRED in real-wallet mode |
| `MAZAPAY_PRICE_SOURCE` | `paprika` | `paprika` \| `lore` \| `fixed` |
| `MAZAPAY_ALLOW_FIXED_PRICE` | `0` | allow auditable `fixed-fallback` when the live feed fails (also required to run `fixed` source against a real wallet) |
| `MAZAPAY_FIXED_USD_PRICE` | `0.002` | synthetic price for offline runs/explicit fixed mode |
| `MAZAPAY_RATE_LOCK_SECONDS` | `900` | quote expiry window |
| `MAZAPAY_CONFIRMATIONS` | `6` | confirmations required to mark paid |
| `MAZAPAY_TX_SCAN_LIMIT` | `1000` | wallet transaction scan depth for reconcile/retry txid matching |
| `MAZAPAY_MIP_CLAIM_ADOPT_SECONDS` | `120` | age before a crashed export claim is adoptable |
| `MAZAPAY_WATCH_INTERVAL` | `10` | watcher poll interval (s) |
| `MAZAPAY_DB` | `/tmp/mazapay-dev.sqlite3` | SQLite store path |
| `MAZAPAY_MIP_EXPORT_DIR` | `/tmp/mazapay-mip-out` | where CSV sessions land |
| `MAZAPAY_MIP_RECEIPT_COLUMNS` / `MAZAPAY_MIP_DISBURSEMENT_COLUMNS` | see `config.py` | CSV column layout per org import definition |
| `MAZAPAY_PORT` / `MAZAPAY_HOST` | `8443` / `127.0.0.1` | HTTP listener |
| `MAZAPAY_TEST_TMP` | *(system temp)* | writable temp root for the test suite (sandbox runs) |

## Order states

- **collect (kind `in`)**: `awaiting` → `paid` at N confirmations; quote
  expiry → `expired`; late funds → `late_paid` → operator `accept` (paid)
  or `refund` (outbound nonce-guarded payment **at the original locked
  MAZA amount**, durably linked to its parent order; order → `refunded`).
- **disburse (kind `out`)**: intent recorded `pending_send` → `sent`
  (txid) on success; RPC failure → `send_uncertain` → `reconcile` binds the
  nonce-matched broadcast (`sent`) or — only when the scan itself
  COMPLETED and found no match — `send_failed` (a failed RPC scan keeps
  the payment `send_uncertain`; an unavailable lookup is never treated as
  proof that no broadcast happened) → `retry` re-broadcasts on the same
  row via an atomic compare-and-set (one winner; concurrent retries are
  refused), and any retry is refused once a txid exists.
- **refund linkage**: the refund payment durably references its parent
  order. Reconciling a refund to `sent` settles the parent to `refunded`;
  proving a refund never broadcast releases the parent back to `late_paid`
  for another attempt — the pair cannot end up inconsistent.

## Honest limits (v0.3)

- **Thin market**: MAZA liquidity is tiny (tracked exchange volume has been
  in the tens-to-hundreds of USD/day). The bridge locks USD pricing and
  records rates, but treasury policy (hold vs sweep) is an accounting
  decision, not code.
- **CSV, not native posting**: the MIP Cloud API poster module (posting
  receipts via `apims-mip-01`) is the next milestone; CSV import is the
  universally-available path today.
- **txid capture is best-effort and bounded**: amounts are confirmed via
  `getreceivedbyaddress`; the reconcile/retry scan reads the last
  `MAZAPAY_TX_SCAN_LIMIT` wallet transactions (default 1000). A broadcast
  older than that window would need manual reconciliation — a dedicated
  bridge wallet keeps this window generous in practice.
- **Export crash recovery latency**: an export session claimed but never
  written (crash between claim and finalize) is adopted by a later run
  only after its lease ages past `MAZAPAY_MIP_CLAIM_ADOPT_SECONDS`
  (default 120s) — a live exporter's lease is never stolen by a
  concurrent run, so recovery is safe rather than instant.
- **Public collect routes**: `POST /orders` / `GET /orders/{id}` and the
  checkout page are unauthenticated (they only create watch addresses and
  lock quotes — nothing spends). Rate limiting on order creation is future
  work; until then, deploy behind a reverse proxy with basic throttling.
- **Single-process**: the SQLite store is safe for one service process; a
  multi-instance deployment needs a real DB layer first.
- MockChain exists so the whole flow is testable without a synced node —
  it is clearly flagged (`/health` → `chain_mode: mock`) and payment
  simulation is impossible when a real wallet is configured.

## License

MIT
