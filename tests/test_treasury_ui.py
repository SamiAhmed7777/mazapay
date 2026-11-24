"""Treasury SECONDARY screens — page shape, escaping, assets, computed a11y.

LANE 4 owns ``pages_pool`` (pool / statements / audit / login), ``treasury.js``
and the lane-4 block at the end of ``treasury.css``. This suite is the contract
for those screens:

- shape   the pinned worklist is in the shipped bytes: the no-Tribal-funds
          banner, four compact KPI tiles, the three Section 5 categories with
          bars, payslip links, the MIP reconciliation export, the statement
          kind switch + month picker + totals, the audit filters and hash
          chain, and the login "check your email" state;
- safety  a ``<script>`` payload is escaped on every screen, there is no inline
          script body, no inline style attribute and no external origin;
- assets  ``treasury.js`` exposes exactly ``window.trPost`` / ``window.trFmt``
          and its behaviour is executed under node when a runtime is present;
- a11y    a COMPUTED contrast matrix over ``treasury.css``' own ``:root``
          tokens: every text/surface pair >= 4.5:1, every border/bar >= 3:1
          (WCAG 2.x 1.4.3 / 1.4.11), using the same sRGB-luminance math as
          ``tests/test_checkout_ui.py``. Skipped with an explicit message while
          Lane 3's stylesheet has not landed.
"""
import copy
import datetime as _dt
import html as _html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_treasury_ui)
    from tests import _sandbox as _env  # type: ignore

from mazapay.treasury import pages_pool  # noqa: E402

requires_writable = _env.requires_writable

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(pages_pool.__file__)))
CSS_PATH = os.path.join(PKG_DIR, "assets", "treasury.css")
JS_PATH = os.path.join(PKG_DIR, "assets", "treasury.js")

CSRF = "csrf-test-token-0001"
NONCE = "nonce-test-abc123"
PAYLOAD = '"><script>alert(1)</script>'

CURRENT_MONTH = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")

USER = {"id": "usr_0001", "email": "treasurer@example.org", "name": "Ada Redcloud",
        "role": "treasurer"}
VIEWER = {"id": "usr_0002", "email": "viewer@example.org", "name": "Vic Viewer",
          "role": "viewer"}

POOL_DATA = {
    "summary": {
        "balance_maza": "24100000.12345678",
        "balance_usd": "48200.00",
        "donated_month_usd": "7250.50",
        "paid_week_usd": "3100.00",
        "donors": 37,
        "categories": [
            {"category": "health", "usd": "1200.00"},
            {"category": "family", "usd": "600.00"},
            {"category": "elder", "usd": "1300.00"},
        ],
    },
    "stipends": [
        {"ref": "HSR-0088", "recipient": "Mary Redcloud", "category": "health",
         "usd": "450.00", "maza": "225000.00", "rate": "0.002", "rate_source": "fixed",
         "txid": "a" * 64, "ts": 1763000000.0, "status": "sent",
         "request_ref": "HSR-0088", "item_id": "rii_abc123def456"},
        {"ref": "HSR-0089", "recipient": "Elder Fuel Co-op", "category": "elder",
         "usd": "300.00", "maza": "150000.00", "rate": "0.002", "rate_source": "fixed",
         "txid": None, "ts": 1763003600.0, "status": "pending",
         "request_ref": None, "item_id": None},
    ],
    "can_donate": True,
    "can_run": False,
    "requests": [
        {"ref": "DR-2027-0142", "status": "pending_signatures", "usd_total": "900.00"},
        {"ref": "DR-2027-0141", "status": "completed", "usd_total": "120.00"},
    ],
}

STATEMENT_DATA = {
    "kind": "requests",
    "month": "2026-11",
    "rows": [
        {"ref": "DR-2027-0142", "date": 1763000000.0, "account_code": "OPS-100",
         "account_name": "Operating", "kind": "drawdown", "status": "completed",
         "usd": "900.00", "maza": "450000.00", "rate": "0.002", "rate_source": "fixed",
         "txids": ["b" * 64], "signers": ["exec@example.org", "council@example.org"],
         "purpose": "Water hauling"},
        {"ref": "HSR-0088", "date": "2026-11-04", "account_code": "POOL-500",
         "account_name": "Humanitarian pool", "kind": "stipend_run", "status": "declined",
         "usd": "450.00", "maza": "225000.00", "rate": "0.002", "rate_source": "fixed",
         "txids": [], "signers": [], "purpose": "Elder fuel stipend"},
    ],
    "totals": {"usd": "1350.00", "maza": "675000.00"},
}

