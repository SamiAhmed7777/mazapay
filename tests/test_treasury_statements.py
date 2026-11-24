"""Treasury statements: month filtering, MIP-shaped CSV, payslip data."""
import csv
import datetime
import io
import unittest
import uuid

try:
    import _sandbox as _env
except ImportError:
    from tests import _sandbox as _env  # type: ignore

requires_writable = _env.requires_writable

from mazapay.treasury import service, statements, store  # noqa: E402
from mazapay.treasury.service import TreasuryError  # noqa: E402

# Import-safe in a read-only sandbox: opening a database is only possible
# with a writable temp dir, and every class here is @requires_writable.
if _env.WRITABLE:
    store.init_db()

HEADER = "REF,DATE,ACCOUNT,KIND,STATUS,USD,MAZA,RATE,RATE_SOURCE,TXIDS,SIGNERS,DESCRIPTION"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _user(role: str) -> dict:
    tag = uuid.uuid4().hex[:6]
    return {"id": f"u_{role}_{tag}", "email": f"{role}.{tag}@example.test",
            "name": f"{role.title()} {tag}", "role": role}


def _address() -> str:
    return "M" + uuid.uuid4().hex[:16]


class StatementCase(unittest.TestCase):
    def setUp(self):
        self.fin = _user("finance_officer")
        self.treas = _user("treasurer")
        self.admin = _user("admin")
        self.acct = store.create_account(
            "Statements account", _uniq("STMT"), "operating", signature_policy="single"
        )
        self.calls = []

    def make(self, *, purpose="Programme run", usd="100.00", kind="payout"):
        return service.create_request(
            self.fin,
            account_id=self.acct["id"],
            kind=kind,
            purpose=purpose,
            items=[
                {
                    "payee_name": "Payee One",
                    "payee_address": _address(),
                    "usd_amount": usd,
                    "category": "health",
                }
            ],
        )

    def pay(self, req):
        req = service.process_request(self.fin, req["id"])
        req = service.submit_request(self.fin, req["id"])
        req = service.sign_request(self.treas, req["id"], decision="approve")
        self.assertEqual(req["status"], "authorized")
        req = service.complete_request(self.treas, req["id"])
        self.assertEqual(req["status"], "completed")
        return req

    def pay_with_fake(self, req):
        def disburse(payload):
            self.calls.append(dict(payload))
            return {
                "id": f"pay_{uuid.uuid4().hex[:8]}",
                "status": "sent",
                "txid": uuid.uuid4().hex,
            }

        from unittest import mock

        with mock.patch("mazapay.api.disburse", side_effect=disburse):
            return self.pay(req)

    def backdate(self, request_id, iso_date):
        ts = datetime.datetime.strptime(iso_date, "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc
        ).timestamp()
        with store.write_db() as conn:
            conn.execute(
                "UPDATE tr_requests SET prepared_ts=? WHERE id=?", (ts, request_id)
            )
        return ts


@requires_writable
class TestMonthBounds(unittest.TestCase):
    def test_bounds_are_utc_month_edges(self):
        start, end = statements.month_bounds("2026-11")
        start_dt = datetime.datetime.fromtimestamp(start, datetime.timezone.utc)
        end_dt = datetime.datetime.fromtimestamp(end, datetime.timezone.utc)
        self.assertEqual((start_dt.year, start_dt.month, start_dt.day), (2026, 11, 1))
        self.assertEqual((end_dt.year, end_dt.month, end_dt.day), (2026, 12, 1))

    def test_december_rolls_into_next_year(self):
        start, end = statements.month_bounds("2026-12")
        end_dt = datetime.datetime.fromtimestamp(end, datetime.timezone.utc)
        self.assertEqual((end_dt.year, end_dt.month), (2027, 1))

    def test_bad_month_rejected(self):
        for bad in ("2026-13", "2026", "nope", "2026-1"):
            with self.assertRaises(ValueError):
                statements.month_bounds(bad)

    def test_resolve_month_defaults_to_current(self):
        self.assertEqual(len(statements.resolve_month(None)), 7)
        self.assertEqual(statements.resolve_month("2026-02"), "2026-02")


