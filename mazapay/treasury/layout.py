"""Treasury presentation layer: the HTML shell and the core screens.

Two jobs, both deliberately dull:

- ``render_shell`` is the ONE full-document wrapper. Every page — Lane 3's
  screens and Lane 4's ``pages_pool`` renderers — is handed to it, so the CSP
  nonce, nav, user chip and demo banner can never drift between pages.
- ``render_*_page`` build the Accounts / Requests / Request / Users / Payslip
  bodies from plain dicts (whatever ``service`` returned). No domain logic
  lives here: roles only decide which buttons are *shown*; ``service`` still
  refuses the action if the caller is not allowed.

Escaping: every value that came from a database row, a form or a URL is passed
through :func:`esc`. The only trusted strings are this module's own markup and
the templates under ``templates/treasury/``. There is no template engine and no
inline script — placeholders are ``{{name}}`` tokens filled by ``str``
substitution (stdlib only, contract §1).
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import settings

_LOG = logging.getLogger("mazapay.treasury.render")

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "treasury"
)

_TOKEN_RE = re.compile(r"\{\{([a-z_]+)\}\}")

# Nav order is pinned by the brief and by contract §10.
NAV_ITEMS: Sequence[tuple[str, str, str]] = (
    ("accounts", "/treasury/accounts", "Accounts"),
    ("requests", "/treasury/requests", "Requests"),
    ("pool", "/treasury/pool", "Pool"),
    ("statements", "/treasury/statements", "Statements"),
    ("audit", "/treasury/audit", "Audit"),
    ("users", "/treasury/users", "Users"),
)

# Contract §5, rendered server-side. The service re-checks; this only decides
# which controls are visible.
_ACTION_ROLES = {
    "process": {"admin", "treasurer", "finance_officer"},
    "submit": {"admin", "treasurer", "finance_officer"},
    "cancel": {"admin", "treasurer", "finance_officer"},
    "complete": {"admin", "treasurer"},
    "manage_users": {"admin"},
}

# Contract §5/§6: who the signature policy calls for. admin never signs.
_SIGNING_ROLES = {
    "single": {"treasurer"},
    "dual": {"executive", "council_treasurer"},
}

_STATUS_LABELS = {
    "prepared": ("pill-mut", "Prepared"),
    "processed": ("pill-amber", "Processed"),
    "pending_signatures": ("pill-amber", "Awaiting signatures"),
    "authorized": ("pill-ok", "Authorized"),
    "completed": ("pill-ok", "Completed"),
    "completed_partial": ("pill-red", "Completed · partial"),
    "cancelled": ("pill-mut", "Cancelled"),
    "declined": ("pill-red", "Declined"),
}

_STATUS_HINTS = {
    "prepared": "Process the request to lock the quote.",
    "processed": "Submit for signature when the quote is still valid.",
    "pending_signatures": "Waiting on the signature quorum.",
    "authorized": "Authorised — ready to broadcast.",
    "completed": "Settled on chain.",
    "completed_partial": "Some items failed; completion is retryable.",
    "cancelled": "Cancelled before settlement.",
    "declined": "A signer declined this request.",
}

_ROLE_LABELS = {
    "admin": "Administrator",
    "executive": "Executive",
    "council_treasurer": "Council treasurer",
    "treasurer": "Treasurer",
    "finance_officer": "Finance officer",
    "viewer": "Viewer",
}

_ROLE_ORDER = ("admin", "executive", "council_treasurer", "treasurer", "finance_officer", "viewer")

# Account kinds carry the product's own language.
_KIND_LABELS = {
    "operating": "USD cash leg",
    "drawdown": "Drawdown account",
    "federal_oversight": "Federal oversight",
    "pool": "Humanitarian pool",
}


# --------------------------------------------------------------- primitives --
def esc(value: Any) -> str:
    """Escape untrusted text for HTML element content and attribute values."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _fill(template: str, mapping: Dict[str, str]) -> str:
    def repl(match: "re.Match[str]") -> str:
        return mapping.get(match.group(1), "")

    return _TOKEN_RE.sub(repl, template)


def _template(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, f"{name}.html"), "r", encoding="utf-8") as fh:
        return fh.read()


def _money(value: Any, *, symbol: str = "$", suffix: str = "") -> str:
    """Format a decimal-string amount; never a float, never a crash."""
    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return esc(value)
    if not amount.is_finite():
        return esc(value)
    body = f"{amount:,.2f}"
    tail = f" {suffix}" if suffix else ""
    return f"{esc(symbol)}{body}{tail}" if symbol else f"{body}{tail}"


def _rate_text(value: Any) -> str:
    """A USD/MAZA rate keeps its significant digits (0.002, not 0.00)."""
    if value in (None, ""):
        return "—"
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return esc(value)
    if not rate.is_finite() or rate <= 0:
        return "—"
    text = format(rate.normalize(), "f")
    if "." in text and len(text.split(".")[1]) < 4:
        text = f"{rate:.4f}"
    return "$" + text


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _dt(ts: Any) -> str:
    try:
        stamp = float(ts)
    except (TypeError, ValueError):
        return "—"
    if stamp <= 0:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(stamp))