AUDIT_DATA = {
    "rows": [
        {"id": 41, "ts": 1763000000.0, "actor_user_id": "usr_0001", "action": "login_ok",
         "object_type": "session", "object_id": "ses_abc123def456",
         "hash": "c" * 64, "prev_hash": "d" * 64, "detail": '{"ip": "127.0.0.1"}'},
        {"id": 40, "ts": 1762990000.0, "actor_user_id": None, "action": "request_completed",
         "object_type": "request", "object_id": "req_ffffeeeedddd",
         "hash": "d" * 64, "prev_hash": "0" * 64, "detail": ""},
    ],
    "object_type": "request",
    "object_id": "",
    "limit": 200,
}


# --------------------------------------------------------------------------- #
# page builders
# --------------------------------------------------------------------------- #
def build_pool(data=None, user=None):
    return pages_pool.render_pool(
        user=copy.deepcopy(user or USER), csrf=CSRF, nonce=NONCE,
        data=POOL_DATA if data is None else data,
    )


def build_statements(data=None, user=None):
    return pages_pool.render_statements(
        user=copy.deepcopy(user or USER), csrf=CSRF, nonce=NONCE,
        data=STATEMENT_DATA if data is None else data,
    )


def build_audit(data=None, user=None):
    return pages_pool.render_audit(
        user=copy.deepcopy(user or USER), csrf=CSRF, nonce=NONCE,
        data=AUDIT_DATA if data is None else data,
    )


def build_login(**kwargs):
    params: dict = {"csrf": CSRF, "nonce": NONCE}
    params.update(kwargs)
    return pages_pool.render_login(**params)


def all_pages():
    return {
        "pool": build_pool(),
        "statements": build_statements(),
        "audit": build_audit(),
        "login": build_login(),
        "login-sent": build_login(sent=True),
    }


def injected_pages():
    """Every screen built from data that tries to break out of HTML."""
    pool = copy.deepcopy(POOL_DATA)
    pool["stipends"][0]["recipient"] = PAYLOAD
    pool["stipends"][0]["ref"] = PAYLOAD
    user = dict(USER, name=PAYLOAD)
    statements = copy.deepcopy(STATEMENT_DATA)
    statements["rows"][0]["purpose"] = PAYLOAD
    statements["rows"][0]["account_name"] = PAYLOAD
    audit = copy.deepcopy(AUDIT_DATA)
    audit["rows"][0]["detail"] = PAYLOAD
    audit["rows"][0]["actor_user_id"] = PAYLOAD
    audit["object_type"] = PAYLOAD
    return {
        "pool": build_pool(pool, user),
        "statements": build_statements(statements, user),
        "audit": build_audit(audit, user),
        "login": build_login(error=PAYLOAD),
    }


