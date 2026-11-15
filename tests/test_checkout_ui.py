"""Checkout-page productisation tests: brand assets, static route, a11y math.

Layers:

- assets   — the vendored MazaCoin artwork is byte-identical to the official
             files the page was designed against (sha256 pins), lives inside
             the package, and GET /static/{name} serves it with image/png +
             a public Cache-Control + the uniform security headers;
- route    — the whitelist is the boundary: unknown names, traversal shapes,
             absolute paths, encoded separators and NUL bytes are all 404, and
             /favicon.ico serves the coin roundel (PNG bytes);
- page     — the shipped HTML carries the brand lockup, the four progress
             states, the receipt panel, the subdued demo chip and the footer
             claim; no external origins, no data: URIs (CSP img-src 'self');
- a11y     — a COMPUTED contrast matrix over the page's own CSS custom
             properties: every declared text/surface pair is >= 4.5:1 and
             every UI-component boundary >= 3:1 (WCAG 2.x 1.4.3 / 1.4.11).
             Values are parsed out of the shipped bytes, not hardcoded, so a
             later palette edit cannot quietly regress legibility.
"""
import hashlib
import os
import re
import unittest

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_checkout_ui)
    from tests import _sandbox as _env  # type: ignore

net_gate = _env.net_gate
requires_writable = _env.requires_writable

from mazapay import api, config, store  # noqa: E402

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(api.__file__)), "assets")

# Official MazaCoin artwork (mazacoin.org), staged 2026-11-15. Renaming the
# vendored copies must not change a byte.
PINNED_SHA256 = {
    "mazacoin-64.png": "cd1d53c7213df1133f4ca1800cc6ea5557331abbdda2553cd2f70db74dc47c98",
    "mazacoin-180.png": "07774f3d1cb5a261bf2218a9843194f0180a165de091312ce7010dcf5394ab36",
    "mazacoin-wordmark.png": "297b571ca7129b165f81b918506a9ef0f502e0c5b3aa1703cd7f4a9d2b39c5cb",
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _page() -> str:
    with open(api._TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# --- contrast math (WCAG 2.x relative luminance) -----------------------------
def _lin(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"not a 6-digit hex colour: {hex_color!r}")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(page: str) -> dict:
    """Parse the :root custom properties out of the shipped CSS."""
    block = page.split(":root {", 1)[1].split("}", 1)[0]
    found = dict(re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", block))
    # tokens declared with a trailing comment are already captured by the
    # regex above (it stops at the hex), so nothing else to strip.
    return found


class TestVendoredAssets(unittest.TestCase):
    """The artwork the page is designed against, checked by digest."""

    def test_assets_present_and_byte_identical(self):
        for name, digest in PINNED_SHA256.items():
            path = os.path.join(ASSETS_DIR, name)
            self.assertTrue(os.path.isfile(path), f"missing vendored asset {name}")
            with open(path, "rb") as fh:
                body = fh.read()
            self.assertTrue(body.startswith(PNG_MAGIC), f"{name} is not a PNG")
            self.assertEqual(hashlib.sha256(body).hexdigest(), digest, name)

    def test_page_references_only_whitelisted_assets(self):
        page = _page()
        refs = set(re.findall(r"/static/([A-Za-z0-9._-]+)", page))
        self.assertEqual(refs, set(api._ASSETS), refs)
        # every referenced file is a real whitelist key AND exists on disk
        for name in refs:
            self.assertTrue(os.path.isfile(os.path.join(ASSETS_DIR, name)), name)


@net_gate
class TestStaticRoute(unittest.TestCase):
    """GET /static/{name} + /favicon.ico over a real HTTP server."""

    @classmethod
    def setUpClass(cls):
        store.init_db()
        cls.srv = api.serve("127.0.0.1", 0, with_watcher=False)
        cls.port = cls.srv.server_address[1]
        from concurrent.futures import ThreadPoolExecutor

        cls.pool = ThreadPoolExecutor(max_workers=1)
        cls.pool.submit(cls.srv.serve_forever)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        api.shutdown_watcher()
        cls.pool.shutdown(wait=True)

    def _get(self, path):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_every_whitelisted_asset_is_served_as_png(self):
        for name in api._ASSETS:
            status, headers, body = self._get(f"/static/{name}")
            self.assertEqual(status, 200, name)
            self.assertEqual(headers.get("Content-Type"), "image/png", name)
            self.assertIn("public", headers.get("Cache-Control", ""), name)
            self.assertIn("max-age=", headers.get("Cache-Control", ""), name)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(headers.get("X-Frame-Options"), "DENY")
            self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))
            self.assertIn("img-src 'self'", headers.get("Content-Security-Policy", ""))
            with open(os.path.join(ASSETS_DIR, name), "rb") as fh:
                self.assertEqual(body, fh.read(), name)
            self.assertEqual(int(headers["Content-Length"]), len(body), name)

    def test_favicon_serves_the_coin_roundel(self):
        status, headers, body = self._get("/favicon.ico")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        with open(os.path.join(ASSETS_DIR, "mazacoin-180.png"), "rb") as fh:
            self.assertEqual(body, fh.read())

    def test_unknown_and_traversal_names_are_404(self):
        for path in (
            "/static/does-not-exist.png",
            "/static/",  # no name -> no route
            "/static/..",
            "/static/../api.py",
            "/static/..%2fapi.py",
            "/static/%2e%2e%2fapi.py",
            "/static/mazapay/api.py",
            "/static//etc/passwd",
            "/static/mazacoin-64.png%00.png",
            "/static/MAZACOIN-64.PNG",  # case-sensitive whitelist
        ):
            status, _headers, body = self._get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"import", body, path)  # never source bytes
            self.assertNotIn(b"root:", body, path)

    def test_static_route_is_public_even_in_token_mode(self):
        old = config.API_TOKEN
        config.API_TOKEN = "static-token-mode"
        try:
            status, _headers, _body = self._get("/static/mazacoin-64.png")
            self.assertEqual(status, 200)
        finally:
            config.API_TOKEN = old

    def test_page_is_public_and_still_carries_the_old_pin(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("text/html"))
        page = body.decode("utf-8")
        self.assertIn("Pay with MAZA", page)  # pinned by test_smoke + test_mazapay


