"""QR decode-verification tests: the rendered SVG decodes back to the payload.

Closes the no-decoder scannability gap carried since the ed652fc / 9365199
verdicts ("no decoder in env, byte-level scan decode unverified"). The
decoder (mazapay.qrdecode) implements the ISO/IEC 18004 read-side
independently of the vendored encoder, so these checks verify the symbol
against the standard rather than against itself:

- round-trip — mazapay.qr.address_svg(payload) → decode_svg() == payload,
  for checkout-realistic addresses, single-char, unicode, and long payloads
  (the long one crosses into version >= 7 and exercises the version-info
  BCH(18,6) path);
- version sweep — every version 1-40 at forced MEDIUM (no ECC boost), so
  every row of the ECC/interleave tables and every mask pick is exercised;
- ecc sweep — four ECC levels at representative versions, hitting each
  table row; plus a direct transcription cross-check of the decoder's
  spec tables against the vendored encoder's (guards against transcription
  typos; upstream truth itself is pinned by the vendor sha256 test);
- damage detection — flipping one data module or one format module makes
  decode fail with DecodeError (syndromes/BCH), proving the decoder
  verifies rather than silently accepts a damaged symbol;
- SVG contract — malformed documents (no viewBox/rect/modules, duplicate
  or out-of-frame squares, non-square viewBox, wrong size) raise ValueError;
- endpoint — the SVG actually SERVED at GET /orders/{id}/qr.svg decodes to
  exactly the order's deposit address: the end-to-end proof of what a payer's
  scanner would read.

Honest limits (unchanged from the QR feature verdicts): this is a
matrix-level decoder — no camera capture, perspective, or damage
CORRECTION (damaged symbols are rejected, not repaired); no JS/browser
rendering step is exercised.
"""
import unittest

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_qr_decode)
    from tests import _sandbox as _env  # type: ignore

net_gate = _env.net_gate

from mazapay import api, config, qr, qrdecode, store  # noqa: E402
from mazapay.vendor import qrcodegen  # noqa: E402

# Realistic checkout payloads: a plausible 34-char base58-style address, a
# one-char minimum symbol, unicode text, and a payload long enough to force
# version >= 7 (version-info path).
_ADDRESS_LIKE = "MJ2gSQrkQnwMXYuYqXFzJG7ZLnDc9AmVte"
_LONG_PAYLOAD = "x" * 120  # 120 bytes @ ECC M(+boost) lands in version 7-8
_NUMERIC = "1234567890123456"


def _matrix_of(code: "qrcodegen.QrCode"):
    return [
        [code.get_module(x, y) for x in range(code.get_size())]
        for y in range(code.get_size())
    ]


def _forced_code(payload_segments, ecc, version):
    return qrcodegen.QrCode.encode_segments(
        payload_segments, ecc, minversion=version, maxversion=version,
        mask=-1, boostecl=False,
    )


class TestSvgRoundTrip(unittest.TestCase):
    def test_address_like_round_trip(self):
        self.assertEqual(qrdecode.decode_svg(qr.address_svg(_ADDRESS_LIKE)), _ADDRESS_LIKE)

    def test_single_char_round_trip(self):
        self.assertEqual(qrdecode.decode_svg(qr.address_svg("M")), "M")

    def test_unicode_payload_round_trip(self):
        payload = "mazapay ✓ ünïcødé address-check"
        self.assertEqual(qrdecode.decode_svg(qr.address_svg(payload)), payload)

    def test_long_payload_round_trip_crosses_version_7(self):
        # Prove the payload really crosses into version-info territory so
        # the version-info BCH path is exercised, not skipped.
        code = qrcodegen.QrCode.encode_text(_LONG_PAYLOAD, qr._ECC)
        self.assertGreaterEqual(code.get_version(), 7)
        self.assertEqual(qrdecode.decode_svg(qr.address_svg(_LONG_PAYLOAD)), _LONG_PAYLOAD)

    def test_numeric_payload_round_trip(self):
        self.assertEqual(qrdecode.decode_svg(qr.address_svg(_NUMERIC)), _NUMERIC)