# --------------------------------------------------------------------------- #
# contrast math (WCAG 2.x relative luminance — same as test_checkout_ui.py)
# --------------------------------------------------------------------------- #
def _lin(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"not a 6-digit hex colour: {hex_color!r}")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(css: str) -> dict:
    block = css.split(":root {", 1)[1].split("}", 1)[0]
    return dict(re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", block))


# --------------------------------------------------------------------------- #
# pool
# --------------------------------------------------------------------------- #
class TestPoolScreen(unittest.TestCase):
    def setUp(self):
        self.page = build_pool()

    def test_states_contains_no_tribal_funds(self):
        self.assertIn("contains no Tribal funds", self.page)
        self.assertIn('class="banner-strong"', self.page)

    def test_four_compact_kpi_tiles(self):
        self.assertEqual(self.page.count('<div class="kpi">'), 4, "four KPI tiles")
        for label in ("Pool balance", "Paid this week", "Donors", "Scheduled"):
            self.assertIn(label, self.page)
        # compact tiles: label / value / footnote, not a hero number
        self.assertEqual(self.page.count('<div class="k">'), 4)
        self.assertEqual(self.page.count('<div class="v">'), 4)
        self.assertEqual(self.page.count('<div class="d">'), 4)
        self.assertNotIn("hero-amount", self.page)
        # balance shows MAZA and the USD value at the locked rate
        self.assertIn("24,100,000.12 MAZA", self.page)
        self.assertIn("$48,200.00", self.page)
        self.assertIn("$3,100.00", self.page)  # paid this week
        self.assertIn("37", self.page)  # donors
        self.assertIn("$900.00 queued for signature", self.page)

    def test_three_section5_categories_with_bars(self):
        for label in ("Emergency health services", "Emergency family support",
                      "Elders &amp; vulnerable members"):
            self.assertIn(label, self.page)
        self.assertEqual(self.page.count('class="cat"'), 3)
        self.assertEqual(self.page.count('class="bar"'), 3)
        # a bar with a sized fill, and the amount right beside it
        self.assertIn('<span class="bar" role="img" aria-label="Emergency health services: 92%', self.page)
        self.assertIn('<span data-pct="92"></span>', self.page)
        self.assertIn("$1,200.00", self.page)

    def test_bar_widths_are_a_nonce_carried_style_block(self):
        # widths are data, so they ride a CSP-nonced <style>: never an inline attr
        self.assertIn(f'<style nonce="{NONCE}">', self.page)
        self.assertIn('.tr-cats .bar > span[data-pct="92"]{width:92%}', self.page)
        self.assertNotIn('style="', self.page)

    def test_stipend_table_links_the_payslip(self):
        self.assertIn('class="tbl"', self.page)
        self.assertIn("/treasury/payslips/rii_abc123def456", self.page)
        self.assertIn("Mary Redcloud", self.page)
        self.assertIn('class="pill pill-ok"', self.page)  # sent
        self.assertIn('class="pill pill-mut"', self.page)  # pending
        # a row without item_id gets no dead payslip link
        self.assertNotIn("/treasury/payslips/None", self.page)

    def test_export_reconciliation_hits_the_pool_csv(self):
        flat = _html.unescape(self.page)
        self.assertIn("/api/statements?kind=pool&format=csv", flat)
        self.assertIn("Export reconciliation (MIP)", self.page)

    def test_donation_form_shown_for_treasurer(self):
        self.assertIn('data-tr-json="/api/pool/donations"', self.page)
        for field in ("donor_label", "usd_amount", "txid", "note"):
            self.assertIn(f'name="{field}"', self.page)
        self.assertIn("Record donation", self.page)

    def test_donation_form_shown_for_admin(self):
        page = build_pool(user=dict(USER, role="admin"))
        self.assertIn('data-tr-json="/api/pool/donations"', page)

    def test_donation_form_hidden_for_viewer(self):
        page = build_pool(user=VIEWER)
        self.assertNotIn("/api/pool/donations", page)
        # A reader without the role gets no donation card at all (an empty
        # permission box is noise); the pool stays fully readable.
        self.assertNotIn("Record a donation", page)
        self.assertIn("contains no Tribal funds", page)

    def test_empty_pool_still_renders(self):
        page = build_pool({"summary": {}, "stipends": [], "requests": [],
                           "can_donate": False, "can_run": False})
        self.assertIn("contains no Tribal funds", page)
        self.assertIn("No stipend payments recorded", page)
        self.assertIn("$0.00", page)


# --------------------------------------------------------------------------- #
# statements
# --------------------------------------------------------------------------- #
class TestStatementsScreen(unittest.TestCase):
    def setUp(self):
        self.page = build_statements()

    def test_kind_switch_links_both_ledgers(self):
        flat = _html.unescape(self.page)
        self.assertIn("/treasury/statements?kind=requests&month=2026-11", flat)
        self.assertIn("/treasury/statements?kind=pool&month=2026-11", flat)
        # the active ledger is visually distinct and marked for AT
        self.assertIn('class="btn btn-primary btn-sm"', self.page)
        self.assertIn('aria-current="true"', self.page)

    def test_month_picker_defaults_to_the_current_utc_month(self):
        page = build_statements({"kind": "requests", "month": None, "rows": [],
                                 "totals": {"usd": "0", "maza": "0"}})
        self.assertIn('type="month"', page)
        self.assertIn(f'value="{CURRENT_MONTH}"', page)

    def test_totals_row_and_csv_link(self):
        flat = _html.unescape(self.page)
        self.assertIn("/api/statements?kind=requests&month=2026-11&format=csv", flat)
        self.assertIn("Total", self.page)
        self.assertIn("<tfoot>", self.page)
        self.assertIn("$1,350.00", self.page)
        self.assertIn("675,000.00", self.page)

    def test_rows_carry_ref_account_rate_and_status(self):
        for needle in ("DR-2027-0142", "OPS-100", "Operating", "Water hauling",
                       "0.002000", 'class="pill pill-ok"', 'class="pill pill-red"'):
            self.assertIn(needle, self.page)
        # amounts are right-aligned
        self.assertIn('<td class="n">$900.00</td>', self.page)

    def test_pool_kind_is_labelled(self):
        page = build_statements({"kind": "pool", "month": "2026-10", "rows": [],
                                 "totals": {"usd": "0", "maza": "0"}})
        self.assertIn("Donor pool", page)
        self.assertIn("October 2026", page)
        self.assertIn("No rows for this month", page)


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
class TestAuditScreen(unittest.TestCase):
    def setUp(self):
        self.page = build_audit()

    def test_filters_post_to_the_audit_route(self):
        self.assertIn('action="/treasury/audit"', self.page)
        self.assertIn('name="object_type"', self.page)
        self.assertIn('name="object_id"', self.page)
        self.assertIn('value="request"', self.page)  # current filter echoed

    def test_rows_show_short_hash_and_prev_hash(self):
        self.assertIn("c" * 12 + "…" + "c" * 4, self.page)
        self.assertIn("d" * 12 + "…" + "d" * 4, self.page)
        # full digests are still available on hover
        self.assertIn(f'title="{"c" * 64}"', self.page)
        self.assertIn("login_ok", self.page)
        self.assertIn("request_completed", self.page)

    def test_says_the_chain_is_verifiable(self):
        self.assertIn("The chain is verifiable", self.page)
        self.assertIn("prev_hash", self.page)
        self.assertIn("aborts UPDATE and DELETE", self.page)

    def test_empty_state(self):
        page = build_audit({"rows": [], "object_type": "", "object_id": "", "limit": 200})
        self.assertIn("No audit rows match this filter", page)


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
class TestLoginScreen(unittest.TestCase):
    def test_single_email_field_posts_to_the_login_route(self):
        page = build_login()
        self.assertIn('action="/treasury/login"', page)
        self.assertIn('method="post"', page)
        self.assertIn('name="email"', page)
        self.assertIn('type="email"', page)
        self.assertEqual(page.count('type="email"'), 1, "exactly one email field")
        self.assertIn("treasury workspace", page)
        self.assertNotIn('type="password"', page)

    def test_sent_state_asks_you_to_check_your_email(self):
        page = build_login(sent=True)
        self.assertIn("Check your email", page)
        self.assertNotIn('name="email"', page, "the form is replaced by the sent state")

    def test_no_token_in_the_sent_state_without_a_demo_link(self):
        page = build_login(sent=True)
        self.assertNotIn("/treasury/auth/", page)
        for shape in ("?token=", "token=", "trt_"):
            self.assertNotIn(shape, page)

    def test_demo_link_is_shown_and_stripped_of_its_origin(self):
        page = build_login(sent=True, demo_link="http://127.0.0.1:18501/treasury/auth/tok_abc")
        self.assertIn("/treasury/auth/tok_abc", page)
        self.assertIn("Demo mode", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_error_is_shown_without_echoing_credentials(self):
        page = build_login(error="That link has expired.")
        self.assertIn("That link has expired.", page)
        self.assertIn('role="alert"', page)


# --------------------------------------------------------------------------- #
# escaping + CSP hygiene
# --------------------------------------------------------------------------- #
class TestEscapingAndCsp(unittest.TestCase):
    def setUp(self):
        self.pages = injected_pages()

    def test_script_injection_is_escaped_everywhere(self):
        for name, page in self.pages.items():
            self.assertNotIn('"><script>', page, name)
            self.assertNotIn("<script>alert(1)</script>", page, name)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page, name)
            self.assertIn("&quot;&gt;", page, name)

    def test_no_inline_script_body(self):
        for name, page in all_pages().items():
            scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", page, re.S)
            self.assertTrue(scripts, f"{name} loads treasury.js")
            for attrs, body in scripts:
                self.assertEqual(body.strip(), "", f"{name}: inline script body")
                self.assertIn('src="/static/treasury.js"', attrs, name)
                self.assertIn("nonce=", attrs, name)

    def test_no_inline_handlers(self):
        for name, page in all_pages().items():
            self.assertNotIn("onclick=", page, name)
            self.assertNotIn("onsubmit=", page, name)
            self.assertNotIn("onload=", page, name)

    def test_no_inline_style_attribute(self):
        for name, page in all_pages().items():
            self.assertNotIn('style="', page, name)

    def test_no_external_origin(self):
        for name, page in all_pages().items():
            self.assertNotIn("http://", page, name)
            self.assertNotIn("https://", page, name)
            self.assertNotIn("//cdn", page, name)
            self.assertNotIn("@import", page, name)

    def test_no_unfilled_template_slots(self):
        for name, page in all_pages().items():
            self.assertIsNone(re.search(r"\{\{[A-Z0-9_]+\}\}", page), name)

    def test_only_whitelisted_script_and_style_sources(self):
        for name, page in all_pages().items():
            for src in re.findall(r'<script[^>]*src="([^"]+)"', page):
                self.assertEqual(src, "/static/treasury.js", name)
            for href in re.findall(r'<link[^>]*href="([^"]+)"', page):
                self.assertTrue(
                    href.startswith("/static/"),
                    f"{name}: link href must stay same-origin, got {href}",
                )


# --------------------------------------------------------------------------- #
# treasury.js
# --------------------------------------------------------------------------- #
class TestTreasuryJs(unittest.TestCase):
    def setUp(self):
        with open(JS_PATH, "r", encoding="utf-8") as fh:
            self.js = fh.read()

    def test_exposes_exactly_two_globals(self):
        assigned = set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", self.js))
        self.assertEqual(assigned, {"trPost", "trFmt"}, assigned)

    def test_strict_mode_and_no_inline_or_external_imports(self):
        self.assertIn("'use strict';", self.js)
        self.assertNotIn("http://", self.js)
        self.assertNotIn("https://", self.js)
        self.assertNotRegex(self.js, r"(?m)^\s*import\s")
        self.assertNotIn("onclick", self.js)

    def test_reads_the_csrf_cookie_and_the_error_envelope(self):
        self.assertIn("mazapay_tr_csrf", self.js)
        self.assertIn("X-CSRF-Token", self.js)
        self.assertIn("data.error", self.js)
        self.assertIn("throw err", self.js)

    def test_progressive_enhancement_target(self):
        self.assertIn("data-tr-json", self.js)
        self.assertIn("[data-tr-status]", self.js)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    @unittest.skipUnless(_env.WRITABLE, "needs a writable temp dir for node")
    def test_behaviour_under_node(self):
        harness = """
'use strict';
global.window = {};
const calls = [];
global.document = {
  cookie: 'mazapay_tr_csrf=tok%2B123; other=1',
  readyState: 'complete',
  documentElement: {
    attrs: {},
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = v; }
  },
  querySelectorAll() { return []; },
  addEventListener() {}
};
let next = { ok: true, status: 201, text: () => Promise.resolve('{"donation":{"id":"dn_1"}}') };
global.fetch = (url, opts) => { calls.push({ url, opts }); return Promise.resolve(next); };
require(__JS__);
(async () => {
  const out = {
    keys: Object.keys(window).sort(),
    fmt: [window.trFmt(123456789), window.trFmt(-5), window.trFmt(0), window.trFmt('nope')]
  };
  const ok = await window.trPost('/api/pool/donations', { usd_amount: '250.00' });
  out.ok = ok;
  out.call = { url: calls[0].url, method: calls[0].opts.method, body: calls[0].opts.body,
               csrf: calls[0].opts.headers['X-CSRF-Token'] };
  next = { ok: false, status: 409,
           text: () => Promise.resolve('{"error":{"code":"insufficient_role","message":"Your role cannot record donations."}}') };
  try { await window.trPost('/api/x', {}); out.err = null; }
  catch (e) { out.err = { message: e.message, status: e.status, code: e.code }; }
  console.log(JSON.stringify(out));
})();
""".replace("__JS__", json.dumps(JS_PATH))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "harness.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(harness)
            node = shutil.which("node") or "node"
            proc = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["keys"], ["trFmt", "trPost"])
        self.assertEqual(out["fmt"][0], "$1,234,567.89")
        self.assertEqual(out["fmt"][1], "-$0.05")
        self.assertEqual(out["fmt"][2], "$0.00")
        self.assertEqual(out["fmt"][3], "\u2014")
        self.assertEqual(out["ok"], {"donation": {"id": "dn_1"}})
        self.assertEqual(out["call"]["method"], "POST")
        self.assertEqual(out["call"]["url"], "/api/pool/donations")
        self.assertEqual(json.loads(out["call"]["body"]), {"usd_amount": "250.00"})
        self.assertEqual(out["call"]["csrf"], "tok+123")
        self.assertEqual(out["err"]["status"], 409)
        self.assertEqual(out["err"]["code"], "insufficient_role")
        self.assertEqual(out["err"]["message"], "Your role cannot record donations.")


