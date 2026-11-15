#!/usr/bin/env python3
"""Simulate a socket-BIND-denied sandbox for the MazaPay test suite.

Scope: this script models the BIND-denial fingerprint specifically -
socket.socket() construction succeeds, but .bind() raises PermissionError.
(The sibling script, simulate_creation_denied.py, models the case where the
constructor itself is denied.) Both scripts run unittest discovery over
tests/ and exit 0 only when the suite succeeds (skips allowed, errors not).

Usage (from the repo root):
    python3 scripts/simulate_bind_denied.py
"""
import socket as _socket_mod
import sys
import unittest
from pathlib import Path

# Running as `python3 scripts/simulate_bind_denied.py` puts scripts/ first on
# sys.path; discovery needs the repo root (for `import mazapay`) AND the tests
# dir (for the suite's plain `import _sandbox`).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))


class _BindDeniedSocket:
    """Construction succeeds; every bind is denied."""

    def __init__(self, *args, **kwargs):
        pass

    def bind(self, *args, **kwargs):
        raise PermissionError(13, "bind denied (simulated sandbox)")

    def close(self):
        pass


def main() -> int:
    _socket_mod.socket = _BindDeniedSocket
    suite = unittest.TestLoader().discover("tests")
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
