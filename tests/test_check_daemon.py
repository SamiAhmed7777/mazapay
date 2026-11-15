"""Offline tests for check_daemon — fake RPC callers + local HTTP servers."""
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox as _env  # type: ignore

_age_claims = _env._age_claims
requires_writable = _env.requires_writable
from mazapay.mazad import MazadRPC  # noqa: E402  (used by TestCredsInUrl)
from mazapay import check_daemon as cd


def all_good_caller(method, *params):
    if method == "getblockchaininfo":
        return {"chain": "main", "blocks": 850000}
    if method == "getnewaddress":
        return "MZCfakeAddressForChecks"
    if method == "getreceivedbyaddress":
        return 0.0
    if method == "listtransactions":
        return []
    if method == "getbalance":
        return 12.5
    if method == "validateaddress":
        return {"isvalid": True, "address": params[0] if params else ""}
    if method == "sendtoaddress":
        # probe uses invalid args; a real daemon rejects with a param error
        raise cd.CheckError("Invalid amount (-1)", cd.CheckError.OTHER)
    raise AssertionError(f"unexpected method {method}")


@requires_writable
class TestRunChecks(unittest.TestCase):
    def test_all_pass_is_ready(self):
        outcome = cd.run_checks(all_good_caller)
        self.assertTrue(outcome["ready"])
        by_name = {c["check"]: c for c in outcome["checks"]}
        for name in ("daemon_reachable_and_authenticated", "wallet_loaded",
                     "receive_tracking", "transaction_history", "send_capability"):
            self.assertTrue(by_name[name]["ok"], name)
        self.assertEqual(by_name["wallet_balance"]["detail"], "balance=12.5")

    def test_validateaddress_missing_blocks_ready(self):
        """The bridge calls validateaddress before every payment - the checker
        must not report READY when that RPC is unavailable."""
        def no_validate(method, *params):
            if method == "validateaddress":
                raise cd.CheckError("Method not found", cd.CheckError.METHOD_MISSING)
            return all_good_caller(method, *params)
        outcome = cd.run_checks(no_validate)
        self.assertFalse(outcome["ready"])
        va = {c["check"]: c for c in outcome["checks"]}["validateaddress"]
        self.assertFalse(va["ok"])
        self.assertIn("before every payment", va["fix"])

    def test_zero_balance_warns_but_ready(self):
        def zero_balance(method, *params):
            if method == "getbalance":
                return 0.0
            return all_good_caller(method, *params)
        outcome = cd.run_checks(zero_balance)
        self.assertTrue(outcome["ready"])
        balance = {c["check"]: c for c in outcome["checks"]}["wallet_balance"]
        self.assertIn("balance is 0", balance["detail"])
        self.assertIn("vendor payments need MAZA", balance["fix"])

    def test_unreachable_stops_early(self):
        def dead(method, *params):
            raise cd.CheckError("connection refused", cd.CheckError.UNREACHABLE)
        outcome = cd.run_checks(dead)
        self.assertFalse(outcome["ready"])
        self.assertEqual(len(outcome["checks"]), 1)  # stopped at the first blocker
        self.assertIn("Is mazad running", outcome["checks"][0]["fix"])

    def test_auth_failure_advises_conf_check(self):
        def bad_auth(method, *params):
            raise cd.CheckError("401 Unauthorized.", cd.CheckError.AUTH)
        outcome = cd.run_checks(bad_auth)
        self.assertFalse(outcome["ready"])
        self.assertIn("rpcuser and rpcpassword", outcome["checks"][0]["fix"])

    def test_sendtoaddress_method_missing_fails_ready(self):
        def no_send(method, *params):
            if method == "sendtoaddress":
                raise cd.CheckError("Method not found", cd.CheckError.METHOD_MISSING)
            return all_good_caller(method, *params)
        outcome = cd.run_checks(no_send)
        self.assertFalse(outcome["ready"])
        send = {c["check"]: c for c in outcome["checks"]}["send_capability"]
        self.assertFalse(send["ok"])

    def test_locked_wallet_stops_with_unlock_advice(self):
        def locked(method, *params):
            if method == "getnewaddress":
                raise cd.CheckError("Error: wallet is encrypted and locked",
                                    cd.CheckError.WALLET)
            if method == "getblockchaininfo":
                return {"chain": "main", "blocks": 1}
            raise AssertionError(method)
        outcome = cd.run_checks(locked)
        self.assertFalse(outcome["ready"])
        self.assertIn("locked/encrypted", outcome["checks"][1]["fix"])

    def test_probe_never_sends_funds(self):
        """The sendtoaddress probe must use invalid args (amount -1)."""
        seen = []
        def spy(method, *params):
            if method == "sendtoaddress":
                seen.append(params)
                raise cd.CheckError("Invalid address", cd.CheckError.OTHER)
            return all_good_caller(method, *params)
        outcome = cd.run_checks(spy)
        self.assertTrue(outcome["ready"])
        self.assertEqual(seen, [("check-invalid-address", -1)])