# --------------------------------------------------------------------------- #
# contrast matrix over the shipped stylesheet
# --------------------------------------------------------------------------- #
CSS_MISSING = "TREASURY_CSS_MISSING — rerun after Lane 3 lands"


@unittest.skipUnless(os.path.isfile(CSS_PATH), CSS_MISSING)
class TestContrastMatrix(unittest.TestCase):
    """Every declared pair, computed from treasury.css' own tokens."""

    TEXT_PAIRS = [
        ("text", "surface", "body copy and table values"),
        ("text", "surface-2", "KPI values, input text, inset panels"),
        ("text", "bg", "topbar, page background"),
        ("muted", "surface", "labels, table heads, note copy on a card"),
        ("muted", "surface-2", "ghost button label, KPI footnote"),
        ("muted", "bg", "org name and nav on the page background"),
        ("faint", "surface", "hash digests, card-h right-hand hints"),
        ("faint", "bg", "fine print outside a card"),
        ("amber-soft", "surface", "links and .text accents"),
        ("amber-soft", "surface-2", "demo chip text"),
        ("amber-soft", "amber-tint", "pill-amber / mode chip"),
        ("ok", "surface", "success copy on a card"),
        ("ok", "surface-2", "success copy on an inset panel"),
        ("ok", "ok-tint", "pill-ok"),
        ("red-text", "surface", "error copy on a card"),
        ("red-text", "surface-2", "error copy on an inset panel"),
        ("red-text", "red-tint", "pill-red / banner-red"),
        ("ink", "amber", "primary button label"),
        ("ink", "amber-soft", "primary button hover"),
    ]
    # WCAG 1.4.11: the boundaries that carry meaning need 3:1.
    UI_PAIRS = [
        ("line-strong", "surface-2", "input / select / textarea border"),
        ("line-strong", "surface", "card-adjacent strong boundary"),
        ("amber", "surface-2", "category bar fill against its track"),
        ("amber", "surface", "category bar is visible on the card"),
        ("amber", "bg", "focus ring on the page background"),
    ]

    def setUp(self):
        with open(CSS_PATH, "r", encoding="utf-8") as fh:
            self.css = fh.read()
        self.tokens = _tokens(self.css)

    def test_tokens_captured(self):
        for key in ("bg", "surface", "surface-2", "line", "line-strong", "text", "muted",
                    "amber", "amber-soft", "ink", "red", "red-text", "ok", "ok-tint",
                    "amber-tint", "red-tint", "faint"):
            self.assertIn(key, self.tokens, key)

    def test_text_contrast_floor(self):
        failures = []
        for fg, bg, what in self.TEXT_PAIRS:
            ratio = contrast(self.tokens[fg], self.tokens[bg])
            if ratio < 4.5:
                failures.append(f"{what}: {fg} on {bg} = {ratio:.2f} < 4.5")
        self.assertEqual(failures, [], failures)

    def test_ui_component_contrast_floor(self):
        failures = []
        for fg, bg, what in self.UI_PAIRS:
            ratio = contrast(self.tokens[fg], self.tokens[bg])
            if ratio < 3.0:
                failures.append(f"{what}: {fg} on {bg} = {ratio:.2f} < 3.0")
        self.assertEqual(failures, [], failures)

    def test_faint_never_lands_on_a_tint(self):
        """--faint is below 4.5:1 on --ok-tint, so faint copy stays on surfaces and
        the tinted pills use the --ok / --amber-soft / --red-text pair instead."""
        self.assertLess(
            contrast(self.tokens["faint"], self.tokens["ok-tint"]), 4.5,
            "--faint became text-safe on --ok-tint; the faint/muted split is now wrong",
        )
        for tint in ("ok-tint", "amber-tint", "red-tint"):
            self.assertGreaterEqual(
                contrast(self.tokens["muted"], self.tokens[tint]), 4.5,
                f"--muted must carry text on {tint}",
            )

    def test_brand_red_is_not_used_for_text(self):
        for bg in ("surface", "surface-2", "bg"):
            self.assertLess(contrast(self.tokens["red"], self.tokens[bg]), 4.5)
        self.assertIsNone(re.search(r"(?<!-)color:\s*var\(--red\)", self.css))