class TestCheckoutPageStructure(unittest.TestCase):
    """The shipped bytes carry the whole worklist (no browser here)."""

    def setUp(self):
        self.page = _page()

    def test_brand_lockup_and_palette(self):
        self.assertIn("/static/mazacoin-wordmark.png", self.page)
        self.assertIn("/static/mazacoin-64.png", self.page)
        self.assertIn("/static/mazacoin-180.png", self.page)  # favicon link
        self.assertIn('rel="icon"', self.page)
        # amber replaced the old accent wholesale
        self.assertNotIn("#f5b400", self.page)
        self.assertIn("--amber:#f0a000", self.page)
        # red is present as the brand accent but never as body-text colour:
        # the only red TEXT token is the lightened --red-text.
        self.assertIn("--red:#f00000", self.page)
        self.assertIn("--red-text:#ff7d7d", self.page)

    def test_four_state_progress_and_receipt_panel(self):
        for step_id in ("step-quote", "step-seen", "step-conf", "step-receipt"):
            self.assertIn(f'id="{step_id}"', self.page)
        for label in ("Quote locked", "Payment seen", "Confirmed", "Receipt"):
            self.assertIn(label, self.page)
        # receipt panel with the fields a screenshot needs
        for rid in ("r-usd", "r-maza", "r-rate", "r-conf", "r-tx", "r-time", "r-order"):
            self.assertIn(f'id="{rid}"', self.page)
        self.assertIn('id="receipt"', self.page)
        self.assertIn("Screenshot this panel", self.page)
        # driven by the existing polling contract only
        self.assertIn("fetch('/orders/' + orderId)", self.page)
        self.assertIn("$('status').classList.add('paid')", self.page)

    def test_hierarchy_and_footer(self):
        self.assertIn("Powered by MazaCoin", self.page)
        self.assertIn("settled in USD on the books", self.page)
        self.assertIn('id="mock-banner"', self.page)
        self.assertIn("class=\"chip\"", self.page)  # demoted to a small chip
        self.assertIn('class="hero-amount"', self.page)
        self.assertIn("Scan with your MazaCoin wallet", self.page)
        self.assertIn('id="addr"', self.page)  # copy fallback stays

    def test_no_external_origins_or_data_uris(self):
        self.assertNotIn("http://", self.page)
        self.assertNotIn("https://", self.page)
        self.assertNotIn("data:image", self.page)
        self.assertNotIn("<link rel=\"stylesheet\"", self.page)
        self.assertNotIn("@import", self.page)
        self.assertNotIn("fonts.googleapis", self.page)