class TestVersionAndEccSweep(unittest.TestCase):
    SEG = None  # built in setUpClass

    @classmethod
    def setUpClass(cls):
        cls.SEG = [qrcodegen.QrSegment.make_numeric(_NUMERIC)]

    def test_every_version_1_to_40_medium(self):
        for version in range(1, 41):
            code = _forced_code(self.SEG, qrcodegen.QrCode.Ecc.MEDIUM, version)
            self.assertEqual(code.get_version(), version)
            self.assertEqual(
                qrdecode.decode_matrix(_matrix_of(code)), _NUMERIC,
                "version %d mask %d" % (version, code.get_mask()),
            )

    def test_all_four_ecc_levels_at_representative_versions(self):
        for version in (1, 5, 12, 25, 40):
            for ecc in (
                qrcodegen.QrCode.Ecc.LOW,
                qrcodegen.QrCode.Ecc.MEDIUM,
                qrcodegen.QrCode.Ecc.QUARTILE,
                qrcodegen.QrCode.Ecc.HIGH,
            ):
                code = _forced_code(self.SEG, ecc, version)
                self.assertEqual(
                    qrdecode.decode_matrix(_matrix_of(code)), _NUMERIC,
                    "version %d ecc %d" % (version, ecc.ordinal),
                )

    def test_decoder_tables_match_vendored_spec_tables(self):
        """Transcription guard: the decoder's independent copies of the two
        ISO 18004 tables must equal the vendored encoder's (both ultimately
        cite the standard; this test catches a typo in THIS repo's decoder
        copy, while the sweeps exercise the cells end-to-end)."""
        self.assertEqual(
            qrdecode._ECC_CODEWORDS_PER_BLOCK,
            qrcodegen.QrCode._ECC_CODEWORDS_PER_BLOCK,
        )
        self.assertEqual(
            qrdecode._NUM_ERROR_CORRECTION_BLOCKS,
            qrcodegen.QrCode._NUM_ERROR_CORRECTION_BLOCKS,
        )

    def test_alphanumeric_and_byte_segments(self):
        cases = (
            (qrcodegen.QrSegment.make_alphanumeric("MTB5QK9-LM NO.2$%*+./:"),
             "MTB5QK9-LM NO.2$%*+./:"),
            (qrcodegen.QrSegment.make_bytes(b"MJ2gSQrkQnwMXYuYqXFzJG7ZLnDc9AmVte"),
             "MJ2gSQrkQnwMXYuYqXFzJG7ZLnDc9AmVte"),
        )
        for segment, expected in cases:
            code = qrcodegen.QrCode.encode_segments([segment], qr._ECC)
            self.assertEqual(qrdecode.decode_matrix(_matrix_of(code)), expected)


class TestDamageDetection(unittest.TestCase):
    """The decoder verifies what it READS: format info (BCH), version info
    (BCH), and the Reed-Solomon-covered codewords. Damage to pure structure
    modules (finders/timing/alignment) is honestly NOT detected — the decoder
    never reads them — so these tests flip read modules, not finder modules."""

    def test_flipped_data_module_rejected(self):
        """Remove one dark codeword-carrying module (first non-function module
        in zigzag order, per the decoder's own function map) → nonzero
        Reed-Solomon syndromes → DecodeError, not a silently-decoded symbol."""
        payload = _ADDRESS_LIKE
        code = qrcodegen.QrCode.encode_text(payload, qr._ECC)
        size = code.get_size()
        quiet = qr.QUIET_ZONE_MODULES
        func = qrdecode._function_map(size, (size - 17) // 4)
        target = None
        for right in range(size - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (size - 1 - vert) if upward else vert
                    if not func[y][x] and code.get_module(x, y):
                        target = (x + quiet, y + quiet)
                        break
                if target:
                    break
            if target:
                break
        self.assertIsNotNone(target)
        svg = qr.address_svg(payload)
        square = "M%d,%dh1v1h-1z" % target
        self.assertIn(square, svg)
        mutated = svg.replace(square, "", 1)
        self.assertNotEqual(mutated, svg)
        with self.assertRaises(qrdecode.DecodeError):
            qrdecode.decode_svg(mutated)

    def test_flipped_format_module_rejected(self):
        """Remove one DARK format-info module (first placement) → the BCH(15,5)
        check or the placements-agree check must fail → DecodeError."""
        payload = _ADDRESS_LIKE
        code = qrcodegen.QrCode.encode_text(payload, qr._ECC)
        svg = qr.address_svg(payload)
        coords = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)]
        target = None
        for (x, y) in coords:
            if code.get_module(x, y):
                target = (x + qr.QUIET_ZONE_MODULES, y + qr.QUIET_ZONE_MODULES)
                break
        self.assertIsNotNone(target)
        square = "M%d,%dh1v1h-1z" % target
        self.assertIn(square, svg)
        mutated = svg.replace(square, "", 1)
        self.assertNotEqual(mutated, svg)
        with self.assertRaises(qrdecode.DecodeError):
            qrdecode.decode_svg(mutated)

    def test_clean_svg_still_decodes(self):
        """Sanity guard for the mutation tests: the unmutated document decodes."""
        self.assertEqual(
            qrdecode.decode_svg(qr.address_svg(_ADDRESS_LIKE)), _ADDRESS_LIKE
        )