@unittest.skipUnless(os.path.isfile(CSS_PATH), CSS_MISSING)
class TestLane4StylesheetBlock(unittest.TestCase):
    def setUp(self):
        with open(CSS_PATH, "r", encoding="utf-8") as fh:
            self.css = fh.read()

    def test_marked_block_exists(self):
        self.assertIn("/* ==== lane4:secondary screens ==== */", self.css)

    def test_block_does_not_redeclare_tokens(self):
        block = self.css.split("/* ==== lane4:secondary screens ==== */", 1)[1]
        self.assertNotIn(":root", block)
        for token in ("--bg", "--surface", "--text", "--amber", "--red", "--ok"):
            self.assertNotIn(f"{token}:", block)

    def test_rules_used_by_the_pages_are_shipped(self):
        for selector in (".tbl-wrap", ".tr-cats .cat", ".tr-login", "input[type=month]"):
            self.assertIn(selector, self.css)


# --------------------------------------------------------------------------- #
# fallback shell (only used if Lane 3's layout module is unavailable)
# --------------------------------------------------------------------------- #
class TestFallbackShell(unittest.TestCase):
    def setUp(self):
        self.doc = pages_pool._local_shell(
            title="Pool", body="<p>ok</p>", user=USER, csrf=CSRF, nonce=NONCE, active="pool"
        )

    def test_carries_the_shared_tokens(self):
        root = _tokens(self.doc)
        self.assertEqual(root.get("bg"), "#0b0d12")
        self.assertEqual(root.get("surface"), "#151b24")
        self.assertEqual(root.get("amber"), "#f0a000")
        self.assertEqual(root.get("faint"), "#7b8698")

    def test_is_self_contained(self):
        self.assertIn(f'<style nonce="{NONCE}">', self.doc)
        self.assertIn(f'<script nonce="{NONCE}" src="/static/treasury.js"', self.doc)
        self.assertNotIn("http://", self.doc)
        self.assertNotIn("https://", self.doc)
        self.assertIn('class="nav"', self.doc)


if __name__ == "__main__":
    unittest.main()
