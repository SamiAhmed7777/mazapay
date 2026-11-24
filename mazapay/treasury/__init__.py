"""Treasury workspace for MazaPay.

Sibling package to the payments bridge: accounts, approval requests, a
two-signature rail, a donor-funded humanitarian pool, statements and an
append-only audit trail. Runs as its own process (python3 -m
mazapay.treasury.server) against the same SQLite database, with every table
prefixed tr_.

See docs/CONTRACT-treasury.md for the pinned contract.
"""

__all__ = ["settings"]