def _day(ts: Any) -> str:
    try:
        stamp = float(ts)
    except (TypeError, ValueError):
        return "—"
    if stamp <= 0:
        return "—"
    return time.strftime("%Y-%m-%d", time.gmtime(stamp))


def _role_label(role: Any) -> str:
    key = str(role or "")
    return _ROLE_LABELS.get(key, key.replace("_", " ").title() or "—")


def _initials(name: Any) -> str:
    parts = [p for p in str(name or "").replace(".", " ").split() if p]
    if not parts:
        return "—"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def status_pill(status: Any) -> str:
    key = str(status or "")
    css, label = _STATUS_LABELS.get(key, ("pill-mut", key.replace("_", " ").title() or "Unknown"))
    return f'<span class="pill {css}">{esc(label)}</span>'


def account_status_label(account: Dict[str, Any]) -> str:
    """Product wording for an account's status (contract §10 concept screen)."""
    raw = str(account.get("status") or "open").strip()
    low = raw.lower()
    if "disrupt" in low or "bank" in low:
        return "Banking access disrupted"
    if "lock" in low or "council" in low or "await" in low:
        return "Locked · awaiting Council"
    if str(account.get("kind") or "") == "drawdown" and low not in ("open", ""):
        return "Locked · awaiting Council"
    if low in ("open", "active", ""):
        return "Open"
    return raw.replace("_", " ").capitalize()


def account_status_pill(account: Dict[str, Any]) -> str:
    label = account_status_label(account)
    low = label.lower()
    if "disrupt" in low:
        css = "pill-red"
    elif "lock" in low:
        css = "pill-amber"
    elif "open" in low:
        css = "pill-ok"
    else:
        css = "pill-mut"
    return f'<span class="pill {css}">{esc(label)}</span>'


def signature_policy(account: Optional[Dict[str, Any]]) -> str:
    policy = str((account or {}).get("signature_policy") or "dual").lower()
    return policy if policy in _SIGNING_ROLES else "dual"


def policy_label(policy: str) -> str:
    return "Single signature" if policy == "single" else "Dual signature (2-of-2)"


def signing_roles(policy: str) -> set:
    return set(_SIGNING_ROLES.get(policy, _SIGNING_ROLES["dual"]))


def can(user: Optional[Dict[str, Any]], action: str) -> bool:
    if not user:
        return False
    return str(user.get("role") or "") in _ACTION_ROLES.get(action, set())


def user_can_sign(user: Optional[Dict[str, Any]], request: Dict[str, Any], account: Optional[Dict[str, Any]]) -> bool:
    """UI mirror of the service's quorum + separation-of-duties rules."""
    if not user:
        return False
    role = str(user.get("role") or "")
    if role == "admin":
        return False  # admin reads everything, signs nothing (contract §5)
    if role not in signing_roles(signature_policy(account)):
        return False
    if str(request.get("status") or "") != "pending_signatures":
        return False
    uid = str(user.get("id") or "")
    if uid and uid in (str(request.get("prepared_by") or ""), str(request.get("processed_by") or "")):
        return False
    decisions = {str(s.get("user_id") or ""): str(s.get("decision") or "") for s in request.get("signatures") or []}
    return decisions.get(uid) not in ("approve", "decline")


# ------------------------------------------------------------------- shell --
def render_shell(
    *,
    title: str,
    user: Optional[Dict[str, Any]],
    csrf: str,
    nav_active: str = "",
    body: str,
    nonce: str,
    active: Optional[str] = None,
) -> str:
    """Render the one full HTML document every treasury page is wrapped in.

    ``body`` is trusted markup produced by this module or by ``pages_pool``;
    ``title``, the user and the org name are escaped here.

    ``active`` is a compatibility alias for ``nav_active`` (Lane 4's
    ``pages_pool`` calls the shell with ``active=``); the pinned keyword
    ``nav_active`` is what every caller in this lane uses.
    """
    nav_active = active or nav_active
    nav_rows: List[str] = []
    for key, href, label in NAV_ITEMS:
        is_active = key == nav_active
        cls = ' class="nav-on"' if is_active else ""
        current = ' aria-current="page"' if is_active else ""
        nav_rows.append(f'      <a href="{href}"{cls}{current}>{esc(label)}</a>')

    if user:
        chip = (
            '<div class="user">'
            '<div class="chip"><b>' + esc(user.get("name") or user.get("email") or "Signed in") + "</b>"
            "<span>" + esc(_role_label(user.get("role"))) + "</span></div>"
            '<span class="avatar" aria-hidden="true">' + esc(_initials(user.get("name") or user.get("email"))) + "</span>"
            '<form method="post" action="/treasury/logout" class="inline">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            '<button type="submit" class="btn btn-sm btn-ghost">Sign out</button>'
            "</form></div>"
        )
    else:
        chip = '<a class="btn btn-sm btn-ghost" href="/treasury/login">Sign in</a>'

    mode = '<span class="mode">Demo</span>' if settings.DEMO_MODE else ""

    banner = ""
    if settings.DEMO_MODE:
        banner = (
            '<div class="banner" role="status">'
            '<span class="banner-strong">Demo mode</span>'
            '<span class="banner-body">Sample organisation, sample balances. '
            "Nothing here touches a live wallet or a federal award.</span></div>"
        )

    return _fill(
        _template("base"),
        {
            "title": esc(title),
            "org": esc(settings.ORG_NAME),
            "mode": mode,
            "user": chip,
            "nav": "\n".join(nav_rows),
            "banner": banner,
            "body": body,
            "csrf": esc(csrf),
            "nonce": esc(nonce),
        },
    )


