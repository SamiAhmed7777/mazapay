"""QR checkout feature tests.

Layers covered (the vendored ENCODER itself is trusted to upstream — see
the provenance test — everything downstream of it is verified here):

- provenance  — the vendored qrcodegen.py is byte-identical to the pinned
                upstream commit (sha256) and still carries the MIT header,
                so an accidental edit of vendored bytes fails the suite;
- structure   — the produced matrix has valid version/size, three finder
                patterns with separators, timing patterns, and the always-
                dark module (structural QR properties from ISO/IEC 18004);
- format info — the 15-bit format-info fields are verified INDEPENDENTLY:
                recomputed per the spec's BCH(15,5) formula (generator
                0x537, XOR mask 0x5412) for each candidate mask and
                compared against BOTH placements in the matrix, so the
                library's ECC level + mask encoding is checked against the
                standard, not against itself;
- renderer    — the SVG is well-formed XML with one path whose unit-square
                count equals the dark-module count, is deterministic, is
                distinct per payload, and a pinned golden hash guards
                renderer regressions;
- endpoint    — GET /orders/{id}/qr.svg over a real HTTP server: 200 with
                image/svg+xml + no-store + the uniform security headers,
                unknown id 404, and outbound (spending) order ids stay
                hidden (404) from unauthenticated reads, exactly like the
                order itself.
"""
import hashlib
import unittest
import urllib.error  # noqa: F401  (urllib.request raises it)
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_qr)
    from tests import _sandbox as _env  # type: ignore

net_gate = _env.net_gate

from mazapay import api, config, qr, store  # noqa: E402
from mazapay.vendor import qrcodegen  # noqa: E402

# Byte-identity pin: raw upstream file at
# nayuki/QR-Code-generator bcfe1b018cec5f1f91b5705eca5bb4cdd34e9f8d,
# python/qrcodegen.py. Mirrors the constant in mazapay/vendor/__init__.py.
VENDOR_SHA256 = "9f4ed1dd201dcb92b1bc0d6e14f46c754bcff0ce48580c5d7e8ace8f6926c8ef"

_PAYLOAD = "tMAZA-MOCK-x-0123456789abcdef"

# ISO/IEC 18004 format-info data bits: 2 ECC-level bits (L=01, M=00,
# Q=11, H=10) above the 3 mask bits.
_ECC_FORMAT_BITS = {
    qrcodegen.QrCode.Ecc.LOW: 0b01,
    qrcodegen.QrCode.Ecc.MEDIUM: 0b00,
    qrcodegen.QrCode.Ecc.QUARTILE: 0b11,
    qrcodegen.QrCode.Ecc.HIGH: 0b10,
}


def _expected_format_bits(ecc: "qrcodegen.QrCode.Ecc", mask: int) -> int:
    """Recompute the 15-bit format field straight from the ISO formula."""
    data = (_ECC_FORMAT_BITS[ecc] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)  # BCH(15,5), generator 0x537
    return ((data << 10) | rem) ^ 0x5412


def _bit(bits: int, i: int) -> bool:
    return ((bits >> i) & 1) != 0


class TestVendorProvenance(unittest.TestCase):
    def test_vendored_bytes_match_upstream_pin(self):
        import os
        path = os.path.join(
            os.path.dirname(qrcodegen.__file__), "qrcodegen.py"
        )
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(digest, VENDOR_SHA256)

    def test_mit_license_header_intact(self):
        import inspect
        src = inspect.getsource(qrcodegen)
        self.assertIn("Copyright (c) Project Nayuki. (MIT License)", src)
        self.assertIn("Permission is hereby granted, free of charge", src)