class TestContrastMatrix(unittest.TestCase):
    """Every declared pair, computed from the page's own tokens."""

    # (foreground token, background token, minimum ratio, what it is)
    TEXT_PAIRS = [
        ("text", "surface", 4.5, "body copy on the card"),
        ("text", "surface-2", 4.5, "receipt values / address on inset panels"),
        ("text", "bg", 4.5, "footer tagline on the page"),
        ("muted", "surface", 4.5, "labels and helper copy on the card"),
        ("muted", "surface-2", 4.5, "status line + receipt labels"),
        ("muted", "bg", 4.5, "footer fine print"),
        ("amber", "surface", 4.5, "brand accent text (progress, chips)"),
        ("amber-soft", "surface", 4.5, "hero amount"),
        ("amber-soft", "surface-2", 4.5, "rate-held pill inside the QR tile"),
        ("ok", "surface", 4.5, "confirmed status text"),
        ("ok", "surface-2", 4.5, "receipt seal glyph"),
        ("red-text", "surface", 4.5, "error copy"),
        ("red-text", "surface-2", 4.5, "expired-quote heading"),
        ("ink", "amber", 4.5, "primary button label on amber"),
        ("ink", "amber-soft", 4.5, "primary button label on the gradient top"),
    ]
    # WCAG 1.4.11: component boundaries / focus indicators need 3:1.
    UI_PAIRS = [
        ("line-strong", "surface-2", 3.0, "amount field border"),
        ("line-strong", "surface", 3.0, "ghost button border on the card"),
        ("amber", "surface", 3.0, "focus ring / active progress node"),
        ("line-strong", "surface-2", 3.0, "progress node ring (pending)"),
    ]
    # literal colours used outside :root (placeholder), still computed
    LITERAL_PAIRS = [
        ("#7b879c", "surface-2", 4.5, "amount-field placeholder"),
    ]

    def setUp(self):
        self.page = _page()
        self.tokens = _tokens(self.page)

    def test_tokens_captured(self):
        # if the palette block is restructured, this fails loudly instead of
        # letting the matrix silently pass over missing tokens
        for key in (
            "bg", "surface", "surface-2", "text", "muted", "amber",
            "amber-soft", "ink", "ok", "red-text", "line-strong",
        ):
            self.assertIn(key, self.tokens, key)

    def test_text_contrast_floor(self):
        failures = []
        for fg, bg, minimum, what in self.TEXT_PAIRS:
            r = contrast(self.tokens[fg], self.tokens[bg])
            if r < minimum:
                failures.append(f"{what}: {fg} on {bg} = {r:.2f} < {minimum}")
        self.assertEqual(failures, [], failures)

    def test_ui_component_contrast_floor(self):
        failures = []
        for fg, bg, minimum, what in self.UI_PAIRS:
            r = contrast(self.tokens[fg], self.tokens[bg])
            if r < minimum:
                failures.append(f"{what}: {fg} on {bg} = {r:.2f} < {minimum}")
        self.assertEqual(failures, [], failures)

    def test_literal_pair_contrast_floor(self):
        for fg, bg, minimum, what in self.LITERAL_PAIRS:
            r = contrast(fg, self.tokens[bg])
            self.assertGreaterEqual(r, minimum, f"{what}: {r:.2f}")

    def test_brand_red_is_not_used_for_text(self):
        """--red on any page surface is below 4.5:1, so it must not carry copy."""
        for bg in ("surface", "surface-2", "bg"):
            self.assertLess(
                contrast(self.tokens["red"], self.tokens[bg]), 4.5,
                "--red became a text-safe colour; the red-text split is now wrong",
            )
        # and the page really does use the split: no `color:var(--red)` rule
        self.assertNotRegex(self.page, r"color:\s*var\(--red\)")


if __name__ == "__main__":
    unittest.main()
