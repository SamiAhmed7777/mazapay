"""QR-code SVG rendering for the checkout page (payment request → code).

Payload convention (flipped 2026-11-15, Sami directive "do it" after the
scheme was verified in the official codebase): the QR encodes the official
`maza:` payment URI — `maza:<address>?amount=<decimal MAZA>` — exactly the
format MazaCoin's own wallet code implements and generates (MazaCoin/maza
src/qt/guiutil.cpp: parseBitcoinURI accepts scheme "maza" with amount/label/
message params and req- rejection semantics; formatBitcoinURI emits
"maza:%1?amount=%2"; the running v0.16.3 maza-qt binary contains the scheme
strings). Scanning pre-fills address AND amount in the official desktop
wallet. The page keeps the address text + Copy address button as the
fallback for scanners that only read bare addresses (paper wallets).

Amount formatting mirrors the wallet's own output: decimal with a "." point,
no thousands separators, trailing zeros stripped, quantized to 8 decimals
(the wallet parses at BitcoinUnits::BTC precision). No order data beyond
address + amount ever enters the SVG text — only the boolean matrix — so
the document has no injection surface.

Encoder: the vendored pure-Python library (Project Nayuki, MIT) — see
mazapay/vendor/__init__.py for the provenance pin. This module renders the
library's boolean module matrix to a self-contained SVG: white background
(the spec quiet zone AND scannability on the dark checkout card), one <path>
of unit squares.
"""
from decimal import Decimal
from typing import List

from .vendor import qrcodegen


def payment_uri(address: str, maza_amount) -> str:
    """Build the official maza: payment URI (BIP-21 semantics, MazaCoin form).

    maza_amount arrives as the order's stored REAL (float via SQLite);
    Decimal(str(value)) keeps typical 2dp quotes exact and quantizing to
    8dp bounds any float tail. format(..., "f") after normalize() avoids
    exponent forms ("8E+2") the wallet parser would misread as the address.
    """
    if not isinstance(address, str) or not address:
        raise ValueError("payment_uri: address must be a non-empty string")
    if maza_amount is None:
        return f"maza:{address}"
    d = Decimal(str(maza_amount)).quantize(Decimal("1E-8"))
    amount = format(d.normalize(), "f")
    if amount in ("", "-0"):
        amount = "0"
    return f"maza:{address}?amount={amount}"

# ISO/IEC 18004 quiet zone: 4 modules on every side.
QUIET_ZONE_MODULES = 4

# Requested error-correction level: M (~15% recoverable damage). The
# encoder may silently boost to a higher level when it fits the same
# version (boostecl default) — a QR at version 3-Q scans better than a
# version 4-M, so the boost is welcome; tests assert against the ACTUAL
# level via get_error_correction_level(), not this request.
_ECC = qrcodegen.QrCode.Ecc.MEDIUM


def address_svg(payload: str) -> str:
    """Render a payment QR code as a standalone SVG document string.

    One module = one viewBox unit; the consumer sizes it with CSS
    (width/height attributes on <img>), so the document itself is
    resolution-independent.
    """
    if not isinstance(payload, str) or not payload:
        raise ValueError("qr payload must be a non-empty string")
    code = qrcodegen.QrCode.encode_text(payload, _ECC)
    size = code.get_size()
    total = size + 2 * QUIET_ZONE_MODULES
    parts: List[str] = []
    for y in range(size):
        for x in range(size):
            if code.get_module(x, y):
                parts.append(
                    f"M{x + QUIET_ZONE_MODULES},{y + QUIET_ZONE_MODULES}h1v1h-1z"
                )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {total} {total}" shape-rendering="crispEdges" '
        'role="img" aria-label="MAZA payment address QR code">'
        f'<rect width="{total}" height="{total}" fill="#ffffff"/>'
        f'<path d="{"".join(parts)}" fill="#000000"/>'
        "</svg>"
    )
