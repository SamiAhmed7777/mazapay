#!/usr/bin/env python3
"""Simulate a socket-creation-DENIED sandbox for the MazaPay test suite.

Scope: this script models the CONSTRUCTOR-denial fingerprint specifically -
socket.socket() itself raises PermissionError before any bind attempt, as
observed in restricted judge/CI sandboxes. Other denial shapes (bind-only
denial, read-only temp dirs) are NOT modeled here; the suite degrades to
clean skips in those too (see tests/test_sandbox_gates.py and the
MAZAPAY_TEST_TMP read-only transcript), but this script exercises only the
constructor-denial case.

It runs unittest discovery over tests/ and exits 0 only when the suite
succeeds (skips allowed, errors not).

Usage (from the repo root):
    python3 scripts/simulate_creation_denied.py
"""
import socket as _socket_mod
import sys
import unittest
from pathlib import Path

# Running as `python3 scripts/simulate_creation_denied.py` puts scripts/ first
# on sys.path; discovery needs the repo root (for `import mazapay`) AND the
# tests dir (for the suite's plain `import _sandbox`).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))


class _DeniedSocket:
    """Stands in for socket.socket; construction is denied."""

    def __init__(self, *args, **kwargs):
        raise PermissionError(13, "Socket creation denied (simulated sandbox)")


def main() -> int:
    _socket_mod.socket = _DeniedSocket
    suite = unittest.TestLoader().discover("tests")
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