def render_unavailable(*, title: str, user, csrf: str, nonce: str, nav_active: str, message: str = "") -> str:
    """Plain card used when a sibling lane's page module is not importable."""
    detail = message or "This screen belongs to another part of the treasury build. The rest of the portal keeps working."
    body = (
        '<div class="card"><h1 class="ptitle">Page not available yet</h1>'
        f'<p class="sub">{esc(detail)}</p>'
        '<p class="note" style="margin-top:12px"><a href="/treasury/accounts">Back to accounts</a></p></div>'
    )
    return render_shell(title=title, user=user, csrf=csrf, nav_active=nav_active, body=body, nonce=nonce)


def render_error_page(*, title: str, user, csrf: str, nonce: str, nav_active: str, message: str) -> str:
    body = (
        '<div class="card"><h1 class="ptitle">' + esc(title) + "</h1>"
        f'<p class="sub">{esc(message)}</p>'
        '<p class="note" style="margin-top:12px"><a href="/treasury/accounts">Back to accounts</a></p></div>'
    )
    return render_shell(title=title, user=user, csrf=csrf, nav_active=nav_active, body=body, nonce=nonce)


# --------------------------------------------------------------- accounts --
def _account_row(acc: Dict[str, Any], *, editable: bool = False) -> str:
    kind = str(acc.get("kind") or "")
    name = acc.get("name") or kind or "Account"
    kind_label = _KIND_LABELS.get(kind, kind.replace("_", " ").title() or "Account")
    code = acc.get("code") or ""
    cap = _money(acc.get("cap_per_request_usd"))
    cap_day = _money(acc.get("cap_daily_usd"))
    policy = signature_policy(acc)
    addr = str(acc.get("maza_address") or "")
    addr_html = f'<div class="mono faint">{esc(addr)}</div>' if addr else ""
    link = "/treasury/requests?account_id=" + esc(acc.get("id") or "")
    edit_html = ""
    if editable:
        edit_html = (
            '<div><a class="dim" href="/treasury/accounts/%s/edit">Edit settings</a></div>'
            % esc(acc.get("id") or "")
        )
    return (
        "<tr>"
        f'<td><a class="lead" href="{link}">{esc(name)}</a>'
        f'<div class="dim">{esc(kind_label)}</div>{addr_html}{edit_html}</td>'
        f'<td class="mono">{esc(code)}</td>'
        f"<td>{account_status_pill(acc)}</td>"
        f'<td class="n">{cap}</td>'
        f'<td class="n">{cap_day}</td>'
        f'<td class="dim">{esc("Single" if policy == "single" else "Dual")}</td>'
        "</tr>"
    )


def _short_id(value: Any) -> str:
    """A user id that has no name in the payload is shown truncated, not raw."""
    text = str(value or "")
    if not text:
        return "system"
    return text[:12] + "…" if len(text) > 12 else text


def _audit_detail(raw: Any) -> str:
    """Audit detail is a JSON object string; render it as compact key=value."""
    if raw in (None, ""):
        return ""
    if isinstance(raw, dict):
        payload: Any = raw
    else:
        try:
            payload = json.loads(str(raw))
        except (ValueError, TypeError):
            return str(raw)
    if isinstance(payload, dict):
        bits = []
        for key, value in list(payload.items())[:6]:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"), default=str)
            bits.append(f"{key}={value}")
        return " · ".join(bits)
    return str(payload)


def _audit_rows(rows: Iterable[Dict[str, Any]], limit: int = 8,
                people: Optional[Dict[str, str]] = None) -> str:
    people = people or {}
    out: List[str] = []
    for row in list(rows)[:limit]:
        action = str(row.get("action") or "")
        detail = _audit_detail(row.get("detail"))
        object_type = str(row.get("object_type") or "")
        object_id = str(row.get("object_id") or "")
        actor_id = str(row.get("actor_user_id") or "")
        actor = str(row.get("actor_name") or people.get(actor_id) or _short_id(actor_id))
        out.append(
            '<div class="auditrow">'
            f'<span class="at">{esc(_dt(row.get("ts")))}</span>'
            f'<span><span class="ak">{esc(action)}</span> '
            f'<span class="av">{esc(object_type)} {esc(object_id)}</span>'
            + (f'<div class="av">{esc(detail)}</div>' if detail else "")
            + f'<div class="ah">by {esc(actor)}</div></span>'
            f'<span class="ah mono">{esc(str(row.get("hash") or "")[:10])}</span>'
            "</div>"
        )
    return "".join(out) if out else '<p class="empty">No activity recorded yet.</p>'