@requires_writable
class TestStatementRows(StatementCase):
    def test_month_filtering_on_prepared_ts(self):
        current = self.make(purpose="current month")
        old = self.make(purpose="old month")
        self.backdate(old["id"], "2020-01-15")

        this_month = statements.statement_rows(kind="requests", month=statements.current_month())
        refs = {row["ref"] for row in this_month}
        self.assertIn(current["ref"], refs)
        self.assertNotIn(old["ref"], refs)

        then = statements.statement_rows(kind="requests", month="2020-01")
        then_refs = {row["ref"] for row in then}
        self.assertIn(old["ref"], then_refs)
        self.assertNotIn(current["ref"], then_refs)

    def test_row_shape_and_decimal_strings(self):
        req = self.pay_with_fake(self.make(purpose="shape check"))
        rows = statements.statement_rows(kind="requests", month=statements.current_month())
        row = next(r for r in rows if r["ref"] == req["ref"])
        for key in (
            "ref", "date", "account_code", "account_name", "kind", "status",
            "usd", "maza", "rate", "rate_source", "txids", "signers", "purpose",
        ):
            self.assertIn(key, row)
        self.assertEqual(row["account_code"], self.acct["code"])
        self.assertEqual(row["account_name"], "Statements account")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["usd"], "100.00")
        self.assertEqual(row["maza"], "50000.00")
        self.assertEqual(row["rate"], "0.002")
        self.assertEqual(row["rate_source"], "fixed")
        self.assertEqual(row["kind"], "payout")
        self.assertEqual(row["purpose"], "shape check")
        self.assertIsInstance(row["txids"], list)
        self.assertEqual(len(row["txids"]), 1)
        self.assertEqual(row["signers"], [self.treas["id"]])
        date = datetime.datetime.strptime(row["date"], "%Y-%m-%d")
        self.assertIsInstance(date, datetime.datetime)

    def test_draft_request_appears_without_txids(self):
        req = self.make(purpose="not yet paid")
        rows = statements.statement_rows(kind="requests", month=statements.current_month())
        row = next(r for r in rows if r["ref"] == req["ref"])
        self.assertEqual(row["status"], "prepared")
        self.assertEqual(row["txids"], [])
        self.assertEqual(row["maza"], "")

    def test_pool_statement_kind(self):
        donation = service.record_donation(
            self.treas, donor_label=_uniq("Donor"), usd_amount="25.00", txid="abc123",
            note="seed",
        )
        rows = statements.statement_rows(kind="pool", month=statements.current_month())
        row = next(r for r in rows if r["ref"] == donation["id"])
        self.assertEqual(row["kind"], "donation")
        self.assertEqual(row["status"], "received")
        self.assertEqual(row["usd"], "25.00")
        self.assertEqual(row["maza"], "12500.00")
        self.assertEqual(row["account_code"], statements.POOL_ACCOUNT_CODE)
        self.assertEqual(row["txids"], ["abc123"])
        self.assertIn("seed", row["purpose"])

        request_rows = statements.statement_rows(kind="requests", month=statements.current_month())
        self.assertNotIn(donation["id"], {r["ref"] for r in request_rows})

    def test_unknown_statement_kind_rejected(self):
        with self.assertRaises(TreasuryError) as ctx:
            statements.statement_rows(kind="wallets", month=statements.current_month())
        self.assertEqual(ctx.exception.code, "invalid_kind")


