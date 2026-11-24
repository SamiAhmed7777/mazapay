# MazaPay Treasury

The treasury workspace is the **organisation-side** half of MazaPay: the screens
a nation's finance office uses to hold MAZA, approve spending, sign off in pairs,
run elder stipends from a donor pool, and hand an auditor a provable trail. The
bridge (`docs/user-guide.md`) collects money from members and pays vendors one
payment at a time; the treasury decides *who gets paid, by whom, and on whose
authority* — and then calls the bridge to settle.

Pinned contract: `docs/CONTRACT-treasury.md`. If this page and the contract
disagree, the contract wins and this page is the bug.

---

## 1. What it is — and the three promises it keeps

**It is the organisation's own money.** Every account in the workspace belongs to
the organisation running it. It never custodies third-party funds, never holds a
donor's MAZA on their behalf, and never acts as an exchange. Donations are
*recorded* into the humanitarian pool, not taken into safekeeping for a return.

**Federal award funds are documentary.** Accounts of kind `federal_oversight`
(e.g. `PL 93-638` — Indian Self-Determination) can hold requests that are
prepared, signed and authorised — because the paper trail is the point — but
`complete` refuses with `409 federal_award_documentary_only`. Nothing is ever
broadcast from those accounts. A signature there authorises a book entry, not a
payment.

**Money is Decimal, always.** Every amount is a `decimal.Decimal` in code and a
`TEXT` decimal string in sqlite (`"48200.00"`), never a `float`. Rates are
locked per request and stored with their source. This is what makes the USD value
of a MAZA payment defensible under IRS Notice 2014-21 / ASU 2023-08.