def _needs_signature_rows(rows: Iterable[Dict[str, Any]]) -> str:
    out: List[str] = []
    for req in rows:
        ref = str(req.get("ref") or req.get("id") or "")
        out.append(
            '<div class="sigrow">'
            f'<div><a class="who" href="/treasury/requests/{esc(req.get("id") or "")}">{esc(ref)}</a>'
            f'<div class="role">{esc(req.get("purpose") or "Request")}</div></div>'
            f'<div class="meta">{_money(req.get("usd_total"))}<br>'
            f'<span class="faint">{esc(_dt(req.get("prepared_ts")))}</span></div>'
            "</div>"
        )
    return "".join(out) if out else (
        '<p class="empty">Nothing is waiting on you. Signing duty follows the account policy '
        "and separation of duties.</p>"
    )


def _pool_block(pool: Optional[Dict[str, Any]]) -> str:
    pool = pool or {}
    cats = pool.get("categories") or []
    bars: List[str] = []
    try:
        total = sum(Decimal(str(c.get("usd") or "0")) for c in cats) or Decimal("0")
    except (InvalidOperation, ValueError):
        total = Decimal("0")
    for cat in cats[:5]:
        name = str(cat.get("category") or "other")
        try:
            usd = Decimal(str(cat.get("usd") or "0"))
            pct = int((usd / total * 100).to_integral_value()) if total > 0 else 0
        except (InvalidOperation, ValueError, ZeroDivisionError):
            usd, pct = Decimal("0"), 0
        pct = max(0, min(100, pct))
        bars.append(
            '<div class="cat">'
            f'<span class="cname">{esc(name.capitalize())}</span>'
            f'<span class="bar"><span style="width:{pct}%"></span></span>'
            f'<span class="mono">{_money(usd)}</span>'
            "</div>"
        )
    body = "".join(bars) if bars else '<p class="empty">No donations categorised yet.</p>'
    return (
        '<div class="stack">'
        f'<div><div class="k">Balance</div><div class="v" style="font-size:18px;font-weight:650">'
        f'{_money(pool.get("balance_usd"))}</div>'
        f'<div class="faint mono">{_money(pool.get("balance_maza"), symbol="", suffix="MAZA")}</div></div>'
        f'<div class="cats">{body}</div>'
        "</div>"
    )


def _kpi(label: str, value: str, detail: str = "") -> str:
    return (
        '<div class="kpi">'
        f'<div class="k">{esc(label)}</div><div class="v">{value}</div>'
        + (f'<div class="d">{esc(detail)}</div>' if detail else "")
        + "</div>"
    )


def render_accounts_page(
    *, user: Optional[Dict[str, Any]], csrf: str, nonce: str, data: Dict[str, Any]
) -> str:
    accounts: List[Dict[str, Any]] = list(data.get("accounts") or [])
    needs: List[Dict[str, Any]] = list(data.get("needs_signature") or [])
    activity: List[Dict[str, Any]] = list(data.get("activity") or [])
    pool = data.get("pool") or {}

    committed = Decimal("0")
    open_accounts = 0
    for acc in accounts:
        if account_status_label(acc) == "Open":
            open_accounts += 1
    for row in data.get("month_requests") or []:
        committed += _decimal_or_zero(row.get("usd_total"))

    kpis = "".join(
        [
            _kpi("Committed this month", _money(committed), "requests prepared in the UTC month"),
            _kpi("Awaiting your signature", str(len(needs)), "role-qualified, not yet signed"),
            _kpi("Pool balance", _money(pool.get("balance_usd")), "donor-funded, humanitarian only"),
            _kpi("Open accounts", f"{open_accounts} of {len(accounts)}", "ready to move money"),
        ]
    )

    admin = str((user or {}).get("role") or "") == "admin"
    rows = "".join(_account_row(acc, editable=admin) for acc in accounts) if accounts else (
        '<tr><td colspan="6"><p class="empty">No accounts yet. The seed script creates the demo org; '
        "an admin can add accounts out of band.</p></td></tr>"
    )

    actions = (
        '<a class="btn btn-ghost" href="/treasury/statements">Statements</a>'
        '<a class="btn" href="/treasury/pool">Humanitarian pool</a>'
    )

    body = _fill(
        _template("accounts"),
        {
            "actions": actions,
            "banners": "",
            "kpis": kpis,
            "as_of": "as of " + _dt(time.time()) + " UTC",
            "account_rows": rows,
            "activity": _audit_rows(activity),
            "sig_count": str(len(needs)),
            "sig_rows": _needs_signature_rows(needs),
            "pool": _pool_block(pool),
        },
    )
    return render_shell(
        title="Accounts · MazaPay Treasury",
        user=user,
        csrf=csrf,
        nav_active="accounts",
        body=body,
        nonce=nonce,
    )


