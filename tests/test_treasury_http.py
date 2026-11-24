"""HTTP-layer tests for the treasury portal (Lane 3).

Covers the contract §9 surface that can be proved without a browser:

- transport   — unauthenticated pages 303 to /treasury/login, /healthz is open,
                unknown routes and wrong methods come back as the JSON error
                envelope, the 1 MiB body cap answers 413 before buffering;
- headers     — CSP carries a fresh nonce and no 'unsafe-inline', plus nosniff,
                no-referrer and COOP on every response;
- static      — the exact-filename whitelist serves treasury.css as text/css and
                refuses traversal, absolute and NUL-shaped names;
- api          — 401 without a session, 403 with a wrong CSRF token, 200 with
                both, TreasuryError status/code passed through;
- pages       — accounts/request/users screens render for a signed-in viewer,
                user data is escaped (XSS), the four-step rail maps the pinned
                state machine, and a payslip renders from a completed item.

Sessions are minted directly with the auth helpers — no magic-link round trip —
and the server binds port 0 on a daemon thread.
"""
import http.client
import json
import os
import re
import socket
import unittest
import urllib.parse

try:
    import _sandbox as _env
except ImportError:  # package-style invocation
    from tests import _sandbox as _env  # type: ignore

net_gate = _env.net_gate

from mazapay import store as bridge_store  # noqa: E402
from mazapay.treasury import auth, http as tr_http, settings, store  # noqa: E402
from mazapay.treasury import service  # noqa: E402


def _text(body: bytes) -> str:
    return body.decode("utf-8", "replace")