Two supporting promises: **stdlib only** (no pip packages, no CDN, no external
fonts — the whole portal is Python's stdlib plus the assets in the repo), and
**no external assets** (CSS/JS/logos are served from `mazapay/assets/`, which is
also why the CSP can be `default-src 'none'`).

---

## 2. Architecture

```
        browser ──TLS──> Caddy (vhost, :443)
                            │  proxy → 127.0.0.1:18501
                            ▼
                  ┌──────────────────────────────┐
                  │ mazapay-treasury (systemd)   │   python3 -m mazapay.treasury.server
                  │ mazapay/treasury/http.py     │   HTML pages + /api/* JSON
                  │   ├─ auth.py    (magic link, │
                  │   │             sessions)    │
                  │   ├─ service.py (state       │
                  │   │   machine, caps, duties) │
                  │   ├─ statements.py (CSV)     │
                  │   └─ store.py   (tr_* + chain)│
                  └───────────┬──────────────────┘
                              │ same sqlite file (WAL)
                              ▼
        /var/lib/mazapay/mazapay.sqlite3
          orders, vendors, audit   ← the bridge's tables
          tr_users, tr_accounts, tr_requests, tr_request_items,
          tr_signatures, tr_pool_donations, tr_audit, tr_sessions …
                              ▲
                  ┌───────────┴──────────────────┐
                  │ mazapay (bridge, :18500)     │  python3 -m mazapay.api
                  └──────────────────────────────┘
```

Facts worth knowing before you touch anything:

- **Its own process, its own port.** The treasury binds `127.0.0.1:18501`; the
  bridge keeps `127.0.0.1:18500`. They are separate services with separate
  restart behaviour — a treasury bug cannot stop collections.
- **One database file.** Treasury tables share the bridge's sqlite file and are
  all prefixed `tr_`. A completed drawdown writes a real `orders` row through
  `mazapay.api.disburse(...)`, so statements can join request items to
  disbursements. Connections come from `mazapay.store.connect()` (WAL, 15 s busy
  timeout, row factory) — never `sqlite3.connect()` directly.
- **Append-only audit with a hash chain.** Every transition appends to
  `tr_audit`; `hash = sha256(prev_hash + "\n" + canonical_json({ts,
  actor_user_id, action, object_type, object_id, detail}))` and the first row's
  `prev_hash` is 64 zeros. `BEFORE UPDATE` / `BEFORE DELETE` triggers `ABORT`, so
  history cannot be rewritten by any caller — including this codebase. Rewriting
  it with triggers dropped still breaks the chain, which the audit page and
  `store.verify_chain()` report.
- **Process-safe writes.** Service mutations run inside
  `store.write_db()` (`BEGIN IMMEDIATE`), so the row change and its audit append
  commit together — an interrupted request can never be half-recorded.

---

## 3. Who can do what

| action | admin | executive | council_treasurer | treasurer | finance_officer | viewer |
|---|---|---|---|---|---|---|
| read anything | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| create / process a request | ✓ | – | – | ✓ | ✓ | – |
| submit for signature | ✓ | – | – | ✓ | ✓ | – |
| sign | – | ✓ (dual only) | ✓ (dual only) | ✓ (single only) | – | – |
| cancel | ✓ | – | – | ✓ | ✓ | – |
| complete (settle) | ✓ | – | – | ✓ | – | – |
| record a donation | ✓ | – | – | ✓ | – | – |
| manage users | ✓ | – | – | – | – | – |

Two rules sit on top of the table:

- **`admin` cannot sign.** Administration and authority are separated so the
  audit story survives a compromised admin account.
- **Separation of duties.** Whoever prepared or processed a request may not sign
  it, even if their role qualifies (`separation_of_duties`). A signature from a
  role the account's policy does not call for is *recorded* but returns
  `counted: false` — visible in the trail, no effect on quorum.

Signature policy is per account: `single` (one `treasurer`) or `dual` (one
`executive` **and** one `council_treasurer`). Caps are per request and per day,
in USD, and are checked at `complete` against today's settled total.

---

## 4. Request lifecycle

```
                     create (finance_officer / treasurer)
                                     │
                                     ▼
   ┌────────────┐   process    ┌───────────┐   submit    ┌────────────────────┐
   │  prepared  │─────────────▶│ processed │────────────▶│ pending_signatures │
   └─────┬──────┘   locks the  └─────┬─────┘             └──────┬─────────────┘
         │           quote           │                          │
         │                           │            sign (quality quorum)
         │                           │              │            │
         │                           │      approve │            │ decline
         │                           │              ▼            ▼
         │                           │        ┌────────────┐  ┌──────────┐
         │                           │        │ authorized │  │ declined │
         │                           │        └──────┬─────┘  └──────────┘
         │                           │               │ complete (broadcast per item)
         │                           │               ▼
         │                           │     ┌──────────────────┐   any item failed
         │                           │     │    completed     │   ───────────────▶ completed_partial
         │                           │     └──────────────────┘        (retryable)
         └─────────────── cancel ─────┴──────────────────────────────▶ cancelled
```

Step by step, with what the operator sees:

1. **prepared** — items (payee, USD amount) and a purpose. No rate yet.
2. **process** — locks the quote: `rate`, `rate_source`, `rate_locked_ts`,
   `rate_expires_ts = now + MAZAPAY_RATE_LOCK_SECONDS` (default 900 s), and each
   item's `maza_amount`, using `pricing.quote_maza()` — the same call the member
   checkout uses, so treasury and checkout can never disagree about a day's rate.
3. **submit** — requires at least one item and a live (non-expired) quote. Moves
   to **pending_signatures**.
4. **sign** — quorum per the account policy, minus separation of duties.
   A decline ends the request with a required reason (**declined**).
5. **complete** — re-checks the federal guard, the account's daily cap and (for
   stipend runs) the pool balance, then calls
   `mazapay.api.disburse({"address": …, "usd_amount": …, "ref": ref})` per
   pending item, storing `payment_id` + `txid` and marking the item `sent`.
   One failing item marks *that* item `failed` with its error and leaves the
   request **completed_partial** — retry `complete` and only the unsent items
   are attempted. An item that already has a `txid` is never broadcast twice.
6. **cancel** — allowed while prepared, processed or pending_signatures.

Statements (`/treasury/statements`) read the same rows the state machine wrote;
`format=csv` emits the MIP-shaped columns with `mip_export._safe_cell`'s
anti-formula prefixing, because those files get opened in Excel.

---

## 5. Run it locally

Ten lines of local work — no wallet, no mail, no network:

```bash
# 1  a throwaway database plus demo data (see §6)
export MAZAPAY_TR_DB=/tmp/treasury-demo.sqlite3
python3 scripts/treasury_seed_demo.py --db "$MAZAPAY_TR_DB"

# 2  serve it (demo mode; no mail is sent, so see step 3 for signing in)
MAZAPAY_TR_DEMO_MODE=1 MAZAPAY_TR_COOKIE_SECURE=0 \
  python3 -m mazapay.treasury.server          # http://127.0.0.1:18501

# 3  mint a sign-in link (no mail is sent today) and open it in the browser
MAZAPAY_TR_DB="$MAZAPAY_TR_DB" MAZAPAY_TR_DEMO_MODE=1 python3 -c \
  "from mazapay.treasury import auth; print(auth.request_magic_link('admin@example.org','http://127.0.0.1:18501')['link'])"
curl -s localhost:18501/healthz                # {"ok": true, "mode": "demo", …}
```