# --------------------------------------------------------------- requests --
def _request_row(req: Dict[str, Any]) -> str:
    rid = str(req.get("id") or "")
    ref = str(req.get("ref") or rid)
    return (
        "<tr>"
        f'<td><a class="lead mono" href="/treasury/requests/{esc(rid)}">{esc(ref)}</a>'
        f'<div class="dim">{esc(req.get("account_code") or req.get("account_name") or "")}</div></td>'
        f'<td>{esc(req.get("purpose") or "")}</td>'
        f'<td>{status_pill(req.get("status"))}</td>'
        f'<td class="n">{_money(req.get("usd_total"))}</td>'
        f'<td class="n faint">{_money(req.get("maza_total"), symbol="")}</td>'
        f'<td class="dim nowrap">{esc(_day(req.get("prepared_ts")))}</td>'
        "</tr>"
    )


def render_requests_page(*, user, csrf: str, nonce: str, data: Dict[str, Any]) -> str:
    rows: List[Dict[str, Any]] = list(data.get("requests") or [])
    total = sum((Decimal(str(r.get("usd_total") or "0")) for r in rows), Decimal("0")) if rows else Decimal("0")
    body_table = "".join(_request_row(r) for r in rows) if rows else (
        '<tr><td colspan="6"><p class="empty">No requests match this filter.</p></td></tr>'
    )
    body = (
        '<div class="head"><div class="grow"><h1 class="ptitle">Requests</h1>'
        '<p class="sub">Drawdowns, payouts and stipend runs. Every one carries its signatures and audit trail.</p></div>'
        '<div class="actions"><a class="btn btn-ghost" href="/treasury/accounts">Accounts</a></div></div>'
        '<section class="kpis">'
        + _kpi("Requests shown", str(len(rows)), "newest first")
        + _kpi("Value shown", _money(total), "USD total")
        + "</section>"
        '<section class="card"><h2 class="card-h">All requests</h2>'
        '<table class="tbl"><thead><tr><th>Ref</th><th>Purpose</th><th>Status</th>'
        '<th class="n">USD</th><th class="n">MAZA</th><th>Prepared</th></tr></thead>'
        f"<tbody>{body_table}</tbody></table></section>"
    )
    return render_shell(
        title="Requests · MazaPay Treasury",
        user=user,
        csrf=csrf,
        nav_active="requests",
        body=body,
        nonce=nonce,
    )


def _chain(request: Dict[str, Any]) -> str:
    """Four-step rail with the exact §6 mapping."""
    status = str(request.get("status") or "prepared")
    steps = [
        ("Prepared", "prepared_ts", "prepared_by", "Written by the officer"),
        ("Processed", "processed_ts", "processed_by", "Quote locked, items priced"),
        ("Authorized (2-of-2)", "authorized_ts", None, "Signature quorum reached"),
        ("Completed", "completed_ts", None, "Broadcast and recorded"),
    ]
    reached = {
        "prepared": 1,
        "processed": 2,
        "pending_signatures": 2,
        "authorized": 3,
        "completed": 4,
        "completed_partial": 4,
        "cancelled": 0,
        "declined": 2,
    }.get(status, 1)
    terminal_bad = status in ("cancelled", "declined")
    out: List[str] = []
    for index, (label, ts_key, _actor_key, sub) in enumerate(steps, start=1):
        if index <= reached:
            css = "step step-done"
        elif index == reached + 1 and not terminal_bad:
            css = "step step-now"
        else:
            css = "step step-lock"
        stamp = _dt(request.get(ts_key))
        when = f'<div class="st">{esc(stamp)}</div>' if request.get(ts_key) else ""
        out.append(
            f'<div class="{css}"><div class="sn">{index}</div>'
            f'<div class="sl">{esc(label)}</div><div class="sd">{esc(sub)}</div>{when}</div>'
        )
    return "".join(out)


def _action_form(request_id: str, csrf: str, action: str, label: str, *, css: str = "btn", decision: str = "") -> str:
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    if decision:
        hidden += f'<input type="hidden" name="decision" value="{esc(decision)}">'
    if decision == "decline":
        # A decline needs a reason (contract §6); works without JS.
        hidden += (
            '<input type="text" name="note" required maxlength="200" placeholder="Reason for declining"'
            ' aria-label="Reason for declining" style="width:190px">'
        )
    return (
        f'<form method="post" action="/treasury/requests/{esc(request_id)}/{action}" class="inline js-form" '
        f'data-action="{esc(action or decision)}">'
        f"{hidden}"
        f'<button class="{css}" type="submit">{esc(label)}</button>'
        "</form>"
    )


def _request_actions(
    *, user, csrf: str, request: Dict[str, Any], account: Optional[Dict[str, Any]]
) -> str:
    rid = str(request.get("id") or "")
    status = str(request.get("status") or "")
    buttons: List[str] = []
    if status == "prepared" and can(user, "process"):
        buttons.append(_action_form(rid, csrf, "process", "Process · lock quote", css="btn btn-primary"))
    if status == "processed" and can(user, "submit"):
        buttons.append(_action_form(rid, csrf, "submit", "Submit for signature", css="btn btn-primary"))
    if user_can_sign(user, request, account):
        buttons.append(_action_form(rid, csrf, "sign", "Approve", css="btn btn-primary", decision="approve"))
        buttons.append(_action_form(rid, csrf, "sign", "Decline", css="btn btn-danger", decision="decline"))
    if status == "authorized" and can(user, "complete"):
        buttons.append(_action_form(rid, csrf, "complete", "Complete · broadcast", css="btn btn-primary"))
    if status in ("prepared", "processed", "pending_signatures") and can(user, "cancel"):
        buttons.append(_action_form(rid, csrf, "cancel", "Cancel", css="btn btn-ghost"))
    if not buttons:
        buttons.append('<span class="faint">No action available to you at this stage.</span>')
    return "".join(buttons)