@requires_writable
class TestMasking(unittest.TestCase):
    def test_mask_url_hides_password(self):
        masked = cd.mask_url("http://rpcuser:supersecret@10.0.0.8:12832")
        self.assertNotIn("supersecret", masked)
        self.assertIn("rpcuser", masked)
        self.assertIn("***", masked)
        self.assertIn("10.0.0.8:12832", masked)

    def test_mask_url_without_credentials_unchanged(self):
        self.assertEqual(cd.mask_url("http://127.0.0.1:12832"), "http://127.0.0.1:12832")

    def test_mask_url_with_reserved_characters(self):
        url = "http://us%20er%40x:p%40ss%3Aw%2Ford@127.0.0.1:12832"
        masked = cd.mask_url(url)
        # the PASSWORD is masked; the username is not secret and stays visible
        self.assertNotIn("p%40ss%3Aw%2Ford", masked)
        self.assertIn("***", masked)
        self.assertIn("127.0.0.1:12832", masked)
        import urllib.parse
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(urllib.parse.unquote(parts.username), "us er@x")
        self.assertEqual(urllib.parse.unquote(parts.password), "p@ss:w/ord")


@requires_writable
class TestConf(unittest.TestCase):
    def test_read_conf_parses_rpc_settings(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("rpcuser=alice\nrpcpassword=wonderland\nrpcport=12832\nserver=1\n")
            path = fh.name
        try:
            conf = cd.read_conf(path)
            self.assertEqual(conf["rpcuser"], "alice")
            self.assertEqual(conf["rpcpassword"], "wonderland")
            self.assertEqual(conf["rpcport"], "12832")
            url = cd.build_url(conf.get("rpcconnect", "127.0.0.1"),
                               conf.get("rpcport", "12832"),
                               conf["rpcuser"], conf["rpcpassword"])
            self.assertTrue(url.startswith("http://alice:wonderland@"))
        finally:
            os.unlink(path)


@requires_writable
class TestCli(unittest.TestCase):
    def test_main_json_ready_exit_zero(self):
        payload = {"getblockchaininfo": {"chain": "main", "blocks": 5}}
        def caller_factory(url, timeout):
            def call(method, *params):
                if method in payload:
                    return payload[method]
                return all_good_caller(method, *params)
            return call
        buf = io.StringIO()
        with mock.patch.object(cd, "make_caller", caller_factory), \
             redirect_stdout(buf):
            rc = cd.main(["http://u:p@127.0.0.1:12832", "--json"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn('"ready": true', out)

    def test_main_not_ready_exit_one(self):
        def caller_factory(url, timeout):
            def call(method, *params):
                raise cd.CheckError("connection refused", cd.CheckError.UNREACHABLE)
            return call
        buf = io.StringIO()
        with mock.patch.object(cd, "make_caller", caller_factory), \
             redirect_stdout(buf):
            rc = cd.main(["http://u:p@127.0.0.1:12832"])
        self.assertEqual(rc, 1)
        self.assertIn("NOT READY", buf.getvalue())


_NET_SKIP = (
    "requires a writable temp dir AND local socket creation/binding "
    "(these tests write temp conf files and bind local servers)"
)


@_env.net_gate
class TestTrickyCredentials(unittest.TestCase):
    """Credentials containing reserved characters must authenticate for real.

    build_url percent-encodes; the checker decodes for the Basic header. The
    server strictly verifies the decoded credentials — a masking or encoding
    bug fails here, not in production.
    """

    CREDENTIALS = [
        ("alice", "p@ss"),
        ("alice", "p/ss#word?"),
        ("us er@x", "påsswörd✓"),
        ("a:b", "c/d#e?f@g/h"),
        ("alice", "pw%dd%literal"),  # configparser interpolation char
    ]
    def _server_requiring_auth(self, expected_auth):
        import json as jmod
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        results = {
            "getblockchaininfo": {"chain": "main", "blocks": 1},
            "getnewaddress": "MZCfakeAddress",
            "getreceivedbyaddress": 0.0,
            "listtransactions": [],
            "getbalance": 3.5,
            "validateaddress": {"isvalid": True, "address": "MZCfakeAddress"},
        }

        class H(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_POST(self):  # noqa: N802
                if self.headers.get("Authorization") != expected_auth:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                req = jmod.loads(self.rfile.read(length).decode())
                resp = jmod.dumps({"jsonrpc": "1.0", "id": req.get("id"),
                                   "result": results.get(req["method"]),
                                   "error": None}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def test_build_url_encoded_creds_authenticate_end_to_end(self):
        import base64
        for user, password in self.CREDENTIALS:
            with self.subTest(user=user, password=password):
                expected = "Basic " + base64.b64encode(
                    f"{user}:{password}".encode("utf-8")
                ).decode("ascii")
                srv = self._server_requiring_auth(expected)
                try:
                    url = cd.build_url("127.0.0.1", str(srv.server_address[1]), user, password)
                    outcome = cd.run_checks(cd.make_caller(url, timeout=5))
                    self.assertTrue(outcome["ready"], outcome["checks"])
                finally:
                    srv.shutdown()
                    srv.server_close()

    def test_conf_path_tricky_credentials_strict_server_full_matrix(self):
        """maza.conf autodetection path across the FULL tricky matrix: each
        credential pair is written to a maza.conf, main --conf --json runs with
        NO mocking of the caller, and the strict server 401s anything but the
        exactly-matching decoded Basic header (round-11 finding: the previous
        version of this test covered only CREDENTIALS[0])."""
        import base64
        import json as jmod
        import tempfile, os
        for user, password in self.CREDENTIALS:
            with self.subTest(user=user, password=password):
                expected = "Basic " + base64.b64encode(
                    f"{user}:{password}".encode("utf-8")
                ).decode("ascii")
                srv = self._server_requiring_auth(expected)
                try:
                    with tempfile.NamedTemporaryFile(
                        "w", suffix=".conf", delete=False
                    ) as fh:
                        fh.write(
                            f"rpcuser={user}\nrpcpassword={password}\n"
                            f"rpcport={srv.server_address[1]}\nserver=1\n"
                        )
                        conf_path = fh.name
                    buf = io.StringIO()
                    try:
                        # REAL path: main -> read_conf -> build_url (encode)
                        # -> make_caller (decode) -> strict server.
                        with redirect_stdout(buf):
                            rc = cd.main(["--json", "--conf", conf_path])
                        self.assertEqual(rc, 0)
                        parsed = jmod.loads(buf.getvalue())
                        self.assertTrue(parsed["ready"], parsed)
                    finally:
                        os.unlink(conf_path)
                finally:
                    srv.shutdown()
                    srv.server_close()

    def test_read_conf_tolerates_percent_password(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("rpcuser=alice\nrpcpassword=p%40ss%literal\nrpcport=12832\nserver=1\n")
            conf_path = fh.name
        try:
            conf = cd.read_conf(conf_path)
            self.assertEqual(conf["rpcpassword"], "p%40ss%literal")
            self.assertEqual(conf["rpcuser"], "alice")
        finally:
            os.unlink(conf_path)

    def test_mask_never_leaks_tricky_passwords(self):
        import urllib.parse
        for user, password in self.CREDENTIALS:
            with self.subTest(user=user, password=password):
                url = cd.build_url("127.0.0.1", "12832", user, password)
                masked = cd.mask_url(url)
                self.assertNotIn(password, masked)
                self.assertNotIn(urllib.parse.quote(password, safe=""), masked)
                self.assertIn("***", masked)


@_env.net_gate
class TestCredsInUrl(unittest.TestCase):
    """Regression: credentials embedded in the RPC URL must still work.

    Python's urllib resolves "user:pass@host" as a hostname (gaierror -2)
    when creds ride in the request URL; the checker and MazadRPC must strip
    userinfo and authenticate via the Authorization header only.
    """

    def _rpc_server(self):
        import json as jmod
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = {"auth": None}

        class H(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_POST(self):  # noqa: N802
                received["auth"] = self.headers.get("Authorization")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                req = jmod.loads(body.decode())
                results = {
                    "getblockchaininfo": {"chain": "main", "blocks": 1},
                    "getnewaddress": "MZCfakeAddress",
                    "getreceivedbyaddress": 0.0,
                    "listtransactions": [],
                    "getbalance": 3.5,
                    "validateaddress": {"isvalid": True, "address": "MZCfakeAddress"},
                }
                result = results.get(req["method"])
                resp = jmod.dumps({"jsonrpc": "1.0", "id": req.get("id"), "result": result,
                                   "error": None}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, received

    def test_checker_with_creds_in_url_reaches_server(self):
        srv, received = self._rpc_server()
        try:
            port = srv.server_address[1]
            outcome = cd.run_checks(
                cd.make_caller(f"http://alice:secret@127.0.0.1:{port}", timeout=5)
            )
            self.assertTrue(outcome["ready"], outcome["checks"])
            import base64
            expected = "Basic " + base64.b64encode(b"alice:secret").decode()
            self.assertEqual(received["auth"], expected)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_mazadrpc_with_creds_in_url_reaches_server(self):
        from mazapay.mazad import MazadRPC
        srv, _received = self._rpc_server()
        try:
            port = srv.server_address[1]
            rpc = MazadRPC(url=f"http://alice:secret@127.0.0.1:{port}",
                           user="alice", password="secret", timeout=5)
            info = rpc.getinfo()
            self.assertEqual(info["chain"], "main")
        finally:
            srv.shutdown()
            srv.server_close()


    def test_mazadrpc_url_only_config_works(self):
        """MazadRPC with ONLY a creds-in-URL (no user/password args) must
        extract and use them - the url-only configuration path."""
        import base64
        srv, received = self._rpc_server()
        try:
            port = srv.server_address[1]
            rpc = MazadRPC(url=f"http://alice:secret@127.0.0.1:{port}", timeout=5)
            self.assertEqual(rpc.user, "alice")
            self.assertEqual(rpc.password, "secret")
            info = rpc.getinfo()
            self.assertEqual(info["chain"], "main")
            expected = "Basic " + base64.b64encode(b"alice:secret").decode()
            self.assertEqual(received["auth"], expected)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_http_401_classified_as_auth(self):
        """A real HTTP 401 must surface as a credential problem, not a
        transport problem."""
        import threading
        import urllib.error
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H401(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_POST(self):  # noqa: N802
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H401)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            caller = cd.make_caller(f"http://alice:wrong@127.0.0.1:{srv.server_address[1]}",
                                    timeout=5)
            outcome = cd.run_checks(caller)
            self.assertFalse(outcome["ready"])
            first = outcome["checks"][0]
            self.assertEqual(first["check"], "daemon_reachable_and_authenticated")
            self.assertIn("401", first["detail"])
            self.assertIn("rpcuser and rpcpassword", first["fix"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_json_output_is_pure_json_with_conf(self):
        import json as jmod
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("rpcuser=alice\nrpcpassword=wonderland\nrpcport=12832\nserver=1\n")
            conf_path = fh.name
        buf = io.StringIO()
        def caller_factory(url, timeout):
            def call(method, *params):
                return all_good_caller(method, *params)
            return call
        with mock.patch.object(cd, "make_caller", caller_factory), \
             redirect_stdout(buf):
            rc = cd.main(["--json", "--conf", conf_path])
        try:
            self.assertEqual(rc, 0)
            parsed = jmod.loads(buf.getvalue())  # MUST parse: JSON only, no prose
            self.assertTrue(parsed["ready"])
            self.assertEqual(parsed["conf"], conf_path)
        finally:
            os.unlink(conf_path)


if __name__ == "__main__":
    unittest.main()