`MAZAPAY_TR_COOKIE_SECURE=0` is required for plain-HTTP localhost (a `Secure`
cookie is not sent over `http://`). The magic link is single-use and expires in
`MAZAPAY_TR_MAGIC_TTL` (900 s), so mint it when you are ready to sign in. Every
knob comes from env; the full table is in `settings.py`, and the ones that matter
locally are:

| variable | default | meaning |
|---|---|---|
| `MAZAPAY_TR_HOST` / `MAZAPAY_TR_PORT` | `127.0.0.1` / `18501` | listener |
| `MAZAPAY_TR_DB` | bridge `config.DB` | sqlite path — the shared file |
| `MAZAPAY_TR_BASE_URL` | `http://127.0.0.1:18501` | origin used to build magic links |
| `MAZAPAY_TR_DEMO_MODE` | `0` | demo banner + magic link returned in-process |
| `MAZAPAY_TR_COOKIE_SECURE` | `1` | `Secure` on the session/CSRF cookies |
| `MAZAPAY_TR_SESSION_TTL` / `MAZAPAY_TR_MAGIC_TTL` | `28800` / `900` | lifetimes (s) |

Quotes use the bridge's price feed, so an offline run needs
`MAZAPAY_PRICE_SOURCE=fixed MAZAPAY_FIXED_USD_PRICE=0.00125` (the seeder sets
these for itself and the demo server command inherits whatever is in your
environment).

---

## 6. Demo data (the concept board, reproducible)

`scripts/treasury_seed_demo.py` builds exactly what the concept board shows, is
idempotent, and refuses to touch anything that smells like production:

```bash
python3 scripts/treasury_seed_demo.py --db /tmp/treasury-demo.sqlite3
python3 scripts/treasury_seed_demo.py                    # honours MAZAPAY_TR_DB
```

What it creates (all keyed by stable ids, so a second run changes nothing):

- six users — admin (`MAZAPAY_TR_ADMIN_EMAIL`, default `admin@example.org`) plus
  `executive@`, `council.treasurer@`, `treasurer@`, `finance.officer@`,
  `viewer@example.org`;
- five accounts — `OPS-CASH` *USD cash leg* (operating, status
  `banking_disrupted`, i.e. dormant while the bank leg is disrupted), `OPS-GEN`
  *General Fund* (single signature), `DRW-ROADS` *Roads & Facilities* (dual, caps
  $75k/request, $150k/day), `FED-HHS` *Health & Human Services* (federal
  oversight, `PL 93-638`), `POOL-HUM` *Humanitarian Pool*;
- four payees (vendor, health, family, elder);
- six pool donations totalling **10,300.00 USD** at **0.00125 USD/MAZA**
  (8,240,000.00 MAZA);
- `DR-2027-0141` — completed dual-signed drawdown, $30,500.00, items `sent` with
  mock disbursement ids/txids;
- `DR-2027-0142` — pending_signatures, **$48,200.00 / 38,560,000.00 MAZA**,
  executive signed, council treasurer pending;
- `HSR-0088` — prepared stipend run, 12 elder stipends at $250.00 = $3,000.00
  (no rate locked yet, as `prepared` should be);
- `PS-0138` … `PS-0141` — four completed elder-stipend payslips in the pool.

Guards: a resolved path containing `/var/lib/mazapay` requires
`--i-know-this-is-not-production`; a database already holding bridge orders with
`kind='in'` (i.e. real collections) requires `--force`. Both exit non-zero
without writing anything.

It seeds **through the shipped service layer** when that layer is importable
(so the rows are the ones the state machine produces, mock chain and audit chain
included) and falls back to contract-shaped inserts, reporting what it skipped,
when a module is missing. The summary it prints ends with the exact server
command and the request/payslip URLs it created.

---

## 7. Tests

```bash
python3 -m unittest discover -s tests -v            # the whole suite
python3 -m unittest tests.test_treasury_service -v  # one module
```

Treasury tests are offline by construction: `tests/_sandbox.py` points
`MAZAPAY_DB` at a temp directory and forces `MAZAPAY_PRICE_SOURCE=fixed`, the
mock chain is used when `MAZAPAY_MAZAD_RPC` is empty, servers bind port 0, and
classes skip cleanly when the sandbox has no writable temp dir or no sockets. If
your environment cannot write temp files, set `MAZAPAY_TEST_TMP=/some/writable/dir`.

