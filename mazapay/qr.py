"""QR-code SVG rendering for the checkout page (deposit address → code).

Payload convention (deliberate): the QR encodes the BARE deposit address.
The MazaCoin ecosystem has no established payment-URI scheme (mazacoin.org's
own homepage still carries a "(maza address and QR code go here)" placeholder;
the official paper-wallet generator scans bare addresses), and an invented
"scheme:addr?amount=x" string would scan as plain text in most mobile wallet
apps — worse for the payer than a clean address. The amount stays as displayed
text on the page; payment matching is by address watch and the page already
instructs "Send exactly this amount". If a community convention emerges
(Payu's call), the payload is one string at the call site in api.py.

Encoder: the vendored pure-Python library (Project Nayuki, MIT) — see
mazapay/vendor/__init__.py for the provenance pin. This module renders the
library's boolean module matrix to a self-contained SVG: white background
(the spec quiet zone AND scannability on the dark checkout card), one <path>
of unit squares. No order data ever enters the SVG text — only the boolean
matrix — so the document has no injection surface.
"""
from typing import List

from .vendor import qrcodegen

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