def _signature_rows(signatures: Sequence[Dict[str, Any]], people: Optional[Dict[str, str]] = None) -> str:
    people = people or {}
    out: List[str] = []
    for sig in signatures:
        decision = str(sig.get("decision") or "")
        css = "pill-ok" if decision == "approve" else "pill-red"
        signer_id = str(sig.get("user_id") or "")
        who = sig.get("user_name") or sig.get("name") or people.get(signer_id) or _short_id(signer_id)
        out.append(
            '<div class="sigrow">'
            f'<div><span class="who">{esc(who)}</span>'
            f'<div class="role">{esc(_role_label(sig.get("role_at_signing")))}</div>'
            + (f'<div class="role">{esc(sig.get("note"))}</div>' if sig.get("note") else "")
            + "</div>"
            f'<span class="pill {css}">{esc(decision.capitalize() or "Recorded")}</span>'
            f'<div class="meta">{esc(_dt(sig.get("signed_ts")))}</div>'
            "</div>"
        )
    return "".join(out) if out else '<p class="empty">No signatures yet.</p>'


def _item_rows(items: Sequence[Dict[str, Any]], csrf: str) -> str:
    out: List[str] = []
    for item in items:
        status = str(item.get("status") or "pending")
        css = {"sent": "pill-ok", "failed": "pill-red"}.get(status, "pill-mut")
        txid = str(item.get("txid") or "")
        slip = ""
        if status == "sent" and item.get("id"):
            slip = f' <a class="mono faint" href="/treasury/payslips/{esc(item.get("id"))}">payslip</a>'
        out.append(
            "<tr>"
            f'<td><span class="lead">{esc(item.get("payee_name") or "Payee")}</span>{slip}'
            f'<div class="dim mono">{esc(item.get("payee_address") or "")}</div></td>'
            f'<td class="dim">{esc(item.get("category") or "—")}</td>'
            f'<td class="n">{_money(item.get("usd_amount"))}</td>'
            f'<td class="n">{_money(item.get("maza_amount"), symbol="")}</td>'
            f'<td><span class="pill {css}">{esc(status.capitalize())}</span></td>'
            f'<td class="mono faint">{esc(txid[:18] + "…" if len(txid) > 18 else txid or "—")}</td>'
            "</tr>"
        )
        if status == "failed" and item.get("error"):
            out.append(
                f'<tr><td colspan="6" class="formmsg">Last attempt failed: {esc(item.get("error"))}</td></tr>'
            )
    return "".join(out) if out else '<tr><td colspan="6"><p class="empty">No items on this request.</p></td></tr>'