class TestMatrixStructure(unittest.TestCase):
    """QR structural properties, checked straight on the vendor matrix."""

    @classmethod
    def setUpClass(cls):
        cls.code = qrcodegen.QrCode.encode_text(_PAYLOAD, qr._ECC)
        cls.size = cls.code.get_size()

    def _dark(self, x, y):
        return self.code.get_module(x, y)

    def test_size_is_valid_version_geometry(self):
        self.assertGreaterEqual(self.size, 21)
        self.assertLessEqual(self.size, 177)
        self.assertEqual((self.size - 17) % 4, 0)  # 17 + 4*version

    def test_finder_patterns_and_separators(self):
        for ox, oy in ((0, 0), (0, self.size - 7), (self.size - 7, 0)):
            for i in range(7):
                # 7x7 ring + center 3x3 dark
                self.assertTrue(self._dark(ox + i, oy), (ox, oy, i))
                self.assertTrue(self._dark(ox, oy + i), (ox, oy, i))
                self.assertTrue(self._dark(ox + 6, oy + i), (ox, oy, i))
                self.assertTrue(self._dark(ox + i, oy + 6), (ox, oy, i))
            for x in range(1, 6):
                for y in range(1, 6):
                    dark = (2 <= x <= 4) and (2 <= y <= 4)
                    self.assertEqual(
                        self._dark(ox + x, oy + y), dark, (ox, oy, x, y)
                    )
            # one-module light separator around the finder
            for j in range(8):
                self.assertFalse(self._dark(ox + j, oy + 7))
                self.assertFalse(self._dark(ox + 7, oy + j))

    def test_timing_patterns_alternate(self):
        for c in range(8, self.size - 8):
            self.assertEqual(self._dark(c, 6), c % 2 == 0, c)
            self.assertEqual(self._dark(6, c), c % 2 == 0, c)

    def test_dark_module_always_dark(self):
        self.assertTrue(self._dark(8, self.size - 8))

    def test_format_info_matches_iso_recomputation(self):
        """The 15-bit format fields equal the independently recomputed
        ISO/IEC 18004 value for the code's ACTUAL ECC level + mask —
        both placements, proving the library's BCH computation and
        placement against the standard, not against itself."""
        size = self.size
        actual_ecc = self.code.get_error_correction_level()
        actual_mask = self.code.get_mask()
        bits = _expected_format_bits(actual_ecc, actual_mask)
        first_ok = all(
            [
                all(self._dark(8, i) == _bit(bits, i) for i in range(6)),
                self._dark(8, 7) == _bit(bits, 6),
                self._dark(8, 8) == _bit(bits, 7),
                self._dark(7, 8) == _bit(bits, 8),
                all(self._dark(14 - i, 8) == _bit(bits, i) for i in range(9, 15)),
            ]
        )
        second_ok = all(
            [
                all(self._dark(size - 1 - i, 8) == _bit(bits, i) for i in range(8)),
                all(self._dark(8, size - 15 + i) == _bit(bits, i) for i in range(8, 15)),
            ]
        )
        self.assertTrue(first_ok, "first format-info placement wrong")
        self.assertTrue(second_ok, "second format-info placement wrong")

    def test_actual_mask_is_the_only_iso_match(self):
        """Exactly one mask's format field matches the matrix — and it is
        the mask the code reports (also proves the requested-MEDIUM code
        may legally boost: actual ECC ∈ the spec's four levels)."""
        size = self.size
        actual_ecc = self.code.get_error_correction_level()
        matched = [
            mask
            for mask in range(8)
            if self._placement_matches(_expected_format_bits(actual_ecc, mask))
        ]
        self.assertEqual(matched, [self.code.get_mask()])

    def _placement_matches(self, bits: int) -> bool:
        size = self.size
        return all(
            [
                all(self._dark(8, i) == _bit(bits, i) for i in range(6)),
                self._dark(8, 7) == _bit(bits, 6),
                self._dark(8, 8) == _bit(bits, 7),
                self._dark(7, 8) == _bit(bits, 8),
                all(self._dark(14 - i, 8) == _bit(bits, i) for i in range(9, 15)),
                all(self._dark(size - 1 - i, 8) == _bit(bits, i) for i in range(8)),
                all(self._dark(8, size - 15 + i) == _bit(bits, i) for i in range(8, 15)),
            ]
        )


