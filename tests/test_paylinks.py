"""Payment links (PAYLINK) end-to-end sweep (token-auth mode, mock chain).

A link is a front door to the existing order machinery: these tests exercise
the merchant CRUD surface (POST/GET /links, deactivate), the public /pay page
(active / paid / unavailable states, escaping, hygiene) and the buyer
checkout that funnels into the SAME order flow the checkout page uses —
settlement is driven by the existing simulate_payment + watcher path, and the
link status observes it via the paylinks status-sync hook.

Mirrors tests/test_smoke.py's server pattern: one shared HTTP server over a
temp store, net-gated so restricted sandboxes skip cleanly.
"""
import itertools
import json
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox as _env  # type: ignore

net_gate = _env.net_gate

from mazapay import api, config, store  # noqa: E402

TOKEN = "paylinks-test-token"


@net_gate
class TestPaymentLinks(unittest.TestCase):
    """Merchant link CRUD + public pay page + buyer checkout + settlement."""

    # One fresh per-IP cap slot per test instance; see setUp.
    _CAP_SLOTS = itertools.count(1)

    @classmethod
    def setUpClass(cls):
        store.init_db()
        cls._old_token = config.API_TOKEN
        config.API_TOKEN = TOKEN
        cls.srv = api.serve("127.0.0.1", 0, with_watcher=False)
        cls.port = cls.srv.server_address[1]
        cls.pool = ThreadPoolExecutor(max_workers=1)
        cls.pool.submit(cls.srv.serve_forever)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        api.shutdown_watcher()
        cls.pool.shutdown(wait=True)
        config.API_TOKEN = cls._old_token

    def setUp(self):
        """A fresh client IP per test: intake caps key on X-Forwarded-For's
        first hop (the deploy binds loopback behind a proxy) and the suite's
        store is shared by every module in the process, and one test below
        parks orders AT the per-IP cap - so each test takes its own slot from
        a counter. Not hash(): str hashing is salted per process, so two tests
        could draw one bucket and the second's checkout got 429 (GitHub run
        45905355750)."""
        n = next(self._CAP_SLOTS) - 1
        self._client_ip = f"10.250.{n // 255}.{n % 255 + 1}"

    # -- helpers --------------------------------------------------------------
    def _ip(self) -> str:
        """This test's own client IP (one cap slot per test; see setUp)."""
        return self._client_ip

    def _req_headers(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        token: Optional[str] = TOKEN,
    ) -> Tuple[int, Any, Dict[str, str]]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "X-Forwarded-For": self._ip()}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw), dict(resp.headers)
                except ValueError:
                    return resp.status, raw, dict(resp.headers)  # HTML page
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode() or "{}"
            exc.close()
            try:
                return exc.code, json.loads(payload), {}
            except ValueError:
                return exc.code, payload, {}

    def _req(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        token: Optional[str] = TOKEN,
    ) -> Tuple[int, Any]:
        status, payload, _headers = self._req_headers(method, path, body, token)
        return status, payload

    def _make_link(self, usd: str = "5.00", memo: str = "", **extra: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {"usd_amount": usd, "memo": memo}
        body.update(extra)
        status, link = self._req("POST", "/links", body)
        self.assertEqual(status, 201, link)
        return link

    # -- auth matrix -----------------------------------------------------------
    def test_auth_matrix(self):
        # merchant routes refuse anonymous callers (existing auth behavior)
        for method, path, body in (
            ("POST", "/links", {"usd_amount": "5.00"}),
            ("GET", "/links", None),
            ("POST", "/links/some-link/deactivate", {}),
        ):
            status, err = self._req(method, path, body, token=None)
            self.assertEqual(status, 401, f"{method} {path}: {err}")
            self.assertIn("error", err)
            status, err = self._req(method, path, body, token="wrong-token")
            self.assertEqual(status, 401, f"{method} {path} (bad token): {err}")
        # same routes with the token: 201/200
        status, link = self._req("POST", "/links", {"usd_amount": "5.00"})
        self.assertEqual(status, 201)
        status, listing = self._req("GET", "/links")
        self.assertEqual(status, 200)
        self.assertTrue(any(l["link_id"] == link["link_id"] for l in listing["links"]))
        status, body = self._req("POST", f"/links/{link['link_id']}/deactivate", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "deactivated")

    # -- happy path: page -> checkout -> settle -> paid ------------------------
    def test_happy_path_checkout_settles_and_flips_link(self):
        status, link = self._req(
            "POST", "/links", {"usd_amount": "25.50", "memo": "Invoice 42 teakettle"}
        )
        self.assertEqual(status, 201)
        lid = link["link_id"]
        self.assertEqual(link["status"], "active")
        self.assertEqual(link["usd_amount"], "25.50")
        self.assertEqual(link["memo"], "Invoice 42 teakettle")
        self.assertTrue(link["pay_url"].endswith(f"/pay/{lid}"), link["pay_url"])

        # public page: amount + memo + the served form's endpoint
        status, page = self._req("GET", f"/pay/{lid}", token=None)
        self.assertEqual(status, 200)
        self.assertIsInstance(page, str)
        self.assertIn("25.50", page)
        self.assertIn("Invoice 42 teakettle", page)
        self.assertIn(f"/pay/{lid}/checkout", page)

        # buyer checkout through the served endpoint (POST /orders shape);
        # body values are IGNORED — the order carries the link's amount/memo
        status, order = self._req(
            "POST",
            f"/pay/{lid}/checkout",
            {"usd_amount": "999.00", "ref": "buyer-rewritten"},
            token=None,
        )
        self.assertEqual(status, 201, order)
        self.assertEqual(order["status"], "awaiting")
        self.assertEqual(order["usd_amount"], 25.5)  # the LINK's amount
        self.assertEqual(order["ref"], "Invoice 42 teakettle")  # the LINK's memo
        self.assertTrue(order["address"])

        # link stays active until the order SETTLES (order_id is bound)
        status, listing = self._req("GET", "/links")
        row = [l for l in listing["links"] if l["link_id"] == lid][0]
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["order_id"], order["id"])

        # settlement via the EXISTING simulate + watcher path
        status, sim = self._req("POST", f"/orders/{order['id']}/simulate_payment", {})
        self.assertEqual(status, 200, sim)
        status, counts = self._req("POST", "/watch", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(counts["paid"], 1, counts)

        # /pay now shows the paid banner; the listing shows paid + order_id
        status, page = self._req("GET", f"/pay/{lid}", token=None)
        self.assertEqual(status, 200)
        self.assertIn("already been paid", page)
        self.assertNotIn(f"/pay/{lid}/checkout", page)  # no pay form left
        status, listing = self._req("GET", "/links")
        row = [l for l in listing["links"] if l["link_id"] == lid][0]
        self.assertEqual(row["status"], "paid")
        self.assertEqual(row["order_id"], order["id"])

    # -- validation -------------------------------------------------------------
    def test_validation_hostile_amounts_are_400(self):
        for bad in ("NaN", "-5", "0", "1.234", "0.001", "abc", "12\x0012"):
            status, err = self._req("POST", "/links", {"usd_amount": bad})
            self.assertEqual(status, 400, f"usd_amount={bad!r}: {err}")
            self.assertIn("error", err)
        # missing amount
        status, err = self._req("POST", "/links", {})
        self.assertEqual(status, 400, err)
        # non-string amounts (a JSON number is already a float here)
        for bad in (5.0, 5, True, {"x": 1}, [1]):
            status, err = self._req("POST", "/links", {"usd_amount": bad})
            self.assertEqual(status, 400, f"usd_amount={bad!r}: {err}")

    def test_validation_memo_cap_and_types(self):
        # over the documented 200-char cap -> 400 (never truncated silently)
        status, err = self._req("POST", "/links", {"usd_amount": "5.00", "memo": "x" * 201})
        self.assertEqual(status, 400, err)
        self.assertIn("memo", err["error"])
        status, err = self._req("POST", "/links", {"usd_amount": "5.00", "memo": "x" * 10000})
        self.assertEqual(status, 400, err)
        # non-string memo is a 400, never a 500
        status, err = self._req("POST", "/links", {"usd_amount": "5.00", "memo": {"x": 1}})
        self.assertEqual(status, 400, err)
        # exactly at the cap is accepted
        status, link = self._req("POST", "/links", {"usd_amount": "5.00", "memo": "x" * 200})
        self.assertEqual(status, 201, link)

    def test_unicode_memo_stored_verbatim(self):
        # hostile-input rule: unicode text must be accepted (not 400/500) and
        # stored verbatim; the /pay page renders it escaped
        memo = "café ☕ — 日本語"
        status, link = self._req("POST", "/links", {"usd_amount": "5.00", "memo": memo})
        self.assertEqual(status, 201, link)
        self.assertEqual(link["memo"], memo)
        status, page = self._req("GET", f"/pay/{link['link_id']}", token=None)
        self.assertEqual(status, 200)
        self.assertIn(memo, page)

    # -- idempotency -------------------------------------------------------------
    def test_idempotency_key_replays_same_link(self):
        # unique per run: the module must stay re-runnable against a shared
        # store (a fixed literal would replay an old pass's link with 200)
        key = f"paylinks-idem-{uuid.uuid4().hex[:12]}"
        s1, l1 = self._req(
            "POST",
            "/links",
            {"usd_amount": "5.00", "memo": "idem", "idempotency_key": key},
        )
        self.assertEqual(s1, 201, l1)
        # same key, different body: the ORIGINAL link comes back with 200
        s2, l2 = self._req(
            "POST",
            "/links",
            {"usd_amount": "9.00", "memo": "changed", "idempotency_key": key},
        )
        self.assertEqual(s2, 200)
        self.assertEqual(l1["link_id"], l2["link_id"])
        self.assertEqual(l2["usd_amount"], "5.00")
        self.assertEqual(l2["memo"], "idem")

    # -- states -------------------------------------------------------------------
    def test_deactivated_link_shows_unavailable_and_refuses_orders(self):
        link = self._make_link("7.00", "state-deact")
        lid = link["link_id"]
        status, body = self._req("POST", f"/links/{lid}/deactivate", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "deactivated")
        # deactivating again is an idempotent 200
        status, body = self._req("POST", f"/links/{lid}/deactivate", {})
        self.assertEqual(status, 200)
        status, page = self._req("GET", f"/pay/{lid}", token=None)
        self.assertEqual(status, 200)
        self.assertIn("no longer available", page)
        self.assertNotIn(f"/pay/{lid}/checkout", page)  # NO pay form
        status, err = self._req("POST", f"/pay/{lid}/checkout", {}, token=None)
        self.assertEqual(status, 409, err)

    def test_expired_link_shows_unavailable_and_refuses_orders(self):
        link = self._make_link("7.00", "state-expire")
        lid = link["link_id"]
        # TTL injection: expires_at in the past (same-format UTC ISO string)
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
        store.update_link(lid, {"expires_at": past}, "test_expire")
        status, page = self._req("GET", f"/pay/{lid}", token=None)
        self.assertEqual(status, 200)
        self.assertIn("no longer available", page)
        self.assertNotIn(f"/pay/{lid}/checkout", page)  # NO pay form
        status, err = self._req("POST", f"/pay/{lid}/checkout", {}, token=None)
        self.assertEqual(status, 409, err)
        # an expired link can still be deactivated (cleanup)
        status, body = self._req("POST", f"/links/{lid}/deactivate", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "deactivated")

    def test_paid_link_cannot_be_deactivated(self):
        link = self._make_link("3.00", "state-paid-deact")
        lid = link["link_id"]
        _s, order = self._req("POST", f"/pay/{lid}/checkout", {}, token=None)
        self._req("POST", f"/orders/{order['id']}/simulate_payment", {})
        self._req("POST", "/watch", {})
        status, err = self._req("POST", f"/links/{lid}/deactivate", {})
        self.assertEqual(status, 409, err)
        # unknown link id is a 404
        status, err = self._req("POST", "/links/no-such-link/deactivate", {})
        self.assertEqual(status, 404, err)

    # -- public page hygiene -------------------------------------------------------
    def test_public_page_hygiene_and_xss_escaping(self):
        xss = "<script>alert(1)</script>"
        status, link = self._req("POST", "/links", {"usd_amount": "12.00", "memo": xss})
        self.assertEqual(status, 201)
        lid = link["link_id"]
        status, page, headers = self._req_headers("GET", f"/pay/{lid}", token=None)
        self.assertEqual(status, 200)
        # memo rendered ESCAPED, never as a live tag
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)", page)
        # a hostile memo must not reach the order ref either (neutral tag)
        status, order = self._req("POST", f"/pay/{lid}/checkout", {}, token=None)
        self.assertEqual(status, 201, order)
        self.assertEqual(order["ref"], "paylink")
        # nothing about the merchant surface leaks to the public page
        self.assertNotIn("Authorization", page)
        self.assertNotIn(TOKEN, page)
        self.assertNotIn("/admin", page)
        self.assertNotIn("vendor", page.lower())
        # same security headers as the checkout page
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))

    def test_unknown_link_id_is_json_404(self):
        status, err = self._req("GET", "/pay/does-not-exist", token=None)
        self.assertEqual(status, 404)
        self.assertIsInstance(err, dict)  # JSON, no HTML leak
        self.assertIn("error", err)
        status, err = self._req("POST", "/pay/does-not-exist/checkout", {}, token=None)
        self.assertEqual(status, 404)
        self.assertIn("error", err)

    def test_link_ids_are_unguessable(self):
        ids = set()
        for _ in range(3):
            _s, link = self._req("POST", "/links", {"usd_amount": "1.00"})
            ids.add(link["link_id"])
            self.assertGreaterEqual(len(link["link_id"]), 22)
            self.assertRegex(link["link_id"], r"^[A-Za-z0-9_-]+$")
        self.assertEqual(len(ids), 3)

    # -- buyer flow respects the existing intake caps ------------------------------
    def test_checkout_respects_existing_intake_caps(self):
        link = self._make_link("1.00", "caps")
        lid = link["link_id"]
        made = 0
        last_status = None
        for _ in range(config.MAX_AWAITING_ORDERS_PER_IP + 1):
            last_status, _order = self._req("POST", f"/pay/{lid}/checkout", {}, token=None)
            if last_status == 429:
                break
            self.assertEqual(last_status, 201)  # until the per-IP cap trips
            made += 1
        self.assertEqual(
            last_status,
            429,
            f"per-IP awaiting cap not enforced after {made} checkouts",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