@net_gate
class TreasuryHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bridge_store.init_db()
        store.init_db()
        auth.init_db()

        cls.admin = auth.bootstrap_admin("admin@treasury.test", "Ada Admin")
        cls.treasurer = auth.create_user(
            cls.admin, email="treasurer@treasury.test", name="Tess Treasurer", role="treasurer"
        )
        cls.executive = auth.create_user(
            cls.admin, email="exec@treasury.test", name="Eli Executive", role="executive"
        )
        cls.council = auth.create_user(
            cls.admin, email="council@treasury.test", name="Cora Council", role="council_treasurer"
        )
        cls.viewer = auth.create_user(
            cls.admin, email="viewer@treasury.test", name="Vic Viewer", role="viewer"
        )
        cls.finance = auth.create_user(
            cls.admin, email="finance@treasury.test", name="Flo Finance", role="finance_officer"
        )

        cls.account = store.create_account(
            "Operating", "OPS-USD", "operating",
            maza_address="MZopSaddress0001", signature_policy="dual",
            cap_per_request_usd="50000", cap_daily_usd="200000",
        )
        cls.disrupted = store.create_account(
            "Cash leg", "CASH-USD", "operating",
            maza_address="MZcash0001", status="banking_disrupted",
        )
        cls.drawdown = store.create_account(
            "Drawdown", "DRW-USD", "drawdown", status="locked",
            signature_policy="dual", cap_per_request_usd="25000",
        )
        cls.federal = store.create_account(
            "Federal award", "FED-OVS", "federal_oversight",
            award_ref="AWD-2027-01", status="open",
        )

        # One full lifecycle: prepared -> processed -> pending_signatures ->
        # authorized -> completed, with a real (mock-chain) txid on the item.
        cls.purpose = "Emergency health stipend <script>alert(1)</script>"
        request = service.create_request(
            cls.treasurer,
            account_id=cls.account["id"],
            kind="payout",
            purpose=cls.purpose,
            items=[{
                "payee_name": "Jo Payee",
                "payee_address": "MZpayee0001",
                "usd_amount": "100.00",
                "category": "health",
            }],
        )
        cls.request_id = request["id"]
        cls.request_ref = request["ref"]
        service.process_request(cls.treasurer, cls.request_id)
        service.submit_request(cls.treasurer, cls.request_id)
        service.sign_request(cls.executive, cls.request_id, decision="approve")
        service.sign_request(cls.council, cls.request_id, decision="approve")
        service.complete_request(cls.treasurer, cls.request_id)
        detail = service.get_request(cls.admin, cls.request_id)
        cls.item_id = detail["items"][0]["id"]
        cls.txid = detail["items"][0]["txid"]

        # A second request left at 'prepared' so the rail's early state is
        # covered too.
        prepared = service.create_request(
            cls.finance,
            account_id=cls.account["id"],
            kind="payout",
            purpose="Vendor invoice",
            items=[{"payee_name": "Acme", "payee_address": "MZacme0001", "usd_amount": "250.00"}],
        )
        cls.prepared_id = prepared["id"]

        # A third request parked at 'pending_signatures' so the Accounts screen's
        # "Needs your signature" card can be proved to list a real request for a
        # qualifying signer.
        waiting = service.create_request(
            cls.treasurer,
            account_id=cls.account["id"],
            kind="payout",
            purpose="Waiting on the Council",
            items=[{"payee_name": "Sam Signer", "payee_address": "MZsign0001", "usd_amount": "75.00"}],
        )
        cls.waiting_id = waiting["id"]
        cls.waiting_ref = waiting["ref"]
        service.process_request(cls.treasurer, cls.waiting_id)
        service.submit_request(cls.treasurer, cls.waiting_id)

        cls.server, cls.thread = tr_http.start("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    # -- helpers -------------------------------------------------------------
    @classmethod
    def _session(cls, user):
        token = auth.create_session(user["id"])
        info = auth.resolve_session(token)
        assert info is not None, "session did not resolve"
        return token, info["session"]["csrf"]

    def _call(self, method, path, *, session=None, csrf=None, form=None, json_body=None,
              body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if session:
            cookie = f"{settings.SESSION_COOKIE}={session}"
            if csrf:
                cookie += f"; {settings.CSRF_COOKIE}={csrf}"
            hdrs["Cookie"] = cookie
        if csrf:
            hdrs["X-CSRF-Token"] = csrf
        payload = None
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif form is not None:
            payload = urllib.parse.urlencode(form).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            payload = body
        hdrs.update(headers or {})
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
            header_list = response.getheaders()
        finally:
            conn.close()
        merged = {key.lower(): value for key, value in header_list}
        return status, merged, raw, header_list

    def _json(self, method, path, **kwargs):
        status, headers, raw, _ = self._call(method, path, **kwargs)
        return status, headers, json.loads(_text(raw) or "{}")

    # -- transport -----------------------------------------------------------
    def test_healthz_is_open_and_json(self):
        status, headers, body = self._json("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["content-type"])
        self.assertTrue(body["ok"])
        self.assertIn(body["mode"], ("demo", "live"))

    def test_unauthenticated_page_redirects_to_login(self):
        for path in ("/treasury/accounts", "/treasury/requests", "/treasury/pool",
                     "/treasury/statements", "/treasury/audit", "/treasury/users",
                     f"/treasury/requests/{self.request_id}"):
            with self.subTest(path=path):
                status, headers, _raw, _ = self._call("GET", path)
                self.assertEqual(status, 303)
                self.assertEqual(headers["location"], "/treasury/login")

    def test_root_redirects_to_accounts(self):
        status, headers, _raw, _ = self._call("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/treasury/accounts")

    def test_unknown_route_is_json_404(self):
        status, headers, body, _ = self._call("GET", "/treasury/nope")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["content-type"])
        payload = json.loads(_text(body))
        self.assertEqual(set(payload["error"]), {"code", "message"})
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_wrong_method_is_json_405(self):
        status, _headers, body, _ = self._call("GET", "/api/pool/donations")
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(_text(body))["error"]["code"], "method_not_allowed")

    def test_body_cap_rejects_before_buffering(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(
                b"POST /api/requests HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\nContent-Length: 2097152\r\n\r\n"
            )
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        response = b"".join(chunks).decode("latin-1")
        self.assertIn("413", response.split("\r\n", 1)[0])
        self.assertIn("payload_too_large", response)

    # -- headers / CSP -------------------------------------------------------
    def test_csp_has_fresh_nonce_and_no_unsafe_inline(self):
        status, headers, body, _ = self._call("GET", "/treasury/login")
        self.assertEqual(status, 200)
        csp = headers["content-security-policy"]
        self.assertNotIn("unsafe-inline", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("form-action 'self'", csp)
        nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
        self.assertIsNotNone(nonce, csp)
        assert nonce is not None  # narrowing for the type checker
        document = _text(body)
        self.assertIn(f'nonce="{nonce.group(1)}"', document)
        # A second response must carry a different nonce.
        _s2, headers2, _b2, _ = self._call("GET", "/treasury/login")
        self.assertNotEqual(headers["content-security-policy"],
                            headers2["content-security-policy"])

    def test_hardening_headers_on_every_response(self):
        for path in ("/healthz", "/static/treasury.css", "/treasury/login"):
            with self.subTest(path=path):
                _status, headers, _raw, _ = self._call("GET", path)
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["referrer-policy"], "no-referrer")
                self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")
                self.assertIn("content-security-policy", headers)

    # -- static whitelist ----------------------------------------------------
    def test_static_serves_css(self):
        status, headers, body, _ = self._call("GET", "/static/treasury.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["content-type"])
        self.assertIn(b"--amber:#f0a000", body.replace(b" ", b""))

    def test_static_refuses_unknown_and_hostile_names(self):
        names = [
            "/static/nope.css",
            "/static/../mazapay/api.py",
            "/static/%2e%2e%2fapi.py",
            "/static//etc/passwd",
            "/static/..%2fapi.py",
            "/static/treasury.css%00.png",
            "/static/",
        ]
        for path in names:
            with self.subTest(path=path):
                status, _headers, body, _ = self._call("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"import", body)

    # -- JSON API ------------------------------------------------------------
    def test_api_requires_session(self):
        status, headers, body, _ = self._call("GET", "/api/accounts")
        self.assertEqual(status, 401)
        self.assertIn("application/json", headers["content-type"])
        payload = json.loads(_text(body))
        self.assertEqual(payload["error"]["code"], "not_authenticated")

    def test_api_post_requires_csrf(self):
        token, csrf = self._session(self.treasurer)
        status, _headers, body, _ = self._call(
            "POST", "/api/requests",
            session=token,
            json_body={"account_id": self.account["id"], "kind": "payout",
                       "purpose": "no csrf", "items": []},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(_text(body))["error"]["code"], "csrf_failed")
        # Wrong token, right cookie shape: still refused.
        status2, _h, body2, _ = self._call(
            "POST", "/api/requests",
            session=token, csrf="not-the-token",
            json_body={"account_id": self.account["id"], "kind": "payout",
                       "purpose": "bad csrf", "items": []},
        )
        self.assertEqual(status2, 403)
        self.assertIn("csrf_failed", _text(body2))
        # And the right pair is accepted far enough to reach validation.
        status3, _h, _b, _ = self._call(
            "POST", "/api/requests", session=token, csrf=csrf,
            json_body={"account_id": self.account["id"], "kind": "payout",
                       "purpose": "valid csrf", "items": []},
        )
        self.assertEqual(status3, 400)

    def test_api_me_and_accounts(self):
        token, csrf = self._session(self.viewer)
        status, _headers, body = self._json("GET", "/api/me", session=token)
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "viewer")
        self.assertIn("csrf", body)

        status, _headers, body = self._json("GET", "/api/accounts", session=token)
        self.assertEqual(status, 200)
        self.assertIsInstance(body["accounts"], list)
        self.assertTrue(any(a["id"] == self.account["id"] for a in body["accounts"]))

    def test_api_creates_request_with_201(self):
        token, csrf = self._session(self.treasurer)
        status, _headers, body = self._json(
            "POST", "/api/requests", session=token, csrf=csrf,
            json_body={
                "account_id": self.account["id"],
                "kind": "payout",
                "purpose": "API-created payout",
                "items": [{"payee_name": "Api Payee", "payee_address": "MZapi0001",
                           "usd_amount": "10.00"}],
            },
        )
        self.assertEqual(status, 201)
        self.assertTrue(body["request"]["ref"])
        self.assertEqual(body["request"]["usd_total"], "10.00")

    def test_api_error_shape_passes_treasury_error(self):
        token, _csrf = self._session(self.viewer)
        status, _headers, body = self._json("GET", "/api/requests/req_doesnotexist", session=token)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        self.assertTrue(body["error"]["message"])

    def test_api_statement_csv(self):
        token, _csrf = self._session(self.viewer)
        status, headers, body, _ = self._call("GET", "/api/statements?kind=requests&format=csv",
                                              session=token)
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["content-type"])
        first = _text(body).splitlines()[0]
        self.assertEqual(
            first,
            "REF,DATE,ACCOUNT,KIND,STATUS,USD,MAZA,RATE,RATE_SOURCE,TXIDS,SIGNERS,DESCRIPTION",
        )

    def test_api_users_admin_only(self):
        viewer_token, _ = self._session(self.viewer)
        status, _h, body = self._json("GET", "/api/users", session=viewer_token)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "insufficient_role")

        admin_token, _ = self._session(self.admin)
        status, _h, body = self._json("GET", "/api/users", session=admin_token)
        self.assertEqual(status, 200)
        self.assertTrue(any(u["email"] == "viewer@treasury.test" for u in body["users"]))

    # -- pages ---------------------------------------------------------------
    def test_accounts_page_renders_for_signed_in_viewer(self):
        token, _ = self._session(self.viewer)
        status, _headers, body, _ = self._call("GET", "/treasury/accounts", session=token)
        self.assertEqual(status, 200)
        document = _text(body)
        self.assertIn("Accounts", document)
        self.assertIn("Needs your signature", document)
        self.assertIn("Operating", document)
        self.assertIn("Banking access disrupted", document)
        self.assertIn("Locked · awaiting Council", document)
        self.assertIn("/static/treasury.css", document)
        self.assertIn("/static/treasury.js", document)

    def _signature_card(self, document: str) -> str:
        start = document.find("Needs your signature")
        self.assertGreater(start, -1)
        end = document.find("</section>", start)
        return document[start:end]

    def test_accounts_page_marks_needs_signature_for_signer_only(self):
        token, _ = self._session(self.executive)
        status, _headers, body, _ = self._call("GET", "/treasury/accounts", session=token)
        self.assertEqual(status, 200)
        document = _text(body)
        self.assertIn("Needs your signature", document)
        self.assertIn(self.waiting_ref, self._signature_card(document))
        # The treasurer prepared and submitted it, so separation of duties
        # keeps it out of their own signature card.
        treasurer_token, _ = self._session(self.treasurer)
        _s, _h, treasurer_body, _ = self._call("GET", "/treasury/accounts", session=treasurer_token)
        self.assertNotIn(self.waiting_ref, self._signature_card(_text(treasurer_body)))

    def test_request_page_escapes_user_data(self):
        token, _ = self._session(self.viewer)
        status, _headers, body, _ = self._call(
            "GET", f"/treasury/requests/{self.request_id}", session=token
        )
        self.assertEqual(status, 200)
        document = _text(body)
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", document)
        self.assertIn(self.request_ref, document)
        self.assertIn("Prepared", document)
        self.assertIn("Authorized (2-of-2)", document)
        self.assertIn("Completed", document)
        self.assertIn("Audit trail", document)
        self.assertIn(self.txid, document)
        # Completed request: every rail step is done, none is "now".
        self.assertEqual(document.count("step step-done"), 4)
        self.assertNotIn("step step-now", document)

    def test_prepared_request_rail_marks_second_step(self):
        token, _ = self._session(self.viewer)
        status, _headers, body, _ = self._call(
            "GET", f"/treasury/requests/{self.prepared_id}", session=token
        )
        self.assertEqual(status, 200)
        document = _text(body)
        self.assertEqual(document.count("step step-done"), 1)
        self.assertIn("step step-now", document)
        self.assertIn("step step-lock", document)
        # Role-aware actions are rendered server-side for a finance officer.
        finance_token, _ = self._session(self.finance)
        _s, _h, finance_body, _ = self._call(
            "GET", f"/treasury/requests/{self.prepared_id}", session=finance_token
        )
        self.assertIn("/process", _text(finance_body))
        # A viewer sees no action button for the same request.
        self.assertNotIn("/process", document)

    def test_federal_request_shows_oversight_note(self):
        request = service.create_request(
            self.treasurer,
            account_id=self.federal["id"],
            kind="drawdown",
            purpose="Award documentary draw",
            items=[{"payee_name": "Award", "payee_address": "MZawd0001", "usd_amount": "5.00"}],
        )
        token, _ = self._session(self.viewer)
        _status, _h, body, _ = self._call("GET", f"/treasury/requests/{request['id']}", session=token)
        document = _text(body)
        self.assertIn("federal award funds: oversight trail only", document)

    def test_payslip_renders_for_signed_in_viewer(self):
        token, _ = self._session(self.viewer)
        status, _headers, body, _ = self._call(
            "GET", f"/treasury/payslips/{self.item_id}", session=token
        )
        self.assertEqual(status, 200)
        document = _text(body)
        self.assertIn("Payslip", document)
        self.assertIn("Jo Payee", document)
        self.assertIn("USD value at receipt", document)
        self.assertIn(self.txid, document)
        self.assertIn("/static/treasury.css", document)
        with open(os.path.join(tr_http._ASSETS_DIR, "treasury.css"), encoding="utf-8") as fh:
            self.assertIn("@media print", fh.read())

    def test_users_page_is_admin_only(self):
        viewer_token, _ = self._session(self.viewer)
        status, _headers, _body, _ = self._call("GET", "/treasury/users", session=viewer_token)
        self.assertEqual(status, 403)

        admin_token, _ = self._session(self.admin)
        status, _h, body, _ = self._call("GET", "/treasury/users", session=admin_token)
        self.assertEqual(status, 200)
        document = _text(body)
        self.assertIn("Add a user", document)
        self.assertIn("viewer@treasury.test", document)

    def test_login_form_requires_csrf(self):
        status, _headers, _body, _ = self._call(
            "POST", "/treasury/login", form={"email": "someone@treasury.test"}
        )
        self.assertEqual(status, 403)

    def test_login_page_renders_without_a_session(self):
        status, _headers, body, _ = self._call("GET", "/treasury/login")
        self.assertEqual(status, 200)
        self.assertIn("MazaPay", _text(body))

    def test_magic_link_token_is_never_logged(self):
        # The access-log path is masked before it reaches the logger.
        self.assertEqual(
            tr_http._mask_path("/treasury/auth/secret-token-value"),
            "/treasury/auth/[redacted]",
        )
        self.assertEqual(tr_http._mask_path("/treasury/accounts"), "/treasury/accounts")

    def test_logout_clears_cookies(self):
        token, csrf = self._session(self.viewer)
        status, _headers, _body, raw_headers = self._call(
            "POST", "/treasury/logout", session=token, csrf=csrf, form={"csrf": csrf}
        )
        self.assertEqual(status, 303)
        cookies = [value for key, value in raw_headers if key.lower() == "set-cookie"]
        self.assertTrue(any(settings.SESSION_COOKIE in c and "Max-Age=0" in c for c in cookies))
        # The cookie no longer resolves.
        self.assertIsNone(auth.resolve_session(token))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