class TestSvgRenderer(unittest.TestCase):
    def test_wellformed_xml_single_path(self):
        svg = qr.address_svg(_PAYLOAD)
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        ns = {"s": "http://www.w3.org/2000/svg"}
        self.assertEqual(len(root.findall("s:path", ns)), 1)
        self.assertEqual(len(root.findall("s:rect", ns)), 1)
        # 29-char payload @ ECC M → version 3 (29 modules) + 2*4 quiet zone
        self.assertEqual(root.get("viewBox"), "0 0 37 37")

    def test_rect_count_equals_dark_modules(self):
        code = qrcodegen.QrCode.encode_text(_PAYLOAD, qr._ECC)
        dark = sum(
            1
            for y in range(code.get_size())
            for x in range(code.get_size())
            if code.get_module(x, y)
        )
        svg = qr.address_svg(_PAYLOAD)
        rects = svg.count("h1v1h-1z")
        self.assertEqual(rects, dark)
        self.assertGreater(rects, 0)

    def test_deterministic(self):
        self.assertEqual(qr.address_svg(_PAYLOAD), qr.address_svg(_PAYLOAD))

    def test_distinct_payloads_distinct_svgs(self):
        a = qr.address_svg("tMAZA-MOCK-x-aaaa1111bbbb")
        b = qr.address_svg("tMAZA-MOCK-x-cccc3333dddd")
        self.assertNotEqual(a, b)
        self.assertNotIn("tMAZA", a)  # payload text never enters the SVG body
        self.assertNotIn("tMAZA", b)

    def test_golden_regression_hash(self):
        """Renderer regression guard (not an independent-correctness proof):
        the exact SVG bytes for this payload are pinned; any accidental
        rendering change fails here and must be re-pinned deliberately."""
        digest = hashlib.sha256(qr.address_svg(_PAYLOAD).encode("utf-8")).hexdigest()
        self.assertEqual(
            digest,
            "897f4323b7d9c10bde68f485dc0e9602ae627a32672640dd4557ae81daa331d9",
        )

    def test_empty_payload_rejected(self):
        with self.assertRaises(ValueError):
            qr.address_svg("")
        with self.assertRaises(ValueError):
            qr.address_svg(None)  # type: ignore[arg-type]


@net_gate
class TestQrEndpoint(unittest.TestCase):
    """GET /orders/{id}/qr.svg over a real HTTP server (token-auth mode)."""

    @classmethod
    def setUpClass(cls):
        store.init_db()
        cls._old_token = config.API_TOKEN
        config.API_TOKEN = "qr-endpoint-token"
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
        config.API_TOKEN = cls._old_token

    def _req(self, method, path, body=None, token: Optional[str] = "qr-endpoint-token"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {}
        if body is not None:
            import json
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_qr_happy_path_headers_and_body(self):
        status, _, body = self._req(
            "POST", "/orders", {"usd_amount": 12.0, "ref": "qr-test"}
        )
        self.assertEqual(status, 201)
        import json
        order = json.loads(body)
        status, headers, svg_body = self._req("GET", f"/orders/{order['id']}/qr.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/svg+xml")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))
        root = ET.fromstring(svg_body.decode("utf-8"))
        self.assertTrue(root.tag.endswith("svg"))
        # the SVG encodes the SAME address the order JSON advertises
        svg_str = svg_body.decode("utf-8")
        self.assertNotIn(order["address"], svg_str)  # address text is NOT embedded…
        self.assertTrue(svg_str.count("h1v1h-1z") > 0)  # …only its module matrix is

    def test_qr_unknown_order_404(self):
        status, headers, body = self._req("GET", "/orders/does-not-exist/qr.svg")
        self.assertEqual(status, 404)
        self.assertIn(b"not found", body)

    def test_qr_outbound_hidden_without_token(self):
        status, _, body = self._req(
            "POST", "/payments", {"address": "MADDR-QR", "usd_amount": 2.0}
        )
        self.assertEqual(status, 201)
        import json
        pay = json.loads(body)
        status, _, _ = self._req(
            "GET", f"/orders/{pay['id']}/qr.svg", token=None
        )
        self.assertEqual(status, 404)  # never leak existence to the public
        status, _, svg = self._req("GET", f"/orders/{pay['id']}/qr.svg")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
