# CONTRACT — MazaPay Treasury (treasury portal v1)

Status: **pinned**. Disagreements resolve HERE, not in code comments. If a brief
and this document disagree, this document wins — implement it and flag the
conflict in your DONE report.

## 1. Purpose and scope

A treasury workspace on top of the existing MazaPay bridge: accounts, requests
(drawdowns / payouts / stipend runs), a **two-signature approval rail**, a
**donor-funded humanitarian pool**, statements + MIP-shaped CSV, and an
append-only audit trail. It is the product rendered in the concept board.

Hard boundaries (product promises, enforced in code where possible):

- It moves the **organisation's own** MAZA. It never custodies third-party money.
- **Federal award funds** (`account.kind = 'federal_oversight'`) are documentary:
  a request may be prepared, signed and authorised, but `complete` refuses with
  `409 federal_award_documentary_only`. Nothing is broadcast for those accounts.
- Money is `Decimal`, never `float`. SQLite columns for money are `TEXT`.
- stdlib only. No new pip packages, no CDN assets, no external fonts.

Non-goals for v1: multi-organisation tenancy, real bank integration, PDF
generation, email templating beyond the magic link, editing account caps from the
UI, mobile app.

## 2. Package layout and file ownership

```
mazapay/treasury/__init__.py     PARENT
mazapay/treasury/settings.py     PARENT
mazapay/treasury/server.py       PARENT   (entrypoint: python3 -m mazapay.treasury.server)
mazapay/treasury/store.py        LANE 1   schema + queries (tr_* tables), audit hash chain
mazapay/treasury/service.py      LANE 1   state machine, policies, caps, settlement calls
mazapay/treasury/statements.py   LANE 1   monthly statements, MIP-shaped CSV, payslip data
mazapay/treasury/auth.py         LANE 2   users, magic links, sessions, CSRF, roles
mazapay/treasury/http.py         LANE 3   router, CSP, static, JSON API, page dispatch
mazapay/treasury/pages_pool.py   LANE 4   pool / statements / audit / login page renderers
mazapay/templates/treasury/*.html LANE 3 owns base.html, accounts.html, request.html, users.html
                                 LANE 4 owns pool.html, statements.html, audit.html, login.html
mazapay/assets/treasury.css      LANE 3   design system (Lane 4 may APPEND a marked block)
mazapay/assets/treasury.js       LANE 4   shared client JS (POST helper, CSRF header)
deploy/treasury.service          LANE 5   staging systemd unit
scripts/treasury_seed_demo.py    LANE 5   demo org + sample requests (idempotent)
docs/treasury.md                 LANE 5   operator/developer documentation
tests/test_treasury_store.py     LANE 1
tests/test_treasury_service.py   LANE 1
tests/test_treasury_statements.py LANE 1
tests/test_treasury_auth.py      LANE 2
tests/test_treasury_http.py      LANE 3
tests/test_treasury_ui.py        LANE 4
```

**Nobody edits** `mazapay/api.py`, `mazapay/store.py`, `mazapay/config.py`,
`mazapay/pricing.py`, `mazapay/mip_export.py`, `mazapay/templates/checkout.html`,
`deploy/mazapay.service`, or another lane's files. The parent makes any change to
shared files. Lanes do **not** commit and do **not** push.

## 3. Settings (parent-owned `mazapay/treasury/settings.py`)

Read at import time from env, with these defaults:

| name | default | meaning |
|---|---|---|
| `MAZAPAY_TR_HOST` | `127.0.0.1` | bind address |
| `MAZAPAY_TR_PORT` | `18501` | bind port |
| `MAZAPAY_TR_DB` | `config.DB` | sqlite path (same file as MazaPay) |
| `MAZAPAY_TR_SESSION_TTL` | `28800` | session lifetime (s) |
| `MAZAPAY_TR_MAGIC_TTL` | `900` | magic-link lifetime (s) |
| `MAZAPAY_TR_COOKIE_SECURE` | `1` | `Secure` flag on cookies |
| `MAZAPAY_TR_BASE_URL` | `http://127.0.0.1:18501` | origin for magic links |
| `MAZAPAY_TR_DEMO_MODE` | `0` | demo banner + seed-friendly defaults |