class TestSvgContract(unittest.TestCase):
    BASE = qr.address_svg(_ADDRESS_LIKE)

    def test_non_svg_text_rejected(self):
        with self.assertRaises(ValueError):
            qrdecode.decode_svg("<html><body>not a qr</body></html>")
        with self.assertRaises(ValueError):
            qrdecode.decode_svg("")

    def test_nonsquare_viewbox_rejected(self):
        import re
        bad = re.sub(
            r'viewBox="0 0 (\d+) (\d+)"',
            lambda m: 'viewBox="0 0 %s %d"' % (m.group(1), int(m.group(2)) - 1),
            self.BASE,
        )
        self.assertNotEqual(bad, self.BASE)
        with self.assertRaises(ValueError):
            qrdecode.decode_svg(bad)

    def test_missing_rect_rejected(self):
        import re
        bad = re.sub(r"<rect[^>]*/>", "", self.BASE)
        self.assertNotEqual(bad, self.BASE)
        with self.assertRaises(ValueError):
            qrdecode.decode_svg(bad)

    def test_no_modules_rejected(self):
        import re
        stripped = re.sub(r"M\d+,\d+h1v1h-1z", "", self.BASE)
        with self.assertRaises(ValueError):
            qrdecode.decode_svg(stripped)

    def test_out_of_frame_square_rejected(self):
        bad = self.BASE.replace(
            "</svg>", '<path d="M400,400h1v1h-1z" fill="#000000"/></svg>'
        )
        with self.assertRaises(ValueError):
            qrdecode.decode_svg(bad)

    def test_duplicate_square_rejected(self):
        first = "M4,4h1v1h-1z"
        bad = self.BASE.replace(
            "</svg>", '<path d="%s" fill="#000000"/></svg>' % first
        )
        with self.assertRaises(ValueError):
            qrdecode.decode_svg(bad)


class TestDecodeMatrixInput(unittest.TestCase):
    def test_bad_sizes_rejected(self):
        for size in (20, 22, 25, 24):  # below minimum / non-(17+4k)
            m = [[False] * size for _ in range(size)]
            with self.assertRaises(qrdecode.DecodeError):
                qrdecode.decode_matrix(m)

    def test_ragged_matrix_rejected(self):
        m = [[False] * 21 for _ in range(21)]
        m[3] = [False] * 20
        with self.assertRaises(qrdecode.DecodeError):
            qrdecode.decode_matrix(m)

    def test_blank_matrix_rejected(self):
        m = [[False] * 21 for _ in range(21)]
        with self.assertRaises(qrdecode.DecodeError):
            qrdecode.decode_matrix(m)


