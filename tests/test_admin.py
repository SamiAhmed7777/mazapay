"""GET /admin — token-protected orders dashboard (HTML + JSON modes).

Coverage: auth (401 without/with a wrong bearer), the HTML page shape
(content type, summary chips, newest-first rows, per-order escape of
operator-controlled fields), the JSON payload shape (counts over the whole
filtered set, bounded page, filters echoed), the kind/status filters, the
limit bound + validation, and parameter refusals (bad kind/format/limit are
400, never 5xx).
"""
import json
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Tuple

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox as _env  # type: ignore

net_gate = _env.net_gate

from mazapay import api, config, store  # noqa: E402

TOKEN = "admin-dash-token"


@net_gate
class TestAdminDashboard(unittest.TestCase):
    """One shared server; every /admin behavior in both output formats."""

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

    # -- helpers --------------------------------------------------------------
    def _get(self, path: str, token: Optional[str] = TOKEN) -> Tuple[int, str, Dict[str, str]]:
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="GET", headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode() or "{}"
            exc.close()
            return exc.code, payload, dict(exc.headers)

    def _get_json(self, path: str, token: Optional[str] = TOKEN) -> Tuple[int, Any]:
        status, body, _headers = self._get(path, token)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, body

    def _order(self, usd: float = 5.0, ref: str = "admin") -> Dict[str, Any]:
        data = json.dumps({"usd_amount": usd, "ref": ref}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/orders",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            order = json.loads(resp.read().decode())
        self.assertEqual(order["status"], "awaiting")
        return order

    # -- auth -----------------------------------------------------------------
    def test_admin_requires_bearer_token(self):
        for token in (None, "wrong-token"):
            with self.subTest(token=token):
                status, body, headers = self._get("/admin", token=token)
                self.assertEqual(status, 401, body)
                self.assertIn("Authorization", body)
                # RFC 7235: the 401 must challenge, or a plain browser never
                # pops its login prompt on the dashboard.
                www = {k.lower(): v for k, v in headers.items()}.get("www-authenticate", "")
                self.assertIn('Basic realm="mazapay-admin"', www)
                self.assertIn("Bearer", www)

    def test_basic_auth_browser_flow(self):
        """Browsers prompt for Basic natively — the non-CLI operator path."""
        import base64

        good = base64.b64encode(f"operator:{TOKEN}".encode()).decode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/admin")
        req.add_header("Authorization", f"Basic {good}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("MazaPay orders", resp.read().decode())
        # wrong password refused
        bad = base64.b64encode(b"operator:not-the-token").decode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/admin")
        req.add_header("Authorization", f"Basic {bad}")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()
        # malformed base64 is a 401, never a 500
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/admin")
        req.add_header("Authorization", "Basic !!!not-base64!!!")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.fail(f"expected 401, got {resp.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
            exc.close()
        # no userinfo at all ("Basic " + empty payload) is a 401 too
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/admin")
        req.add_header("Authorization", f"Basic {base64.b64encode(b':').decode()}")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()

    # -- HTML page ------------------------------------------------------------
    def test_admin_html_shape_and_rows(self):
        a = self._order(5.0)
        b = self._order(7.0)
        status, body, headers = self._get("/admin")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("text/html"))
        self.assertIn("X-Content-Type-Options", headers)  # uniform security headers
        self.assertEqual(headers.get("Cache-Control"), "no-store")  # order data: never cached
        self.assertIn(a["id"], body)
        self.assertIn(b["id"], body)
        self.assertIn("awaiting", body)
        self.assertIn("/export/mip?kind=in", body)  # export links for operators
        self.assertIn("/export/mip?kind=out", body)
        # newest first: b (created later) appears before a
        self.assertLess(body.index(b["id"]), body.index(a["id"]))
        # inbound ids link to their public detail page
        self.assertIn(f'href="/orders/{a["id"]}"', body)

    def test_admin_html_escapes_operator_controlled_fields(self):
        # The intake regex refuses markup in refs, but the store accepts one
        # via a direct write (vendor flows / older rows) — the page must
        # escape it regardless (defense in depth, verified here).
        order = self._order(1.0)
        store.update_order(order["id"], {"ref": '<b>x & y</b>'}, "admin_escape_test")
        status, body, _ = self._get("/admin")
        self.assertEqual(status, 200)
        self.assertNotIn("<b>x & y</b>", body)
        self.assertIn("&lt;b&gt;x &amp; y&lt;/b&gt;", body)

    def test_admin_full_txid_in_title_attribute(self):
        # The visible cell truncates the txid; the full value must ride the
        # title attribute so an operator can copy it out of the page.
        order = self._order(6.0)
        txid = "9cf20369aa" + "0123456789abcdef" * 3 + "ba09d0"  # 64 hex chars
        self.assertEqual(len(txid), 64)
        store.update_order(order["id"], {"txid": txid}, "admin_txid_test")
        status, body, _ = self._get("/admin")
        self.assertEqual(status, 200)
        self.assertIn(f'title="{txid}"', body)
        # after stripping the title attribute the full txid appears nowhere
        # else — the visible cell stays truncated
        self.assertNotIn(txid, body.replace(f'title="{txid}"', ""))
        self.assertIn(txid[:16] + "…", body)  # truncated visible cell

    # -- JSON payload ---------------------------------------------------------
    def test_admin_json_shape_and_counts(self):
        for _ in range(2):
            self._order(3.0)
        status, snap = self._get_json("/admin?format=json")
        self.assertEqual(status, 200)
        self.assertEqual(snap["filters"], {"kind": "all", "status": "all", "limit": 100})
        self.assertGreaterEqual(snap["total"], 2)
        self.assertEqual(snap["shown"], len(snap["orders"]))
        self.assertLessEqual(snap["shown"], snap["total"])
        self.assertGreaterEqual(snap["counts"].get("awaiting", 0), 2)
        ids = [o["id"] for o in snap["orders"]]
        self.assertEqual(len(ids), len(set(ids)))
        created = [o["created_ts"] for o in snap["orders"]]
        self.assertEqual(created, sorted(created, reverse=True))  # newest first

    def test_admin_json_filters(self):
        inbound = self._order(4.0)
        status, snap = self._get_json("/admin?format=json&kind=in&status=awaiting")
        self.assertEqual(status, 200)
        self.assertTrue(all(o["kind"] == "in" for o in snap["orders"]))
        self.assertTrue(all(o["status"] == "awaiting" for o in snap["orders"]))
        self.assertIn(inbound["id"], [o["id"] for o in snap["orders"]])
        self.assertEqual(snap["filters"], {"kind": "in", "status": "awaiting", "limit": 100})
        # an impossible status filter is a valid (empty) query, not an error
        status, snap = self._get_json("/admin?format=json&status=nonexistent")
        self.assertEqual(status, 200)
        self.assertEqual(snap["total"], 0)
        self.assertEqual(snap["orders"], [])

    def test_admin_json_limit_bounds_page(self):
        for _ in range(3):
            self._order(2.0)
        status, snap = self._get_json("/admin?format=json&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(snap["shown"], 2)
        self.assertGreaterEqual(snap["total"], 3)  # counts cover the whole set
        # page carries the NEWEST rows of the full list
        full = self._get_json("/admin?format=json")[1]
        self.assertEqual(snap["total"], full["total"])
        self.assertEqual([o["id"] for o in snap["orders"]], [o["id"] for o in full["orders"]][:2])

    # -- parameter validation (400, never 5xx) ---------------------------------
    def test_admin_parameter_refusals(self):
        for path in (
            "/admin?format=json&kind=banana",
            "/admin?format=json&limit=0",
            "/admin?format=json&limit=-5",
            "/admin?format=json&limit=999999",
            "/admin?format=json&limit=abc",
            "/admin?format=xml",
        ):
            with self.subTest(path=path):
                status, body, _ = self._get(path)
                self.assertEqual(status, 400, body)
                self.assertIn("error", body)