Also expose `SESSION_COOKIE = "mazapay_tr"`, `CSRF_COOKIE = "mazapay_tr_csrf"`.

Treasury tables live in the **same sqlite file** as MazaPay and are all prefixed
`tr_`. Use `mazapay.store.connect(settings.DB)` for connections (WAL, busy
timeout, row factory are already set there).

## 4. Data model (exact tables and columns)

All `ts` columns are REAL unix seconds. All money is TEXT holding a decimal
string (`"48200.00"`). All ids are `prefix_<12 hex>` like `store._new_id`.

```sql
tr_users(id TEXT PK, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
         role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_ts REAL NOT NULL)
   role IN ('admin','executive','council_treasurer','treasurer','finance_officer','viewer')

tr_login_tokens(id TEXT PK, user_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                created_ts REAL NOT NULL, expires_ts REAL NOT NULL, used_ts REAL)

tr_sessions(id TEXT PK, user_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
            created_ts REAL NOT NULL, expires_ts REAL NOT NULL, revoked_ts REAL,
            ip TEXT, user_agent TEXT)

tr_accounts(id TEXT PK, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL,              -- 'operating' | 'drawdown' | 'federal_oversight' | 'pool'
            maza_address TEXT, status TEXT NOT NULL DEFAULT 'open',
            signature_policy TEXT NOT NULL DEFAULT 'dual',   -- 'single' | 'dual'
            cap_per_request_usd TEXT, cap_daily_usd TEXT,
            award_ref TEXT, notes TEXT, created_ts REAL NOT NULL)

tr_payees(id TEXT PK, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL,
          maza_address TEXT NOT NULL, category TEXT, notes TEXT, created_ts REAL NOT NULL)
   category IN ('vendor','health','family','elder') or NULL

tr_requests(id TEXT PK, ref TEXT UNIQUE NOT NULL,          -- DR-2027-0142 | HSR-0088
            account_id TEXT NOT NULL, kind TEXT NOT NULL,  -- 'drawdown'|'payout'|'stipend_run'
            purpose TEXT NOT NULL, award_ref TEXT, note TEXT,
            status TEXT NOT NULL,                          -- see state machine
            usd_total TEXT NOT NULL, maza_total TEXT,
            rate TEXT, rate_source TEXT, rate_locked_ts REAL, rate_expires_ts REAL,
            prepared_by TEXT NOT NULL, prepared_ts REAL NOT NULL,
            processed_by TEXT, processed_ts REAL,
            submitted_ts REAL, authorized_ts REAL, completed_ts REAL,
            decline_reason TEXT, created_ts REAL NOT NULL)

tr_request_items(id TEXT PK, request_id TEXT NOT NULL, payee_id TEXT,
                 payee_name TEXT NOT NULL, payee_address TEXT NOT NULL,
                 category TEXT, usd_amount TEXT NOT NULL, maza_amount TEXT,
                 status TEXT NOT NULL DEFAULT 'pending',   -- pending|sent|failed
                 payment_id TEXT, txid TEXT, error TEXT, sent_ts REAL,
                 created_ts REAL NOT NULL)

tr_signatures(id TEXT PK, request_id TEXT NOT NULL, user_id TEXT NOT NULL,
              role_at_signing TEXT NOT NULL, decision TEXT NOT NULL,  -- approve|decline
              note TEXT, signed_ts REAL NOT NULL, record_hash TEXT NOT NULL,
              UNIQUE(request_id, user_id, decision))

tr_pool_donations(id TEXT PK, donor_label TEXT NOT NULL, usd_amount TEXT NOT NULL,
                  maza_amount TEXT, txid TEXT, note TEXT, received_ts REAL NOT NULL,
                  recorded_by TEXT NOT NULL)

tr_audit(id INTEGER PK AUTOINCREMENT, ts REAL NOT NULL, actor_user_id TEXT,
         action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
         detail TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)
```