@net_gate
class TestServedQrDecodesToAddress(unittest.TestCase):
    """The money proof: decode the SVG bytes the server actually delivers and
    require the order's deposit address — what a payer's scanner reads."""

    @classmethod
    def setUpClass(cls):
        store.init_db()
        cls._old_token = config.API_TOKEN
        config.API_TOKEN = "qrdecode-endpoint-token"
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

    def test_served_qr_svg_decodes_to_order_address(self):
        import json
        import urllib.request
        body = json.dumps({"usd_amount": 3.0, "ref": "qrdecode-test"}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/orders" % self.port,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer qrdecode-endpoint-token",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            order = json.loads(resp.read())
        req2 = urllib.request.Request(
            "http://127.0.0.1:%d/orders/%s/qr.svg" % (self.port, order["id"])
        )
        with urllib.request.urlopen(req2, timeout=5) as resp:
            svg = resp.read().decode("utf-8")
        self.assertEqual(qrdecode.decode_svg(svg), order["address"])


class TestIsoPadBytePattern(unittest.TestCase):
    """ISO/IEC 18004 tail conformance for the checkout payloads.

    After the data segment and its 4-bit terminator, the pad bits to the
    next byte boundary are zero and the remaining data codewords alternate
    0xEC, 0x11 starting with 0xEC. The boundary arithmetic here is derived
    from the ISO 18004 constants (4-bit mode indicator, byte-mode char
    count 8 bits for v1-9 / 16 bits for v10+, 8 bits per payload byte,
    4-bit terminator) rather than the decoder's own tables, so the
    assertion checks the standard, not the module under test.
    """

    @staticmethod
    def _raw_data_stream(svg_text):
        """Recover (version, data-codeword stream) via the decoder internals."""
        matrix, version = qrdecode._svg_to_matrix(svg_text)
        func = qrdecode._function_map(len(matrix), version)
        ecc_ordinal, mask = qrdecode._read_format_info(matrix, len(matrix))
        raw_cw = qrdecode._num_raw_data_modules(version) // 8
        codewords = qrdecode._read_codewords(
            qrdecode._unmasked(matrix, func, mask), func, len(matrix), raw_cw
        )
        return version, qrdecode._deinterleave(codewords, version, ecc_ordinal)

    def _assert_iso_tail(self, payload):
        version, stream = self._raw_data_stream(qr.address_svg(payload))
        ccount_bits = 8 if version < 10 else 16
        bits = 4 + ccount_bits + 8 * len(payload) + 4
        pad_start, slack = divmod(bits, 8)
        if slack:
            # terminator pad bits: the low-order bits of the last content byte
            self.assertEqual(
                stream[pad_start] & ((1 << (8 - slack)) - 1), 0,
                "version %d: terminator pad bits are not zero" % version,
            )
            pad_start += 1
        pad = stream[pad_start:]
        self.assertGreaterEqual(
            len(pad), 2, "version %d: fixture left no pad bytes to check" % version
        )
        for i, byte in enumerate(pad):
            self.assertEqual(
                byte, 0xEC if i % 2 == 0 else 0x11,
                "version %d pad byte %d: 0x%02X breaks the 0xEC/0x11 alternation"
                % (version, i, byte),
            )

    def test_pad_alternation_address_like(self):
        # mixed-case payload forces byte mode in the vendored encoder
        self._assert_iso_tail(_ADDRESS_LIKE)

    def test_pad_alternation_long_payload(self):
        self._assert_iso_tail(_LONG_PAYLOAD)


class TestGfMulTables(unittest.TestCase):
    """The exp/log refactor must be behavior-identical to the bitwise reference."""

    @staticmethod
    def _peasant_reference(x, y):
        z = 0
        for i in reversed(range(8)):
            z = (z << 1) ^ ((z >> 7) * 0x11D)
            z ^= ((y >> i) & 1) * x
        return z

    def test_full_sweep_matches_peasant_reference(self):
        for x in range(256):
            for y in range(256):
                self.assertEqual(
                    qrdecode._gf_mul(x, y), self._peasant_reference(x, y),
                    "_gf_mul(0x%02X, 0x%02X)" % (x, y),
                )

    def test_exp_log_tables_are_inverse_maps(self):
        for i in range(255):
            self.assertEqual(qrdecode._GF_LOG[qrdecode._GF_EXP[i]], i)


if __name__ == "__main__":
    unittest.main()
