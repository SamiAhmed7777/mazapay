"""Treasury store: schema, append-only audit triggers, hash chain, ref counters."""
import sqlite3
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

try:
    import _sandbox as _env
except ImportError:  # package-style invocation
    from tests import _sandbox as _env  # type: ignore

requires_writable = _env.requires_writable

from mazapay.treasury import store  # noqa: E402

# Import-safe in a read-only sandbox: opening a database is only possible
# with a writable temp dir, and every class here is @requires_writable.
if _env.WRITABLE:
    store.init_db()


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _account(**kw):
    return store.create_account(
        kw.pop("name", "Store test account"),
        _uniq("ST"),
        kw.pop("kind", "operating"),
        **kw,
    )


@requires_writable
class TestSchema(unittest.TestCase):
    def test_init_db_twice_is_idempotent(self):
        store.init_db()
        store.init_db()
        with store.db() as conn:
            names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        for table in (
            "tr_users",
            "tr_login_tokens",
            "tr_sessions",
            "tr_accounts",
            "tr_payees",
            "tr_requests",
            "tr_request_items",
            "tr_signatures",
            "tr_pool_donations",
            "tr_audit",
        ):
            self.assertIn(table, names)

    def test_indexes_present(self):
        with store.db() as conn:
            indexes = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        for name in (
            "idx_tr_requests_account_status",
            "idx_tr_requests_prepared_ts",
            "idx_tr_request_items_request",
            "idx_tr_signatures_request",
            "idx_tr_audit_object",
            "idx_tr_sessions_token",
        ):
            self.assertIn(name, indexes)

    def test_money_columns_are_text(self):
        with store.db() as conn:
            columns = {
                row["name"]: row["type"]
                for row in conn.execute("PRAGMA table_info(tr_requests)")
            }
            item_columns = {
                row["name"]: row["type"]
                for row in conn.execute("PRAGMA table_info(tr_request_items)")
            }
        self.assertEqual(columns["usd_total"], "TEXT")
        self.assertEqual(columns["maza_total"], "TEXT")
        self.assertEqual(columns["rate"], "TEXT")
        self.assertEqual(item_columns["usd_amount"], "TEXT")

    def test_account_money_roundtrips_as_decimal_text(self):
        acct = _account(cap_per_request_usd="1234.5", cap_daily_usd="9000.25")
        self.assertEqual(acct["cap_per_request_usd"], "1234.50")
        self.assertEqual(acct["cap_daily_usd"], "9000.25")
        self.assertIsInstance(acct["cap_per_request_usd"], str)


@requires_writable
class TestAuditAppendOnly(unittest.TestCase):
    def test_update_is_aborted_by_the_database(self):
        with store.write_db() as conn:
            store.append_audit(
                conn,
                actor_user_id="tester",
                action="trigger_probe",
                object_type="probe",
                object_id="probe_update",
                detail={"a": 1},
            )
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            with store.write_db() as conn:
                conn.execute("UPDATE tr_audit SET action='rewritten' WHERE object_id='probe_update'")
        self.assertIn("append-only", str(ctx.exception))

    def test_delete_is_aborted_by_the_database(self):
        with store.write_db() as conn:
            store.append_audit(
                conn,
                actor_user_id="tester",
                action="trigger_probe",
                object_type="probe",
                object_id="probe_delete",
                detail={"a": 2},
            )
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            with store.write_db() as conn:
                conn.execute("DELETE FROM tr_audit WHERE object_id='probe_delete'")
        self.assertIn("append-only", str(ctx.exception))

    def test_stale_prev_hash_is_refused_instead_of_forking(self):
        with store.db() as conn:
            row = conn.execute("SELECT hash FROM tr_audit ORDER BY id DESC LIMIT 1").fetchone()
        stale_head = row["hash"] if row else store.AUDIT_GENESIS
        with store.write_db() as conn:
            store.append_audit(
                conn,
                actor_user_id="tester",
                action="trigger_probe",
                object_type="probe",
                object_id="probe_head",
                detail={"a": 3},
            )
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            with store.write_db() as conn:
                conn.execute(
                    "INSERT INTO tr_audit (ts, actor_user_id, action, object_type,"
                    " object_id, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
                    (1.0, "rogue", "fork_attempt", "probe", "probe_fork",
                     "{}", stale_head, "0" * 64),
                )
        self.assertIn("chain fork", str(ctx.exception))
        ok, broken = store.verify_chain()
        self.assertTrue(ok, f"a refused insert must not fork the chain (broken at {broken})")


