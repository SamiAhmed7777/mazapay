#!/usr/bin/env python3
"""End-to-end staging proof for the MazaPay treasury, over HTTP.

Drives a RUNNING treasury server (never in-process) through the real flow:

  1. GET /healthz
  2. verify the audit hash chain
  3. magic-link sign-in for admin, council treasurer and treasurer
  4. council signature -> a 2-of-2 request moves to `authorized`
  5. treasurer `complete` -> items settle, request `completed`
  6. re-read the request (status, signature count, txid) and its audit rows
  7. print the pool balance after settlement

Usage::

    # terminal 1 (seeded demo state)
    python3 scripts/treasury_seed_demo.py --db /tmp/treasury-demo.sqlite3
    MAZAPAY_TR_DB=/tmp/treasury-demo.sqlite3 MAZAPAY_DB=/tmp/treasury-demo.sqlite3 \
    MAZAPAY_TR_DEMO_MODE=1 MAZAPAY_TR_COOKIE_SECURE=0 MAZAPAY_PRICE_SOURCE=fixed \
    MAZAPAY_FIXED_USD_PRICE=0.00125 MAZAPAY_MAX_DISBURSE_USD=100000 \
    python3 -m mazapay.treasury.server

    # terminal 2
    MAZAPAY_TR_DB=/tmp/treasury-demo.sqlite3 python3 scripts/treasury_e2e_staging.py

The script mutates the demo database (it settles one request). Re-run the seeder
against a fresh file to restore the pristine demo state; stop the server with the
PID printed by `ss -ltnp | grep 18501`.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def client(base: str, email: str):
    """Sign in over HTTP with a demo-mode magic link; return (opener, csrf)."""
    from mazapay.treasury import auth  # same process DB, minting only

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    result = auth.request_magic_link(email, base)
    link = result.get("link")
    if not link:
        raise SystemExit(
            "This proof needs demo mode: run it with MAZAPAY_TR_DEMO_MODE=1 and the"
            " server's MAZAPAY_TR_DB, so the magic link is handed back instead of"
            " being mailed. See the usage block in this file."
        )
    opener.open(base + link.replace(base, "")).read()
    csrf = next(c.value for c in jar if c.name == "mazapay_tr_csrf")
    return opener, csrf


def call(opener, csrf, base, method, path, body=None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["X-CSRF-Token"] = csrf
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with opener.open(req) as res:
        return res.status, json.loads(res.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("MAZAPAY_TR_BASE_URL", "http://127.0.0.1:18501"))
    ap.add_argument("--ref", default="DR-2027-0142", help="request ref to settle")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    with urllib.request.urlopen(base + "/healthz", timeout=10) as res:
        print("healthz:", res.read().decode())

    from mazapay.treasury import store

    print("audit chain:", store.verify_chain())

    admin, acsrf = client(base, "admin@example.org")
    _, listing = call(admin, acsrf, base, "GET", "/api/requests")
    rows = listing.get("requests") or listing.get("items") or []
    rid = next(r["id"] for r in rows if r["ref"] == args.ref)
    print("request:", args.ref, rid)

    council, ccsrf = client(base, "council.treasurer@example.org")
    st, body = call(council, ccsrf, base, "POST", f"/api/requests/{rid}/sign", {"decision": "approve"})
    req_body = body.get("request") or body
    print("council sign:", st, "| status:", req_body.get("status"),
          "| quorum:", body.get("quorum") or req_body.get("quorum"))

    treasurer, tcsrf = client(base, "treasurer@example.org")
    st, body = call(treasurer, tcsrf, base, "POST", f"/api/requests/{rid}/complete", {})
    print("complete:", st, "| status:", (body.get("request") or body).get("status"))
    for item in body.get("items", []):
        print("  item:", item.get("payee_name"), item.get("usd_amount"),
              item.get("status"), "txid:", (item.get("txid") or "")[:20])

    st, body = call(treasurer, tcsrf, base, "GET", f"/api/requests/{rid}")
    req = body.get("request") or body
    print("final:", req.get("status"), "| signatures:", len(body.get("signatures") or []),
          "| completed_ts:", bool(req.get("completed_ts")))

    st, body = call(treasurer, tcsrf, base, "GET", f"/api/audit?object_type=request&object_id={rid}&limit=10")
    rows = body.get("audit") or body.get("rows") or []
    print("audit:", [r["action"] for r in rows], "(%d rows)" % len(rows))

    st, body = call(treasurer, tcsrf, base, "GET", "/api/pool")
    print("pool:", body.get("balance_usd"), "USD |", body.get("donors"), "donors")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:  # a real refusal is evidence too
        print("HTTP", exc.code, exc.read().decode()[:400])
        raise SystemExit(1)