@requires_writable
class TestStatementCsv(StatementCase):
    def _rows(self, req):
        rows = statements.statement_rows(kind="requests", month=statements.current_month())
        return [row for row in rows if row["ref"] == req["ref"]]

    def _parse(self, text):
        reader = csv.DictReader(io.StringIO(text))
        return (reader.fieldnames or []), list(reader)

    def test_header_is_exactly_the_contract_header(self):
        req = self.make()
        text = statements.statement_csv(self._rows(req))
        self.assertTrue(text.startswith(HEADER + "\r\n"), text.splitlines()[0])
        fieldnames, rows = self._parse(text)
        self.assertEqual(",".join(fieldnames), HEADER)
        self.assertEqual(len(rows), 1)

    def test_csv_carries_the_row_values(self):
        req = self.pay_with_fake(self.make(purpose="CSV values"))
        text = statements.statement_csv(self._rows(req))
        _names, rows = self._parse(text)
        row = rows[0]
        self.assertEqual(row["REF"], req["ref"])
        self.assertEqual(row["ACCOUNT"], self.acct["code"])
        self.assertEqual(row["USD"], "100.00")
        self.assertEqual(row["MAZA"], "50000.00")
        self.assertEqual(row["RATE_SOURCE"], "fixed")
        self.assertEqual(row["TXIDS"], req["items"][0]["txid"])
        self.assertEqual(row["SIGNERS"], self.treas["id"])
        self.assertEqual(row["DESCRIPTION"], "CSV values")

    def test_formula_purpose_is_neutralised(self):
        req = self.make(purpose="=SUM(A1:A2)")
        text = statements.statement_csv(self._rows(req))
        _names, rows = self._parse(text)
        self.assertEqual(rows[0]["DESCRIPTION"], "'=SUM(A1:A2)")

    def test_all_formula_prefixes_neutralised(self):
        for purpose in ("=1+1", "+HYPERLINK(1)", "@cmd", "-2+3"):
            req = self.make(purpose=purpose)
            _names, rows = self._parse(statements.statement_csv(self._rows(req)))
            self.assertEqual(rows[0]["DESCRIPTION"], "'" + purpose, purpose)

    def test_plain_negative_numbers_are_not_quoted(self):
        req = self.make()
        row = dict(self._rows(req)[0])
        row["rate"] = "-0.25"
        text = statements.statement_csv([row])
        _names, rows = self._parse(text)
        self.assertEqual(rows[0]["RATE"], "-0.25")

    def test_commas_in_purpose_survive_a_roundtrip(self):
        purpose = "Clinic, pharmacy, and transport"
        req = self.make(purpose=purpose)
        _names, rows = self._parse(statements.statement_csv(self._rows(req)))
        self.assertEqual(rows[0]["DESCRIPTION"], purpose)

    def test_csv_for_month_helpers(self):
        req = self.make(purpose="helper path")
        text = statements.csv_for_month(kind="requests", month=statements.current_month())
        self.assertIn(req["ref"], text)
        self.assertTrue(text.startswith(HEADER))


@requires_writable
class TestPayslip(StatementCase):
    def test_payslip_has_every_required_field(self):
        req = self.pay_with_fake(self.make(purpose="Stipend November"))
        item = req["items"][0]
        slip = statements.payslip(item["id"])
        self.assertEqual(slip["ref"], req["ref"])
        self.assertEqual(slip["recipient"], "Payee One")
        self.assertEqual(slip["category"], "health")
        self.assertEqual(slip["usd"], "100.00")
        self.assertEqual(slip["maza"], "50000.00")
        self.assertEqual(slip["rate"], "0.002")
        self.assertEqual(slip["rate_source"], "fixed")
        self.assertEqual(slip["txid"], item["txid"])
        self.assertEqual(slip["date"], datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))
        self.assertEqual(slip["account_name"], "Statements account")
        self.assertEqual(slip["purpose"], "Stipend November")
        self.assertEqual(slip["status"], "sent")
        self.assertEqual(slip["value_note"], "USD value at receipt")
        self.assertEqual(slip["payee_address"], item["payee_address"])

    def test_payslip_reconciles_with_the_audit_trail(self):
        req = self.pay_with_fake(self.make(purpose="Payslip audit"))
        slip = statements.payslip(req["items"][0]["id"])
        rows = store.audit_rows(object_type="request_item", object_id=req["items"][0]["id"])
        self.assertTrue(rows)
        sent = next(r for r in rows if r["action"] == "item_sent")
        import json

        detail = json.loads(sent["detail"])
        self.assertEqual(detail["txid"], slip["txid"])
        self.assertEqual(detail["payment_id"], req["items"][0]["payment_id"])
        self.assertEqual(detail["usd"], slip["usd"])

    def test_unknown_payslip_is_404(self):
        with self.assertRaises(TreasuryError) as ctx:
            statements.payslip("item_missing" + uuid.uuid4().hex[:4])
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