@requires_writable
class TestAuditChain(unittest.TestCase):
    def _append(self, object_id: str, marker: str = "x") -> int:
        with store.write_db() as conn:
            store.append_audit(
                conn,
                actor_user_id="tester",
                action="chain_probe",
                object_type="chain_probe",
                object_id=object_id,
                detail={"marker": marker},
            )
            row = conn.execute(
                "SELECT id FROM tr_audit WHERE object_id=? ORDER BY id DESC LIMIT 1",
                (object_id,),
            ).fetchone()
        return int(row["id"])

    def test_chain_verifies_after_appends(self):
        self._append(_uniq("chain"))
        ok, broken = store.verify_chain()
        self.assertTrue(ok, f"chain broken at {broken}")
        self.assertIsNone(broken)

    def test_chain_links_previous_hash(self):
        object_id = _uniq("chain-link")
        first = self._append(object_id, "one")
        second = self._append(object_id, "two")
        with store.db() as conn:
            rows = conn.execute(
                "SELECT id, prev_hash, hash FROM tr_audit WHERE id IN (?,?) ORDER BY id",
                (first, second),
            ).fetchall()
        self.assertEqual(rows[1]["prev_hash"], rows[0]["hash"])
        self.assertNotEqual(rows[0]["hash"], rows[1]["hash"])

    def test_broken_chain_detection_reports_first_broken_id(self):
        object_id = _uniq("chain-tamper")
        row_id = self._append(object_id, "original")
        try:
            with store.write_db() as conn:
                conn.execute("DROP TRIGGER tr_audit_block_update")
                conn.execute(
                    "UPDATE tr_audit SET detail=? WHERE id=?",
                    ('{"marker":"tampered"}', row_id),
                )
            ok, broken = store.verify_chain()
            self.assertFalse(ok)
            self.assertEqual(broken, str(row_id))
        finally:
            with store.write_db() as conn:
                conn.execute(
                    "UPDATE tr_audit SET detail=? WHERE id=?",
                    ('{"marker":"original"}', row_id),
                )
            store.init_db()  # restore the append-only trigger
        ok, broken = store.verify_chain()
        self.assertTrue(ok, f"restore failed, broken at {broken}")

    def test_scoped_verify_names_the_matching_row(self):
        object_id = _uniq("chain-scope")
        row_id = self._append(object_id, "scoped")
        try:
            with store.write_db() as conn:
                conn.execute("DROP TRIGGER tr_audit_block_update")
                conn.execute("UPDATE tr_audit SET action='rewritten' WHERE id=?", (row_id,))
            ok, broken = store.verify_chain(object_type="chain_probe", object_id=object_id)
            self.assertFalse(ok)
            self.assertEqual(broken, str(row_id))
        finally:
            with store.write_db() as conn:
                conn.execute("UPDATE tr_audit SET action='chain_probe' WHERE id=?", (row_id,))
            store.init_db()

    def test_audit_rows_filter(self):
        object_id = _uniq("chain-filter")
        self._append(object_id, "one")
        self._append(object_id, "two")
        rows = store.audit_rows(object_type="chain_probe", object_id=object_id)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["object_id"] == object_id for r in rows))
        self.assertEqual([r["detail"] for r in rows][0], '{"marker":"two"}')


@requires_writable
class TestCounters(unittest.TestCase):
    def test_next_counter_is_sequential(self):
        key = _uniq("counter")
        with store.write_db() as conn:
            self.assertEqual(store.next_counter(conn, key), 1)
            self.assertEqual(store.next_counter(conn, key), 2)

    def test_concurrent_allocation_yields_unique_values(self):
        key = _uniq("counter-threads")
        barrier = threading.Barrier(6)

        def allocate(_i):
            barrier.wait(timeout=5)  # contend for the write lock together
            with store.write_db() as conn:
                return store.next_counter(conn, key)

        with ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(allocate, range(6)))
        self.assertEqual(sorted(values), [1, 2, 3, 4, 5, 6])

    def test_to_money_rejects_floats_with_noise(self):
        self.assertEqual(store.to_money("12.345"), "12.34")
        self.assertEqual(store.to_money(Decimal("7")), "7.00")
        with self.assertRaises(ValueError):
            store.to_money("NaN")
        with self.assertRaises(ValueError):
            store.to_money("not money")


if __name__ == "__main__":
    unittest.main()