def render_request_page(*, user, csrf: str, nonce: str, data: Dict[str, Any]) -> str:
    request: Dict[str, Any] = dict(data.get("request") or {})
    items: List[Dict[str, Any]] = list(data.get("items") or request.get("items") or [])
    signatures: List[Dict[str, Any]] = list(data.get("signatures") or request.get("signatures") or [])
    audit: List[Dict[str, Any]] = list(data.get("audit") or request.get("audit") or [])
    account: Optional[Dict[str, Any]] = data.get("account") or (
        {
            "kind": request.get("account_kind"),
            "signature_policy": request.get("signature_policy"),
        }
        if request.get("account_kind") or request.get("signature_policy")
        else None
    )
    request_with_sigs = dict(request, signatures=signatures)

    policy = signature_policy(account)
    status = str(request.get("status") or "")
    kind = str(request.get("kind") or "")
    kind_label = kind.replace("_", " ").title() or "Request"
    account = account or {}
    account_name = request.get("account_name") or account.get("name") or "—"
    account_code = request.get("account_code") or account.get("code") or "—"
    people: Dict[str, str] = dict(data.get("people") or {})
    prepared_by = (
        request.get("prepared_by_name")
        or people.get(str(request.get("prepared_by") or ""))
        or _short_id(request.get("prepared_by"))
    )
    federal = str(account.get("kind") or request.get("account_kind") or "") == "federal_oversight"

    banners: List[str] = []
    if status == "completed_partial":
        banners.append(
            '<div class="banner banner-red"><span class="banner-strong">Completed · partial</span>'
            "<span class=\"banner-body\">At least one item failed to broadcast. The request stays retryable; "
            "already-sent items are never broadcast twice.</span></div>"
        )
    if status == "declined":
        banners.append(
            '<div class="banner banner-red"><span class="banner-strong">Declined</span>'
            f'<span class="banner-body">{esc(request.get("decline_reason") or "A signer declined this request.")}</span></div>'
        )
    if status == "cancelled":
        banners.append(
            '<div class="banner"><span class="banner-strong">Cancelled</span>'
            '<span class="banner-body">No money moved for this request.</span></div>'
        )
    if federal:
        banners.append(
            '<div class="banner"><span class="banner-strong">Federal award funds</span>'
            '<span class="banner-body">Oversight trail only — this request documents the award, '
            "nothing is broadcast for this account.</span></div>"
        )
    flash = data.get("flash")
    if flash:
        kind, message = flash
        css = "banner banner-ok" if kind == "ok" else "banner banner-red"
        banners.insert(
            0,
            f'<div class="{css}" role="status"><span class="banner-strong">'
            + ("Done" if kind == "ok" else "Refused")
            + f'</span><span class="banner-body">{esc(message)}</span></div>',
        )

    rate_line = (
        f'{_rate_text(request.get("rate"))} USD / MAZA'
        if request.get("rate")
        else "no rate locked yet"
    )
    rate_source = str(request.get("rate_source") or "rate not locked")
    if request.get("rate_expires_ts") and request.get("rate_locked_ts"):
        rate_source += " · locked " + _dt(request.get("rate_locked_ts"))
        if str(request.get("status") or "") in ("processed", "pending_signatures"):
            rate_source += ", expires " + _dt(request.get("rate_expires_ts"))

    scope_note = (
        "federal award funds: oversight trail only. Prepare, sign and authorise freely; "
        "completion is refused because no funds move from this account."
        if federal
        else "The organisation's own MAZA. No third-party custody, no pooled client money."
    )

    body = _fill(
        _template("request"),
        {
            "ref": esc(request.get("ref") or request.get("id") or "Request"),
            "purpose": esc(request.get("purpose") or "Untitled request"),
            "kind_label": esc(kind_label),
            "account_name": esc(account_name),
            "account_code": esc(account_code),
            "prepared_by": esc(prepared_by),
            "prepared_ts": esc(_dt(request.get("prepared_ts"))),
            "actions": _request_actions(user=user, csrf=csrf, request=request_with_sigs, account=account),
            "banners": "".join(banners),
            "rate_line": esc(rate_line),
            "usd_total": _money(request.get("usd_total")),
            "maza_total": _money(request.get("maza_total"), symbol="", suffix="MAZA"),
            "rate_source": esc(rate_source),
            "status_pill": status_pill(status),
            "status_hint": esc(_STATUS_HINTS.get(status, "")),
            "policy_line": esc(policy_label(policy)),
            "chain": _chain(request),
            "item_count": str(len(items)),
            "item_rows": _item_rows(items, csrf),
            "audit": _audit_rows(audit, limit=12, people=people),
            "sig_count": str(len(signatures)),
            "sig_rows": _signature_rows(signatures, people),
            "scope_note": esc(scope_note),
        },
    )
    return render_shell(
        title=f'{request.get("ref") or "Request"} · MazaPay Treasury',
        user=user,
        csrf=csrf,
        nav_active="requests",
        body=body,
        nonce=nonce,
    )


