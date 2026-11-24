# Treasury evidence bundle

Generated 2026-11-24T10:33:06Z. Each transcript is the output of a command in this file, and
each carries a sha256 manifest of every file under `mazapay/` and `tests/` plus
the two treasury scripts, so a reviewer can match bytes to claims.

## suite.txt — full suite, writable temp dir

```bash
python3 -m unittest discover -s tests -v
```

## readonly_sim.txt — the reviewer's environment

```bash
TMPDIR=/nonexistent-readonly-dir MAZAPAY_TEST_TMP=/nonexistent-readonly-dir \
python3 -m unittest discover -s tests
```

No writable temp dir and no sockets: `tests/_sandbox.py` reports
`WRITABLE=False`/`SOCKET_OK=False`, the affected classes are decorated
`@requires_writable` / `@net_gate` and skip, and the command still exits 0.
`tests/test_sandbox_gates.py::TestReadOnlySuite` re-runs exactly this check on
every suite run, so an ungated test cannot come back unnoticed.

## e2e_staging.txt — live HTTP flow, shipped script

```bash
python3 scripts/treasury_seed_demo.py --db /tmp/treasury-demo.sqlite3
MAZAPAY_TR_DB=/tmp/treasury-demo.sqlite3 MAZAPAY_DB=/tmp/treasury-demo.sqlite3 \
MAZAPAY_TR_DEMO_MODE=1 MAZAPAY_TR_COOKIE_SECURE=0 MAZAPAY_PRICE_SOURCE=fixed \
MAZAPAY_FIXED_USD_PRICE=0.00125 MAZAPAY_MAX_DISBURSE_USD=100000 \
python3 -m mazapay.treasury.server

# second shell — same database, demo mode
MAZAPAY_TR_DB=/tmp/treasury-demo.sqlite3 MAZAPAY_TR_DEMO_MODE=1 \
python3 scripts/treasury_e2e_staging.py
```

The flow: a 2-of-2 request goes `pending_signatures` -> `authorized` (council
signature) -> `completed` (treasurer `complete`), printing the quorum, the final
status, the audit actions and the pool balance.

Teardown: `kill $(ss -ltnp | grep 18501 | grep -o 'pid=[0-9]*' | cut -d= -f2)` and
delete the demo database; re-seeding restores the pristine demo state.

## On the exact test count

The counts above were produced by Python 3.14.9 on this host. Discovery is per test
file, so a different interpreter may report a different figure if a
platform-gated test is added; what must hold is `OK` and exit 0 in both the
writable and the read-only runs, and `tests/test_sandbox_gates.py` enforces the
read-only one on every run.