The suites that matter here: `test_treasury_store.py` (schema, audit chain,
append-only triggers), `test_treasury_service.py` (state machine, quorum,
separation of duties, caps, federal guard, completion), `test_treasury_statements.py`
(statement rows, CSV neutralisation, payslip data), `test_treasury_auth.py`
(magic-link single use, sessions, CSRF, no user enumeration),
`test_treasury_http.py` (routes, error shape, CSP, body limit) and
`test_treasury_ui.py` (contrast ratios parsed from the shipped CSS).

---

## 8. Security posture

- **Login is a magic link.** `POST /treasury/login` always answers the same way
  (`303 …?sent=1`) whether or not the address exists — no user enumeration. The
  token is `secrets.token_urlsafe(32)`, stored only as `sha256` hex, single-use,
  valid `MAZAPAY_TR_MAGIC_TTL` (900 s) and rate-limited per address and per IP;
  every attempt lands in `tr_audit` as `login_requested` / `login_failed` /
  `login_ok` / `login_rate_limited`.
- **Sessions** are random tokens, `sha256`-hashed at rest, with an absolute TTL
  and an explicit revoke on logout. Cookie `mazapay_tr` (plus `mazapay_tr_csrf`)
  is `HttpOnly`, `SameSite=Lax`, and `Secure` unless `MAZAPAY_TR_COOKIE_SECURE=0`
  for plain-HTTP localhost.
- **CSRF**: every POST requires the `X-CSRF-Token` header (or the paired form
  field) matching the CSRF cookie for the session; comparisons are
  `hmac.compare_digest`, never `==`.