# ------------------------------------------------------------------ users --
def render_users_page(
    *, user, csrf: str, nonce: str, data: Dict[str, Any], error: Optional[str] = None
) -> str:
    users: List[Dict[str, Any]] = list(data.get("users") or [])
    rows: List[str] = []
    for row in users:
        active = bool(row.get("active", 1))
        css = "pill-ok" if active else "pill-mut"
        label = "Active" if active else "Deactivated"
        action = "—"
        if active and str(row.get("id")) != str((user or {}).get("id")):
            action = (
                f'<form method="post" action="/treasury/users/{esc(row.get("id"))}/deactivate" class="inline">'
                f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                '<button class="btn btn-sm btn-ghost" type="submit">Deactivate</button></form>'
            )
        rows.append(
            "<tr>"
            f'<td class="lead">{esc(row.get("name") or "—")}</td>'
            f'<td class="mono">{esc(row.get("email") or "—")}</td>'
            f'<td>{esc(_role_label(row.get("role")))}</td>'
            f'<td><span class="pill {css}">{esc(label)}</span></td>'
            f'<td class="right">{action}</td>'
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5"><p class="empty">No users yet.</p></td></tr>')

    roles = "".join(
        f'<option value="{role}">{esc(_ROLE_LABELS.get(role, role))}</option>' for role in _ROLE_ORDER
    )
    form_error = f'<p class="formmsg" role="alert">{esc(error)}</p>' if error else ""
    body = _fill(
        _template("users"),
        {
            "banners": "",
            "user_count": str(len(users)),
            "user_rows": "".join(rows),
            "csrf": esc(csrf),
            "role_options": roles,
            "form_error": form_error,
        },
    )
    return render_shell(
        title="Users · MazaPay Treasury",
        user=user,
        csrf=csrf,
        nav_active="users",
        body=body,
        nonce=nonce,
    )


# ---------------------------------------------------------------- payslip --
def render_payslip_page(*, user, csrf: str, nonce: str, data: Dict[str, Any]) -> str:
    """Printable receipt for one paid item (contract §11)."""
    request: Dict[str, Any] = dict(data.get("request") or {})
    item: Dict[str, Any] = dict(data.get("item") or data.get("payslip") or {})
    rate = request.get("rate") or item.get("rate")
    source = request.get("rate_source") or item.get("rate_source") or "—"
    txid = str(item.get("txid") or "not broadcast")
    audit = list(data.get("audit") or [])
    recon = _audit_rows(audit, limit=6, people=data.get("people")) if audit else (
        '<p class="empty">No audit rows linked to this item.</p>'
    )

    rows = [
        ("Request", request.get("ref") or "—"),
        ("Recipient", item.get("payee_name") or "—"),
        ("Category", item.get("category") or "—"),
        ("USD amount", _money(item.get("usd_amount"))),
        ("MAZA amount", _money(item.get("maza_amount"), symbol="", suffix="MAZA")),
        ("Rate", _rate_text(rate) + " USD / MAZA"),
        ("Rate source", source),
        ("Transaction", txid),
        ("Date", item.get("date") or _dt(item.get("sent_ts") or item.get("created_ts"))),
    ]
    dl = "".join(f"<dt>{esc(k)}</dt><dd>{v if k in ('USD amount', 'MAZA amount', 'Rate') else esc(v)}</dd>" for k, v in rows)
    body = (
        '<div class="card payslip">'
        '<p class="sub"><a href="/treasury/requests/' + esc(request.get("id") or "") + '">Back to request</a></p>'
        '<h1 class="ptitle">Payslip</h1>'
        f'<p class="sub">{esc(request.get("ref") or "")} · {esc(request.get("purpose") or "")}</p>'
        f'<dl style="margin-top:14px">{dl}</dl>'
        '<p class="note" style="margin-top:14px">USD value at receipt was fixed by the locked quote at processing time; '
        "the MAZA amount is what left the treasury wallet.</p>"
        '<h2 class="card-h" style="margin-top:18px">Reconciliation</h2>'
        f"{recon}"
        "</div>"
    )
    return render_shell(
        title="Payslip · MazaPay Treasury",
        user=user,
        csrf=csrf,
        nav_active="requests",
        body=body,
        nonce=nonce,
    )


def render_account_edit_page(
    *, user, csrf: str, nonce: str, account, error=None
) -> str:
    """Admin form for an account's name, status, signature policy and caps."""
    acc = account or {}
    status = str(acc.get("status") or "open")
    policy = str(acc.get("signature_policy") or "dual")

    def opt(value: str, label: str, current: str) -> str:
        sel = " selected" if value == current else ""
        return f'<option value="{esc(value)}"{sel}>{esc(label)}</option>'

    body = f"""
<div class="head">
  <div class="grow">
    <h1 class="ptitle">Edit account</h1>
    <p class="sub">{esc(acc.get("name") or "")} · {esc(acc.get("code") or "")} · {esc(acc.get("kind") or "")}</p>
  </div>
  <div class="actions"><a class="btn btn-ghost" href="/treasury/accounts">Back to accounts</a></div>
</div>
{"<div class='banner' role='alert'><span class='banner-strong'>" + esc(error) + "</span></div>" if error else ""}
<section class="card">
  <h2 class="card-h">Account settings</h2>
  <form method="post" action="/treasury/accounts/{esc(acc.get("id") or "")}/edit">
    <input type="hidden" name="csrf" value="{esc(csrf)}">
    <div class="formgrid">
      <label class="field">Name
        <input name="name" value="{esc(acc.get("name") or "")}" maxlength="120" required>
      </label>
      <label class="field">Status
        <select name="status">{opt("open", "Open", status)}{opt("banking_disrupted", "Banking access disrupted", status)}{opt("closed", "Closed", status)}</select>
      </label>
      <label class="field">Signature policy
        <select name="signature_policy">{opt("dual", "Dual (executive + council treasurer)", policy)}{opt("single", "Single (treasurer)", policy)}</select>
      </label>
      <label class="field">Cap per request (USD)
        <input name="cap_per_request_usd" value="{esc(acc.get("cap_per_request_usd") or "")}" inputmode="decimal" placeholder="no cap">
      </label>
      <label class="field">Daily cap (USD)
        <input name="cap_daily_usd" value="{esc(acc.get("cap_daily_usd") or "")}" inputmode="decimal" placeholder="no cap">
      </label>
      <label class="field">Award reference
        <input name="award_ref" value="{esc(acc.get("award_ref") or "")}" maxlength="200" placeholder="e.g. PL 93-638">
      </label>
      <label class="field">Notes
        <input name="notes" value="{esc(acc.get("notes") or "")}" maxlength="500">
      </label>
    </div>
    <p class="note">Every change is written to the audit trail with the before and after value.
      A signature-policy change or closing the account is refused while requests are still in
      flight on it.</p>
    <div class="actions"><button class="btn btn-primary" type="submit">Save changes</button></div>
  </form>
</section>
"""
    return render_shell(
        title=f"Edit {acc.get('code') or 'account'} · MazaPay Treasury",
        user=user, csrf=csrf, nav_active="accounts", body=body, nonce=nonce,
    )