`tr_audit` is append-only at the schema level: `BEFORE UPDATE` / `BEFORE DELETE`
triggers must `ABORT` (mirror `mazapay/store.py`'s audit table). `hash =
sha256(prev_hash + "\n" + canonical_json({ts, actor_user_id, action, object_type,
object_id, detail}))` hex; `prev_hash` of the first row is `"0" * 64`.

Indexes: `tr_requests(account_id, status)`, `tr_requests(prepared_ts)`,
`tr_request_items(request_id)`, `tr_signatures(request_id)`,
`tr_audit(object_type, object_id)`, `tr_sessions(token_hash)`.

`store.init_db()` must be idempotent and create every table + trigger. It is
called at server start and by every test.

## 5. Roles and permissions

| action | admin | executive | council_treasurer | treasurer | finance_officer | viewer |
|---|---|---|---|---|---|---|
| read anything | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| create/process request | ✓ | – | – | ✓ | ✓ | – |
| submit for signature | ✓ | – | – | ✓ | ✓ | – |
| sign | – | ✓ (dual only) | ✓ (dual only) | ✓ (single only) | – | – |
| cancel | ✓ | – | – | ✓ | ✓ | – |
| complete | ✓ | – | – | ✓ | – | – |
| record donation | ✓ | – | – | ✓ | – | – |
| manage users | ✓ | – | – | – | – | – |

`admin` may read everything but **may not sign** (keeps the audit story clean).

## 6. State machine (pinned)

```
prepared --process--> processed --submit--> pending_signatures
pending_signatures --sign(quorum)--> authorized --complete--> completed | completed_partial
prepared|processed|pending_signatures --cancel--> cancelled
pending_signatures --sign(decline)--> declined
```

- `process` locks the quote: `rate`, `rate_source`, `rate_locked_ts`,
  `rate_expires_ts` (now + `config.RATE_LOCK_SECONDS`), and per-item
  `maza_amount`. Uses `pricing.quote_maza(usd)` — the same call the checkout uses.
- `submit` requires ≥1 item and a non-expired quote.
- `sign`: `policy=single` → one `approve` from role `treasurer`;
  `policy=dual` → one `approve` from `executive` **and** one from
  `council_treasurer`. Quorum reached → `authorized`.
- Separation of duties: the user who prepared or processed a request may not
  sign it, even if their role qualifies (`separation_of_duties`).
- A signature by a role the policy does not call for is recorded but does not
  count toward quorum (return `counted: false`).
- `decline` by any qualifying signer ends the request (`declined`,
  `decline_reason` required).
- `complete`: for each `pending` item call
  `mazapay.api.disburse({"address": <item address>, "usd_amount": <item usd>,
  "ref": "<request ref>"})`; store `payment_id` (the returned order id) and
  `txid`; mark `sent`. An exception marks that item `failed` with `error` and
  the request ends `completed_partial` (retryable). Never broadcast twice for an
  item that has a `txid`.
- `complete` re-checks: account `cap_daily_usd` (sum of today's completed +
  this request), pool balance for `stipend_run`, and the federal guard.

## 7. Service API (`service.py`) — signatures lanes 3/4 code against

```python
class TreasuryError(Exception):
    code: str          # machine code, e.g. 'insufficient_role'
    status: int        # HTTP status the layer should return

def list_accounts(user) -> list[dict]
def get_account(user, account_id) -> dict
def create_request(user, *, account_id, kind, purpose, items, award_ref=None, note=None) -> dict
def get_request(user, request_id) -> dict          # request + items + signatures + audit
def list_requests(user, *, status=None, account_id=None, limit=100) -> list[dict]
def process_request(user, request_id) -> dict
def submit_request(user, request_id) -> dict
def sign_request(user, request_id, *, decision, note=None) -> dict
def cancel_request(user, request_id, *, reason=None) -> dict
def complete_request(user, request_id) -> dict
def pool_summary(user) -> dict
def record_donation(user, *, donor_label, usd_amount, txid=None, note=None) -> dict
def list_stipends(user, *, limit=100) -> list[dict]
def audit_rows(user, *, object_type=None, object_id=None, limit=200) -> list[dict]
```

`user` is the dict returned by `auth.current_user(...)`: `{id, email, name, role}`.

## 8. Auth API (`auth.py`)

```python
def init_db() -> None                       # creates tables if called directly
def bootstrap_admin(email, name) -> dict    # idempotent; used by the seed script
def create_user(admin_user, *, email, name, role) -> dict
def list_users(user) -> list[dict]
def deactivate_user(admin_user, user_id) -> dict
def request_magic_link(email, base_url) -> dict   # always {"sent": True}; returns link only in demo mode
def consume_magic_link(token) -> dict | None      # returns {user, session_token} once
def create_session(user_id, ip=None, user_agent=None) -> str
def resolve_session(token) -> dict | None         # {user, session}
def revoke_session(token) -> None
def new_csrf_token() -> str
def check_csrf(session, cookie_token, header_token) -> bool
```

Rules: tokens are `secrets.token_urlsafe(32)`, stored as `sha256` hex; compares
are `hmac.compare_digest`; every login attempt is appended to `tr_audit`
(`action='login_requested'|'login_failed'|'login_ok'`); no user enumeration
(unknown email behaves exactly like a known one); magic link is single-use.

## 9. HTTP surface (`http.py`)

Pages (HTML, session cookie; unauthenticated → `303 /treasury/login`):

```
GET  /                     -> 303 /treasury/accounts
GET  /treasury/login       -> login page
POST /treasury/login       -> form {email}; always 303 /treasury/login?sent=1
GET  /treasury/auth/{tok}  -> consume link, Set-Cookie session+csrf, 303 /treasury/accounts
POST /treasury/logout      -> revoke, clear cookies, 303 /treasury/login
GET  /treasury/accounts
GET  /treasury/requests/{id}
GET  /treasury/pool
GET  /treasury/statements?kind=requests|pool&month=YYYY-MM
GET  /treasury/audit?object_type=&object_id=
GET  /treasury/users                       (admin only)
GET  /treasury/payslips/{item_id}          (printable, any signed-in user)
GET  /static/{name}                        whitelist only
GET  /healthz                              {"ok": true, "mode": ...}
```

JSON API (session + `X-CSRF-Token` on POST):

```
GET  /api/me                 -> {"user": {...}, "csrf": "..."}
GET  /api/accounts           -> {"accounts": [ ... ]}
GET  /api/requests           -> {"requests": [ ... ]}
POST /api/requests           -> {"request": {...}}   201
GET  /api/requests/{id}      -> {"request": {...}, "items": [...], "signatures": [...], "audit": [...]}
POST /api/requests/{id}/process|submit|cancel|complete
POST /api/requests/{id}/sign -> {"decision": "approve"|"decline", "note": "..."}
GET  /api/pool               -> {"balance_maza": "...", "balance_usd": "...", "donated_month_usd": "...",
                                 "paid_week_usd": "...", "donors": 37, "categories": [{"category": "health", "usd": "..."}]}
POST /api/pool/donations     -> {"donation": {...}}  201
GET  /api/pool/stipends      -> {"stipends": [ ... ]}
GET  /api/statements?kind=&month=&format=json|csv
GET  /api/audit?object_type=&object_id=&limit=
GET  /api/users              (admin)  |  POST /api/users  |  POST /api/users/{id}/deactivate
```

Error shape (every non-2xx JSON response): `{"error": {"code": "...", "message": "..."}}`
with `TreasuryError.code` / `.status` passed through. Unknown route → 404 with
the same shape. Body limit 1 MiB. All HTML output escaped with `html.escape`;
CSP is `default-src 'none'; style-src 'self' 'nonce-<n>'; script-src 'self'
'nonce-<n>'; img-src 'self'; form-action 'self'; frame-ancestors 'none';
base-uri 'none'`, plus `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`.

`/static/` whitelist: `treasury.css`, `treasury.js`, `mazacoin-180.png`,
`mazacoin-wordmark.png`, `mazacoin-accepted.png` (served from `mazapay/assets/`).

## 10. Page/UI contract

Design tokens are MazaPay's own, copied verbatim from `checkout.html`:
`--bg:#0b0d12; --surface:#151b24; --surface-2:#0d1117; --line:#242c39;
--line-strong:#5f6d8c; --text:#eef1f6; --muted:#9aa4b6; --amber:#f0a000;
--amber-soft:#ffc45c; --ink:#101318; --red:#f00000; --red-text:#ff7d7d;
--radius:18px`. Treasury adds `--ok:#4ecb8a; --ok-tint:#0f2419;
--amber-tint:#241b08; --red-tint:#2a1212; --faint:#7b8698`.

Required CSS classes (Lane 3 defines, Lane 4 uses): `.shell .topbar .brand .org
.mode .user`, `.nav .nav-on .badge`, `.page .ptitle .sub .actions`, `.btn
.btn-primary .btn-ghost .btn-sm`, `.card .card-h`, `.tbl .n .mono`, `.pill
.pill-ok .pill-amber .pill-red .pill-mut`, `.banner .banner-strong`, `.kpis .kpi`,
`.cats .cat .bar`, `.chain .step .step-done .step-now .step-lock`, `.note`,
`.cols2`, `.sigrow`, `.auditrow`, `.faint`.

Accessibility is a test, not an opinion: `tests/test_treasury_ui.py` parses the
shipped `treasury.css` and every text/surface pair must be ≥ 4.5:1 (≥ 3:1 for
borders/bars). Follow `tests/test_checkout_ui.py`'s computed-matrix approach.

No inline scripts; `treasury.js` is loaded with the CSP nonce and must degrade to
nothing (pages are readable without JS).

## 11. Statements, CSV and payslips

`GET /api/statements?kind=requests&month=2026-11` → rows for every request whose
`prepared_ts` falls in the UTC month: `ref, date, account_code, account_name,
kind, status, usd, maza, rate, rate_source, txids[], signers[], purpose`.

`format=csv` returns the same rows as `text/csv; charset=utf-8` with header
`REF,DATE,ACCOUNT,KIND,STATUS,USD,MAZA,RATE,RATE_SOURCE,TXIDS,SIGNERS,DESCRIPTION`
and every cell neutralised exactly like `mip_export._safe_cell` (leading `'` on
`= + - @`), because these files are opened in MIP/Excel.

Payslip page `/treasury/payslips/{item_id}` must show: request ref, recipient
name, category, USD amount, MAZA amount, rate + rate source, txid, date, the
sentence “USD value at receipt”, and reconcile against the audit trail; print
CSS hides the nav.

## 12. Tests

- `python3 -m unittest discover -s tests -v` must pass, and the pre-existing
  suites must stay green (they are, at base commit).
- Treasury tests import `mazapay.treasury.*` and use the `tests/_sandbox.py`
  settings already applied at import time (temp DB).
- Each lane writes a test class per behaviour group; no network; no sleeps longer
  than 1 s; no fixed ports (bind port 0 and read the socket).
- Money: assert on decimal strings, never floats.

## 13. Definition of done for each lane

A lane is done when: its files exist, its own tests pass, the full suite passes,
`git status --short` shows only its own paths (plus nothing else), and the DONE
report lists files, test names, and every deviation from this contract.