- **CSP**: `default-src 'none'` with per-response nonces for the local stylesheet
  and script, plus `nosniff`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy: same-origin` and `frame-ancestors 'none'`. No
  inline script, no CDN, no external font, no `data:` image.
- **No external assets**: `/static/{name}` is an exact-filename whitelist served
  from inside the package; the URL segment is never joined onto a path.
- **Money paths are server-side only.** The browser never computes a rate, an
  amount or a quorum; it posts an intent and the service decides.
- **The listener is loopback-bound** and must stay behind the TLS proxy: the
  session cookie is `Secure`, magic links are built from `MAZAPAY_TR_BASE_URL`,
  and there is no rate limiter in the app worth trusting on a public port.

---

## 9. Staging deployment

Operator actions, in order. Nothing here is done by the release lane — the unit
ships **uninstalled**.

1. **Stage the repo** (the unit runs `python3 -m mazapay.treasury.server`, so the
   *repo root* must be the working directory):

   ```bash
   rsync -a --delete --exclude .git --exclude __pycache__ --exclude '*.sqlite3' \
       /path/to/mazapay/ root@staging:/opt/mazapay/current/
   ```

2. **Ensure the service user and data dir exist** (the bridge unit's installer
   already does this; do not re-create with different ownership):

   ```bash
   id mazapay || useradd --system --home /var/lib/mazapay --shell /usr/sbin/nologin mazapay
   install -d -o mazapay -g mazapay -m 0750 /var/lib/mazapay
   ```

3. **Write the env file** — `0600`, owned by root, read by the unit:

   ```bash
   install -m 0600 -o root -g root /dev/null /etc/mazapay-treasury.env
   cat >/etc/mazapay-treasury.env <<'EOF'
   MAZAPAY_TR_BASE_URL=https://treasury.staging.example.org
   MAZAPAY_TR_DEMO_MODE=1
   MAZAPAY_TR_ADMIN_EMAIL=finance@example.org
   MAZAPAY_TR_ADMIN_NAME=Finance Office
   # the concept-board drawdown exceeds the bridge's $10k per-payment default
   MAZAPAY_MAX_DISBURSE_USD=100000
   EOF
   chmod 0600 /etc/mazapay-treasury.env
   ```

   `MAZAPAY_MAX_DISBURSE_USD` is a *policy* value: the bridge caps one
   disbursement at $10k by default, and a drawdown item larger than that will
   come back `failed` and leave the request `completed_partial`. Size it to the
   organisation's real per-payment ceiling, or split items.

4. **Install and enable the unit**:

   ```bash
   install -m 0644 deploy/treasury.service /etc/systemd/system/mazapay-treasury.service
   systemctl daemon-reload
   systemctl enable --now mazapay-treasury
   systemctl status mazapay-treasury --no-pager
   ```

5. **Seed demo data** on staging (staging database only — never production):

   ```bash
   cd /opt/mazapay/current
   sudo -u mazapay MAZAPAY_TR_DB=/var/lib/mazapay/mazapay.sqlite3 \
       python3 scripts/treasury_seed_demo.py            # add --force if the DB already has real orders
   ```

6. **Caddy vhost** (TLS terminated here; the app stays on loopback):

   ```caddy
   treasury.staging.example.org {
       encode zstd gzip
       header {
           Strict-Transport-Security "max-age=31536000; includeSubDomains"
           X-Content-Type-Options "nosniff"
           -Server
       }
       # never expose /healthz publicly beyond a liveness check
       @health path /healthz
       handle @health {
           respond 404
       }
       reverse_proxy 127.0.0.1:18501 {
           header_up X-Forwarded-For {remote_host}
       }
       log {
           output file /var/log/caddy/treasury.log
       }
   }
   ```

   `caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`.

7. **Verify**:

   ```bash
   curl -s http://127.0.0.1:18501/healthz          # {"ok":true,"mode":"demo","service":"mazapay-treasury"}
   curl -sI https://treasury.staging.example.org/treasury/login | head -20
   journalctl -u mazapay-treasury -n 50 --no-pager
   ```

   `mode` reads `demo` while `MAZAPAY_TR_DEMO_MODE=1` and `live` once it is 0.

Do not point a staging treasury at a database the bridge is *collecting* into
unless you accept demo rows in it: the seeder refuses to run when it finds real
`kind='in'` orders, but the server itself will happily serve whatever is there.

---

## 10. Known limits (read before promising anything)

- **No production mail delivery.** Magic links are generated, audited and
  single-use, but nothing sends email yet. In demo mode `auth.request_magic_link`
  returns the link in-process (the HTTP login handler currently discards it, so
  today you mint a link with the one-liner in §5); in live mode it is logged with
  the token redacted. A real deployment needs an SMTP (or SES-style) sender wired
  into `auth.request_magic_link`, or links issued out-of-band by an administrator.
- **No bank integration.** The USD cash leg is a label, not a bank feed. Balances,
  statements and MIP CSVs describe MAZA movements and their locked USD value;
  reconciling to a bank statement is manual.
- **No PDF payslips.** `/treasury/payslips/{item_id}` is printable HTML with
  print CSS. Print to PDF from the browser; there is no server-side generator.
- **No account or cap editing UI.** Accounts, caps and policies are seeded or
  written at the store level. Changing a cap is an operator/database task today,
  with an audit entry expected but not automated.
- **Single organisation.** One install serves one nation/organisation. There is
  no tenancy column and no cross-organisation isolation; two organisations mean
  two installs.
- **Staging-only deployment.** The unit ships uninstalled, the demo seeder is the
  intended way to fill it, and there is no migration story for an existing
  production database beyond `store.init_db()` creating tables.
- **A stipend run is a request, not a batch job.** `HSR-*` is one request with
  many items; per-item retry exists, but there is no scheduling, no payroll
  calendar and no recurring run.
- **One process, one sqlite file.** WAL + `BEGIN IMMEDIATE` is correct for one
  writer process per service. It is not a multi-instance design.
- **2027 reference years in demo data** are illustrative only; refs are free-form
  text (`DR-2027-0142`), not derived from the system clock.## Completion safety

Two mechanisms make settlement safe when more than one request is in flight, or
when the process dies mid-run.

**Spend reservations are atomic with the completion claim.** Marking a request
`completing` and taking its hold on the account's daily cap (and, for stipend
runs, on the pool balance) happen in one `BEGIN IMMEDIATE` transaction against
`tr_reservations`. Two different authorized requests therefore cannot both pass
a cap check and broadcast: the second sees the first's hold. Holds are settled
when the run completes and released if nothing was sent. A crashed run's hold is
re-claimable after `COMPLETION_LEASE_SECONDS` (300 s), and the re-claim
refreshes it.

**Every item carries a stable bridge reference before it is broadcast.** The
treasury writes `broadcast_ref = "treasury:<request ref>:<item suffix>"` onto the
item, then calls the bridge with that ref. On any later attempt (retry, crash
recovery, lease expiry) the treasury first looks for an outbound order with that
ref:

- order `sent` with a txid → adopt it, record `payment_id`/`txid`, never re-send;
- order `pending_send` / `send_uncertain` → `api.reconcile_payment()`, then adopt
  or mark the item `send_uncertain` (operator decides, the product never retries
  an uncertain send);
- order `send_failed` without a txid → `api.retry_payment()` once, then adopt.

The practical recovery procedure for an operator after a hard stop is therefore:
re-run `complete` on the request. Items that already reached the chain are
adopted, items that never left are sent, and no item can be paid twice. Items
left `send_uncertain` are resolved with the bridge's own reconcile path
(`POST /payments/{id}/reconcile`), which is why the treasury records
`payment_id` on the item whenever it has one.


