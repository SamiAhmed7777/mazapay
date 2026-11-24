"""Treasury secondary screens: pool, statements, audit, login (LANE 4).

Every public function returns a COMPLETE HTML document:

    render_login(*, csrf, nonce, sent=False, error=None, demo_link=None)
    render_pool(*, user, csrf, nonce, data)
    render_statements(*, user, csrf, nonce, data)
    render_audit(*, user, csrf, nonce, data)

Layout: the page body lives in ``mazapay/templates/treasury/<name>.html`` with
``{{PLACEHOLDER}}`` slots that are filled from pre-escaped fragments built here;
the body is then wrapped by ``mazapay.treasury.layout.render_shell``.  If that
sibling module (LANE 3) is absent — or its signature does not accept our
keyword arguments — a local shell carrying the same design tokens is used so
these screens never depend on a lane that has not landed yet.

Security rules that hold on both paths: every interpolation goes through
:func:`_esc`; the only ``<script>`` is ``/static/treasury.js`` loaded with the
CSP nonce (no inline script bodies, no inline handlers); the only inline
``<style>`` is a nonce-carrying block of static rules (CSP ``style-src`` allows
the nonce); no absolute/external origin survives into the document.
"""
from __future__ import annotations

import datetime as _dt
import html
import inspect
import os
import re
from decimal import Decimal, InvalidOperation

from . import settings

__all__ = ["render_login", "render_pool", "render_statements", "render_audit"]

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "treasury"
)
_ASSET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
_CENT = Decimal("0.01")
_MAZA_STEP = Decimal("0.00000001")

PENDING_STATUSES = ("prepared", "processed", "pending_signatures", "authorized")

# Section 5 categories, in the order the board shows them.
POOL_CATEGORIES = (
    ("health", "Emergency health services"),
    ("family", "Emergency family support"),
    ("elder", "Elders & vulnerable members"),
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# Footnote copy reused by both shells.
_LOGIN_FINE = (
    "MazaPay Treasury · organisation funds only. Federal award accounts are "
    "documentary: they can be prepared, signed and authorised, but nothing is "
    "ever broadcast for them."
)


# --------------------------------------------------------------------------- #
# escaping + formatting
# --------------------------------------------------------------------------- #
def _esc(value) -> str:
    """Escape for both text and quoted-attribute contexts."""
    return html.escape("" if value is None else str(value), quote=True)


def _txt(value, default: str = "—") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return _esc(text) if text else default


def _dec(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _fmt_usd(value, default: str = "—") -> str:
    d = _dec(value)
    if d is None:
        return default
    return "$" + format(d, ",.2f")


def _fmt_maza(value, default: str = "—") -> str:
    """MAZA keeps up to 8 decimals, at least 2, never scientific notation."""
    d = _dec(value)
    if d is None:
        return default
    text = format(d.quantize(_MAZA_STEP), ",.8f").rstrip("0")
    whole, _, frac = text.partition(".")
    if not whole:
        whole = "0"
    return whole + "." + (frac + "00")[:2]


def _fmt_rate(value, default: str = "—") -> str:
    d = _dec(value)
    if d is None:
        return default
    return format(d, ".6f")


def _fmt_ts(value, default: str = "—") -> str:
    d = _dec(value)
    if d is None:
        text = _txt(value, "")
        return text or default
    try:
        moment = _dt.datetime.fromtimestamp(float(d), _dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return default
    return moment.strftime("%Y-%m-%d %H:%M")


def _fmt_date(value, default: str = "—") -> str:
    d = _dec(value)
    if d is None:
        text = _txt(value, "")
        return text or default
    try:
        moment = _dt.datetime.fromtimestamp(float(d), _dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return default
    return moment.strftime("%Y-%m-%d")


def _month_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")


def _month_label(month: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})", month or "")
    if not match:
        return _esc(month or "")
    year, mm = int(match.group(1)), int(match.group(2))
    if not 1 <= mm <= 12:
        return _esc(month)
    return f"{_MONTHS[mm - 1]} {year}"


def _short_hash(value) -> str:
    text = "" if value is None else str(value)
    if len(text) <= 16:
        return _esc(text) if text else "—"
    return _esc(text[:12] + "…" + text[-4:])


_PILLS = {
    "completed": ("pill-ok", "completed"),
    "completed_partial": ("pill-amber", "partial"),
    "authorized": ("pill-amber", "authorized"),
    "pending_signatures": ("pill-amber", "awaiting signatures"),
    "processed": ("pill-amber", "processed"),
    "prepared": ("pill-mut", "prepared"),
    "sent": ("pill-ok", "sent"),
    "pending": ("pill-mut", "pending"),
    "failed": ("pill-red", "failed"),
    "declined": ("pill-red", "declined"),
    "cancelled": ("pill-red", "cancelled"),
    "expired": ("pill-mut", "expired"),
}


def _pill(status) -> str:
    key = "" if status is None else str(status).strip()
    cls, label = _PILLS.get(key, ("pill-mut", key or "—"))
    return f'<span class="pill {cls}">{_esc(label)}</span>'


# --------------------------------------------------------------------------- #
# template plumbing
# --------------------------------------------------------------------------- #
_TEMPLATE_CACHE: dict[str, str] = {}


def _template(name: str) -> str:
    if name not in _TEMPLATE_CACHE:
        path = os.path.join(_TEMPLATE_DIR, f"{name}.html")
        with open(path, "r", encoding="utf-8") as fh:
            _TEMPLATE_CACHE[name] = fh.read()
    return _TEMPLATE_CACHE[name]


def _fill(template: str, mapping: dict) -> str:
    """Substitute ``{{NAME}}`` slots; values must already be escaped HTML."""
    missing: list[str] = []

    def _sub(match):
        key = match.group(1)
        if key not in mapping:
            missing.append(key)
            return match.group(0)
        return mapping[key]

    out = _PLACEHOLDER.sub(_sub, template)
    if missing:
        raise ValueError("unfilled template slots: " + ", ".join(sorted(set(missing))))
    return out


# --------------------------------------------------------------------------- #
# local fallback shell (used when LANE 3's layout.py / treasury.css are absent)
# --------------------------------------------------------------------------- #
_TOKENS = (
    ("--bg", "#0b0d12"),
    ("--surface", "#151b24"),
    ("--surface-2", "#0d1117"),
    ("--line", "#242c39"),
    ("--line-strong", "#5f6d8c"),
    ("--text", "#eef1f6"),
    ("--muted", "#9aa4b6"),
    ("--amber", "#f0a000"),
    ("--amber-soft", "#ffc45c"),
    ("--ink", "#101318"),
    ("--red", "#f00000"),
    ("--red-text", "#ff7d7d"),
    ("--radius", "18px"),
    ("--ok", "#4ecb8a"),
    ("--ok-tint", "#0f2419"),
    ("--amber-tint", "#241b08"),
    ("--red-tint", "#2a1212"),
    ("--faint", "#7b8698"),
)

_FALLBACK_CSS = """
:root {
__TOKENS__
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:15px;line-height:1.5}
a{color:var(--amber-soft)}
.shell{max-width:1120px;margin:0 auto;padding:16px 18px 44px}
.topbar{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;padding:8px 0 13px;border-bottom:1px solid var(--line)}
.brand{display:inline-flex;align-items:center;gap:8px;color:var(--text);text-decoration:none;font-weight:700;letter-spacing:.01em}
.brand img{border-radius:50%;display:block}
.brand .faint{font-weight:600}
.org,.mode{font-size:.78rem;color:var(--muted)}
.topbar form{margin:0}
.user{margin-left:auto;font-size:.8rem;color:var(--muted);display:flex;align-items:center;gap:10px}
.nav{display:flex;flex-wrap:wrap;gap:6px;padding:10px 0 0}
.nav a{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border-radius:999px;border:1px solid transparent;color:var(--muted);text-decoration:none;font-size:.82rem;font-weight:600}
.nav a:hover{color:var(--text);border-color:var(--line-strong)}
.nav .nav-on{color:var(--ink);background:var(--amber);border-color:var(--amber)}
.badge{display:inline-block;min-width:18px;padding:0 5px;border-radius:999px;background:var(--surface-2);border:1px solid var(--line-strong);color:var(--amber-soft);font-size:.68rem;text-align:center}
.page{display:flex;flex-direction:column;gap:16px;padding:20px 0 0}
.ptitle{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:10px 16px}
.ptitle h1{margin:2px 0 0;font-size:1.45rem;letter-spacing:-.01em}
.eyebrow{margin:0;font-size:.68rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.sub{margin:5px 0 0;color:var(--muted);font-size:.9rem;line-height:1.5}
.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:44px;padding:9px 15px;border-radius:12px;border:1px solid transparent;font:inherit;font-size:.88rem;font-weight:700;cursor:pointer;text-decoration:none}
.btn-primary{background:linear-gradient(180deg,var(--amber-soft),var(--amber));color:var(--ink);border-color:var(--amber)}
.btn-ghost{background:transparent;border-color:var(--line-strong);color:var(--text)}
.btn-ghost:hover{border-color:var(--amber);color:var(--amber-soft)}
.btn-sm{min-height:34px;padding:6px 12px;font-size:.79rem;border-radius:10px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:16px 17px}
.card-h{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:6px 10px;margin:0 0 11px}
.card-h h2{margin:0;font-size:.95rem;letter-spacing:.01em}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.kpi{background:var(--surface-2);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.kpi .k{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.kpi .v{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.kpi .d{font-size:11.5px;color:var(--muted);margin-top:3px}
.cols2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;align-items:start}
.cats{display:flex;flex-direction:column;gap:10px}
.cat{display:grid;grid-template-columns:minmax(120px,150px) minmax(80px,1fr) auto;gap:10px;align-items:center;font-size:.85rem}
.cat .cname{color:var(--muted)}
.bar{position:relative;display:block;height:8px;border-radius:999px;background:var(--surface-2);border:1px solid var(--line);overflow:hidden}
.bar > span{display:block;height:100%;border-radius:999px;background:var(--amber)}
.tbl{width:100%;border-collapse:collapse;font-size:.83rem}
.tbl caption{caption-side:top;text-align:left;padding:0 0 8px}
.tbl th{text-align:left;color:var(--muted);font-weight:600;font-size:.68rem;letter-spacing:.09em;text-transform:uppercase;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
.tbl td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.tbl tbody tr:last-child td{border-bottom:none}
.tbl .n,.tbl th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl tfoot th,.tbl tfoot td{border-top:1px solid var(--line-strong);border-bottom:none;padding-top:10px;font-weight:700}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.78rem}
.pill{display:inline-block;border-radius:999px;padding:1px 8px;font-size:.7rem;font-weight:700;border:1px solid var(--line-strong);color:var(--text);white-space:nowrap}
.pill-ok{background:var(--ok-tint);border-color:var(--ok);color:var(--ok)}
.pill-amber{background:var(--amber-tint);border-color:var(--amber);color:var(--amber-soft)}
.pill-red{background:var(--red-tint);border-color:var(--red-text);color:var(--red-text)}
.pill-mut{background:var(--surface-2);border-color:var(--line-strong);color:var(--muted)}
.banner{margin:0;border:1px solid var(--line-strong);border-radius:12px;padding:10px 13px;font-size:.85rem;background:var(--surface-2);color:var(--text)}
.banner-strong{border-color:var(--amber);background:var(--amber-tint);color:var(--amber-soft);font-weight:600}
.note{margin:0;color:var(--muted);font-size:.8rem;line-height:1.55}
.faint{color:var(--faint);font-size:.76rem}
.err{margin:8px 0 0;color:var(--red-text);font-size:.85rem;min-height:1.2em}
label{display:block;margin:0 0 6px;font-size:.7rem;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);font-weight:600}
input,select{width:100%;min-height:42px;padding:8px 11px;background:var(--surface-2);border:1px solid var(--line-strong);border-radius:10px;color:var(--text);font:inherit;font-size:.9rem}
input:focus,select:focus{outline:2px solid var(--amber);outline-offset:1px}
label.field{display:flex;flex-direction:column;gap:5px;font-size:.72rem;color:var(--muted);margin:0}
.formgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;align-items:end}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.head{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap}
.head .grow{flex:1 1 260px;min-width:0}
.tbl-wrap{overflow-x:auto}
.formmsg{margin:8px 0 0;font-size:.82rem;color:var(--red-text)}
.empty{padding:14px 0;color:var(--faint);font-size:.85rem}
.stack{display:flex;flex-direction:column;gap:10px}
input[type=month]{width:100%;min-height:42px;padding:8px 11px;background:var(--surface-2);border:1px solid var(--line-strong);border-radius:9px;color:var(--text);font:inherit;font-size:.9rem}
.tr-login{max-width:420px;margin:6vh auto 0;width:100%}
.tr-login .card{margin-bottom:12px}
.tr-login .ptitle{margin:12px 0 6px}
.tr-login .sub{margin-bottom:14px}
@media (max-width:640px){.tbl{font-size:.78rem}.tbl th,.tbl td{padding:7px 7px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""


def _local_shell(*, title: str, body: str, user, csrf: str, nonce: str, active: str) -> str:
    """Minimal same-token shell used only when LANE 3's layout is unavailable."""
    tokens = "\n".join(f"  {name}:{value};" for name, value in _TOKENS)
    css = _FALLBACK_CSS.replace("__TOKENS__", tokens)
    nav_items = (
        ("accounts", "Accounts", "/treasury/accounts"),
        ("requests", "Requests", "/treasury/requests"),
        ("pool", "Pool", "/treasury/pool"),
        ("statements", "Statements", "/treasury/statements"),
        ("audit", "Audit", "/treasury/audit"),
    )
    links = []
    for key, label, href in nav_items:
        cls = "nav-on" if key == active else ""
        links.append(f'<a class="{cls}" href="{href}">{_esc(label)}</a>')
    if user and str(user.get("role") or "") == "admin":
        links.append('<a href="/treasury/users">Users</a>')
    if user:
        who = (
            f'<div class="user"><span>{_txt(user.get("name") or user.get("email"))}'
            f' · {_txt(user.get("role"))}</span>'
            '<form method="post" action="/treasury/logout">'
            '<button class="btn btn-ghost btn-sm" type="submit">Sign out</button></form></div>'
        )
    else:
        who = '<span class="user"><a href="/treasury/login">Sign in</a></span>'
    mode = "demo" if settings.DEMO_MODE else "live"
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="dark">\n'
        f'<meta name="theme-color" content="#0b0d12">\n'
        f"<title>{_esc(title)} · MazaPay Treasury</title>\n"
        '<link rel="icon" type="image/png" href="/static/mazacoin-180.png">\n'
        f'<style nonce="{_esc(nonce)}">{css}</style>\n'
        "</head>\n<body>\n"
        '<div class="shell">\n'
        '<header class="topbar">\n'
        '  <a class="brand" href="/treasury/accounts">'
        '<img src="/static/mazacoin-64.png" alt="" width="26" height="26">'
        'MazaPay <span class="faint">Treasury</span></a>\n'
        f'  <span class="org">{_esc(settings.ORG_NAME)}</span>\n'
        f'  <span class="mode">{mode}</span>\n'
        f"  {who}\n"
        "</header>\n"
        '<nav class="nav" aria-label="Treasury">\n  ' + "\n  ".join(links) + "\n</nav>\n"
        f'<main class="page">\n{body}\n</main>\n'
        "</div>\n"
        f'<script nonce="{_esc(nonce)}" src="/static/treasury.js" defer></script>\n'
        "</body>\n</html>\n"
    )


_SHELL_CANDIDATES = {
    "title": "title",
    "page_title": "title",
    "heading": "title",
    "body": "body",
    "content": "body",
    "inner": "body",
    "html": "body",
    "main": "body",
    "body_html": "body",
    "page_html": "body",
    "main_html": "body",
    "content_html": "body",
    "inner_html": "body",
    "user": "user",
    "me": "user",
    "current_user": "user",
    "csrf": "csrf",
    "csrf_token": "csrf",
    "nonce": "nonce",
    "csp_nonce": "nonce",
    "active": "active",
    "active_nav": "active",
    "nav": "active",
    "nav_active": "active",
    "nav_page": "active",
    "current": "active",
    "page": "active",
}


def _via_sibling_shell(*, title, body, user, csrf, nonce, active):
    """Call LANE 3's render_shell when it exists and accepts our keywords."""
    from .layout import render_shell  # noqa: PLC0415 - optional sibling module

    values = {
        "title": title,
        "body": body,
        "user": user,
        "csrf": csrf,
        "nonce": nonce,
        "active": active,
    }
    try:
        params = inspect.signature(render_shell).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return render_shell(title=title, body=body, user=user, csrf=csrf, nonce=nonce, active=active)

    kwargs: dict[str, object] = {}
    missing: list[str] = []
    variadic = any(p.kind is p.VAR_KEYWORD for p in params.values())
    for name, param in params.items():
        if param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
            continue
        if param.kind is param.POSITIONAL_ONLY:
            missing.append(name)
            continue
        key = _SHELL_CANDIDATES.get(name)
        if key is not None:
            kwargs[name] = values[key]
        elif param.default is param.empty:
            missing.append(name)
    if missing and not variadic:
        raise TypeError(f"render_shell needs unsupported parameters: {missing}")
    return render_shell(**kwargs)


def _page_doc(*, title: str, body: str, user, csrf: str, nonce: str, active: str) -> str:
    try:
        document = _via_sibling_shell(
            title=title, body=body, user=user, csrf=csrf, nonce=nonce, active=active
        )
    except Exception:  # noqa: BLE001 - a sibling lane's gap must not break the page
        return _local_shell(title=title, body=body, user=user, csrf=csrf, nonce=nonce, active=active)
    if not isinstance(document, str) or not document.strip():
        return _local_shell(title=title, body=body, user=user, csrf=csrf, nonce=nonce, active=active)
    return _ensure_assets(document, nonce)


def _ensure_assets(document: str, nonce: str) -> str:
    """Make sure the shared stylesheet (if shipped) and script are referenced."""
    css = os.path.join(_ASSET_DIR, "treasury.css")
    if "/static/treasury.css" not in document and os.path.isfile(css) and "</head>" in document:
        document = document.replace(
            "</head>",
            '<link rel="stylesheet" href="/static/treasury.css">\n</head>',
            1,
        )
    if "/static/treasury.js" not in document:
        tag = f'<script nonce="{_esc(nonce)}" src="/static/treasury.js" defer></script>\n'
        if "</body>" in document:
            document = document.replace("</body>", tag + "</body>", 1)
        else:
            document += tag
    return document


def _local_url(target) -> str:
    """Reduce a magic link to a path so no external origin reaches the document.

    The session cookie is host-scoped, so a same-host path works for the person
    who is already on this page; an absolute link would only add an origin the
    CSP and the page contract both forbid.
    """
    text = "" if target is None else str(target).strip()
    if not text:
        return ""
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*(/.*)?$", text)
    if match:
        return match.group(1) or "/"
    if text.startswith("//"):
        rest = text[2:]
        slash = rest.find("/")
        return rest[slash:] if slash >= 0 else "/"
    return text


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
def render_login(*, csrf: str, nonce: str, sent: bool = False, error: str | None = None,
                 demo_link: str | None = None) -> str:
    if sent:
        title = "Check your email"
        sub = (
            "If that address belongs to a treasury member, a single-use sign-in "
            "link is in that inbox. Nothing else changed on this device."
        )
        parts = [
            '<div class="banner" role="status">'
            '<span class="banner-strong">Check your email</span>'
            '<span class="banner-body">The sign-in link is single-use and expires in '
            "15 minutes.</span></div>",
            '<p class="note">No password was ever stored, so there is nothing to reset. '
            "An address without a treasury account gets this same page and no mail.</p>",
            '<p class="note"><a href="/treasury/login">Use a different address</a></p>',
        ]
    else:
        title = "Sign in"
        sub = (
            "This is the treasury workspace: accounts, drawdowns, signature "
            "approvals and the donor pool. Access is by emailed link — there is no password."
        )
        parts = [
            '<form method="post" action="/treasury/login">',
            f'  <input type="hidden" name="csrf" value="{_esc(csrf)}">',
            '  <label class="field">Email address',
            '    <input id="lg-email" name="email" type="email" required autocomplete="email" '
            'spellcheck="false" placeholder="name@nation.example">',
            "  </label>",
            '  <div class="row"><button class="btn btn-primary" type="submit">'
            "Email me a sign-in link</button></div>",
            "</form>",
            '<p class="note">Links are single-use and expire in 15 minutes. '
            "Only treasury members can sign in; every attempt is written to the audit trail.</p>",
        ]
        if error:
            parts.insert(1, f'<p class="formmsg" role="alert">{_esc(error)}</p>')

    link = _local_url(demo_link)
    if link and sent:
        parts.insert(
            0,
            '<div class="banner"><span class="banner-strong">Demo mode</span>'
            '<span class="banner-body"><a class="mono" href="'
            + _esc(link)
            + '">open the magic link</a></span></div>',
        )

    body = _fill(
        _template("login"),
        {
            "LG_TITLE": _esc(title),
            "LG_SUB": _esc(sub),
            "LG_BODY": "\n".join(parts),
            "LG_FINE": _esc(_LOGIN_FINE),
        },
    )
    return _page_doc(title=title, body=body, user=None, csrf=csrf, nonce=nonce, active="login")


# --------------------------------------------------------------------------- #
# pool
# --------------------------------------------------------------------------- #
def _kpi(label: str, value: str, sub: str) -> str:
    """One compact KPI tile: label / value / footnote (house classes .k .v .d)."""
    return (
        '  <div class="kpi">'
        f'<div class="k">{_esc(label)}</div>'
        f'<div class="v">{value}</div>'
        f'<div class="d">{_esc(sub)}</div>'
        "</div>"
    )


def _category_amounts(summary: dict) -> dict:
    found: dict[str, Decimal] = {}
    rows = summary.get("categories") or []
    if isinstance(rows, dict):
        rows = [{"category": k, "usd": v} for k, v in rows.items()]
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("category") or "").strip().lower()
        amount = _dec(row.get("usd"))
        if not key or amount is None:
            continue
        found[key] = found.get(key, Decimal("0")) + amount
    return found


def _categories_html(summary: dict) -> tuple[str, list[tuple[int, str]]]:
    amounts = _category_amounts(summary)
    resolved = []
    for key, label in POOL_CATEGORIES:
        amount = amounts.get(key, Decimal("0"))
        if amount == 0:
            for alt_key, value in amounts.items():
                if alt_key == label.lower() or alt_key.startswith(key):
                    amount = value
                    break
        resolved.append((key, label, amount))
    peak = max((a for _k, _l, a in resolved), default=Decimal("0"))
    blocks = []
    percents: list[tuple[int, str]] = []
    for key, label, amount in resolved:
        if peak > 0:
            pct = int(round(float(amount) / float(peak) * 100))
        else:
            pct = 0
        if amount > 0:
            pct = max(pct, 2)
        pct = max(0, min(100, pct))
        percents.append((pct, key))
        blocks.append(
            '      <div class="cat">'
            f'<span class="cname">{_esc(label)}</span>'
            f'<span class="bar" role="img" aria-label="{_esc(label)}: {pct}% of the largest '
            f'category, {_fmt_usd(amount)}">'
            f'<span data-pct="{pct}"></span></span>'
            f'<span class="mono">{_fmt_usd(amount) if amount else "$0.00"}</span>'
            "</div>"
        )
    return "\n".join(blocks), percents


def _donation_form() -> str:
    return (
        '    <form method="get" action="/treasury/pool" data-tr-json="/api/pool/donations">\n'
        '      <div class="formgrid">\n'
        '        <label class="field">Donor label\n'
        '          <input type="text" name="donor_label" required maxlength="120" '
        'autocomplete="off" placeholder="Friends of the Elders Fund">\n'
        "        </label>\n"
        '        <label class="field">Amount (USD)\n'
        '          <input type="text" name="usd_amount" required inputmode="decimal" '
        'autocomplete="off" placeholder="250.00">\n'
        "        </label>\n"
        '        <label class="field">MAZA txid (optional)\n'
        '          <input type="text" name="txid" autocomplete="off" spellcheck="false" '
        'placeholder="transaction id seen on chain">\n'
        "        </label>\n"
        '        <label class="field">Note (optional)\n'
        '          <input type="text" name="note" maxlength="280" autocomplete="off">\n'
        "        </label>\n"
        "      </div>\n"
        '      <div class="row">\n'
        '        <button class="btn btn-primary btn-sm" type="submit">Record donation</button>\n'
        '        <span class="note">Recorded donations appear in the pool statement immediately.</span>\n'
        "      </div>\n"
        '      <p class="formmsg" data-tr-status role="status" aria-live="polite"></p>\n'
        '      <noscript><p class="note">Recording a donation needs JavaScript; the pool '
        'and its statement stay readable without it.</p></noscript>\n'
        "    </form>"
    )


def _stipends_html(stipends: list) -> str:
    if not stipends:
        return '    <p class="empty">No stipend payments recorded for this pool yet.</p>'
    rows = []
    for raw in stipends:
        if not isinstance(raw, dict):
            continue
        # Tolerant key lookup: the service may hand back store columns
        # (payee_name/usd_amount/maza_amount/sent_ts/id) or the page-facing
        # aliases. Both are the same payment.
        item = dict(raw)
        item.setdefault("recipient", raw.get("payee_name"))
        item.setdefault("usd", raw.get("usd_amount"))
        item.setdefault("maza", raw.get("maza_amount"))
        item.setdefault("ts", raw.get("sent_ts"))
        item.setdefault("item_id", raw.get("id"))
        rate = _fmt_rate(item.get("rate"))
        source = item.get("rate_source")
        rate_cell = rate if not source else f'{rate}<br><span class="faint">{_txt(source)}</span>'
        txid = item.get("txid")
        tx_cell = (
            f'<span class="mono" title="{_esc(txid)}">{_short_hash(txid)}</span>'
            if txid
            else "—"
        )
        item_id = item.get("item_id")
        request_ref = item.get("request_ref")
        shown_ref = item.get("ref") or request_ref
        if item_id:
            ref_cell = (
                f'<a class="mono" href="/treasury/payslips/{_esc(item_id)}" '
                f'title="Open payslip">{_txt(shown_ref)}</a>'
            )
        else:
            ref_cell = f'<span class="mono">{_txt(shown_ref)}</span>'
        if request_ref and str(request_ref) != str(shown_ref or ""):
            ref_cell += f'<br><span class="faint">{_esc(request_ref)}</span>'
        rows.append(
            "        <tr>\n"
            f"          <td>{ref_cell}</td>\n"
            f'          <td>{_txt(item.get("recipient"))}</td>\n'
            f'          <td>{_txt(item.get("category"))}</td>\n'
            f'          <td class="n">{_fmt_usd(item.get("usd"))}</td>\n'
            f'          <td class="n">{_fmt_maza(item.get("maza"))}</td>\n'
            f'          <td class="n">{rate_cell}</td>\n'
            f'          <td class="n">{_fmt_date(item.get("ts"))}</td>\n'
            f"          <td>{_pill(item.get('status'))}</td>\n"
            "        </tr>"
        )
    return (
        '    <table class="tbl" aria-label="Stipend items paid from the donor pool, newest first">\n'
        "      <thead>\n        <tr>\n"
        "          <th>Ref</th>\n"
        "          <th>Recipient</th>\n"
        "          <th>Category</th>\n"
        '          <th class="n">USD</th>\n'
        '          <th class="n">MAZA</th>\n'
        '          <th class="n">Rate</th>\n'
        '          <th class="n">Date</th>\n'
        "          <th>Status</th>\n"
        "        </tr></thead>\n"
        "      <tbody>\n" + "\n".join(rows) + "\n      </tbody>\n    </table>"
    )


def _bar_css(percents: list[tuple[int, str]]) -> str:
    """Per-page, nonce-carrying rules: bar widths are data, so they cannot live in
    the static stylesheet (and CSP forbids inline style attributes)."""
    base = (
        ".tr-cats .cat{grid-template-columns:minmax(128px,0.9fr) minmax(88px,1.6fr) auto;"
        "gap:12px;align-items:center}"
    )
    seen: list[int] = []
    for pct, _key in percents:
        if pct in seen:
            continue
        seen.append(pct)
        base += f'.tr-cats .bar > span[data-pct="{pct}"]{{width:{pct}%}}'
    return base


def _scheduled(requests) -> tuple[int, Decimal]:
    count = 0
    total = Decimal("0")
    for item in requests or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").strip() in PENDING_STATUSES:
            count += 1
            amount = _dec(item.get("usd_total") if "usd_total" in item else item.get("usd"))
            if amount is not None:
                total += amount
    return count, total


def render_pool(*, user: dict, csrf: str, nonce: str, data: dict) -> str:
    user = user or {}
    data = data or {}
    summary = data.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    stipends = [s for s in (data.get("stipends") or []) if isinstance(s, dict)]
    role = str(user.get("role") or "")
    flag = data.get("can_donate")
    allowed_role = role in ("treasurer", "admin")
    can_donate = allowed_role if flag is None else (bool(flag) and allowed_role)

    balance_maza = _fmt_maza(summary.get("balance_maza"))
    balance_usd = _fmt_usd(summary.get("balance_usd"))
    paid_week = _fmt_usd(summary.get("paid_week_usd"))
    donated_month = _fmt_usd(summary.get("donated_month_usd"))
    donors = summary.get("donors")
    donor_text = _fmt_maza(donors) if donors is not None else "—"
    if donors is not None:
        try:
            donor_text = f"{int(donors):,}"
        except (TypeError, ValueError):
            donor_text = _txt(donors)
    scheduled_count, scheduled_usd = _scheduled(data.get("requests"))

    kpis = "\n".join(
        (
            _kpi("Pool balance", f"{balance_maza} MAZA", f"≈ {balance_usd} at the locked rate"),
            _kpi("Paid this week", paid_week, "stipends settled from this pool"),
            _kpi("Donors", donor_text, f"{donated_month} recorded this month"),
            _kpi(
                "Scheduled",
                f"{scheduled_count} request" + ("" if scheduled_count == 1 else "s"),
                f"{_fmt_usd(scheduled_usd)} queued for signature",
            ),
        )
    )

    cats_html, percents = _categories_html(summary)

    if can_donate:
        donate_html = _donation_form()
        donate_role = "treasurer · admin"
        donate_section = (
            '<section class="card" aria-labelledby="donate-h">\n'
            '  <h2 class="card-h" id="donate-h">Record a donation '
            f'<span class="right faint">{_esc(donate_role)}</span></h2>\n'
            f"{donate_html}\n</section>"
        )
    else:
        # A reader without the role sees no donation card at all: an empty
        # permission box is noise, not information.
        donate_html = ""
        donate_role = _txt(role or "read only")
        donate_section = ""

    body = _fill(
        _template("pool"),
        {
            "KPIS": kpis,
            "CATS": cats_html,
            "DONATE": donate_html,
            "DONATE_SECTION": donate_section,
            "DONATE_ROLE": donate_role,
            "STIPENDS": _stipends_html(stipends),
            "STIPEND_COUNT": f"{len(stipends)} row" + ("" if len(stipends) == 1 else "s"),
            "MONTH_LABEL": _esc(_month_label(_month_now())),
            "BAR_CSS": _bar_css(percents),
            "NONCE": _esc(nonce),
        },
    )
    return _page_doc(title="Donor pool", body=body, user=user, csrf=csrf, nonce=nonce, active="pool")


# --------------------------------------------------------------------------- #
# statements
# --------------------------------------------------------------------------- #
def _statement_rows(rows: list) -> str:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        txids = row.get("txids") or []
        if isinstance(txids, str):
            txids = [txids] if txids else []
        signers = row.get("signers") or []
        if isinstance(signers, str):
            signers = [signers] if signers else []
        tx_html = (
            "<br>".join(
                f'<span class="mono" title="{_esc(t)}">{_short_hash(t)}</span>' for t in txids if t
            )
            or '<span class="faint">—</span>'
        )
        signer_html = (
            "<br>".join(f'<span class="faint">{_esc(s)}</span>' for s in signers if s) or ""
        )
        account = f'<span class="mono">{_txt(row.get("account_code"))}</span>'
        if row.get("account_name"):
            account += f'<br><span class="faint">{_esc(row.get("account_name"))}</span>'
        ref = f'<span class="mono">{_txt(row.get("ref"))}</span>'
        if signer_html:
            ref += "<br>" + signer_html
        rate = _fmt_rate(row.get("rate"))
        if row.get("rate_source"):
            rate += f'<br><span class="faint">{_esc(row.get("rate_source"))}</span>'
        out.append(
            "        <tr>\n"
            f"          <td>{ref}</td>\n"
            f'          <td class="mono">{_fmt_date(row.get("date"))}</td>\n'
            f"          <td>{account}</td>\n"
            f'          <td>{_txt(row.get("kind"))}</td>\n'
            f"          <td>{_pill(row.get('status'))}</td>\n"
            f'          <td class="n">{_fmt_usd(row.get("usd"))}</td>\n'
            f'          <td class="n">{_fmt_maza(row.get("maza"))}</td>\n'
            f'          <td class="n">{rate}</td>\n'
            f'          <td>{_txt(row.get("purpose"))}<br>{tx_html}</td>\n'
            "        </tr>"
        )
    if not out:
        return (
            '        <tr><td colspan="9" class="faint">'
            "No rows for this month. Pick another month, or another ledger above.</td></tr>"
        )
    return "\n".join(out)


def render_statements(*, user: dict, csrf: str, nonce: str, data: dict) -> str:
    user = user or {}
    data = data or {}
    kind = str(data.get("kind") or "requests").strip().lower()
    if kind not in ("requests", "pool"):
        kind = "requests"
    month = str(data.get("month") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        month = _month_now()
    rows = [r for r in (data.get("rows") or []) if isinstance(r, dict)]
    totals = data.get("totals") or {}
    if not isinstance(totals, dict):
        totals = {}

    kind_label = "Requests" if kind == "requests" else "Donor pool"
    now = _month_now()
    body = _fill(
        _template("statements"),
        {
            "KIND": _esc(kind),
            "KIND_LABEL": _esc(kind_label),
            "MONTH": _esc(month),
            "MONTH_LABEL": _esc(_month_label(month)),
            "MIN_MONTH": "2000-01",
            "MAX_MONTH": _esc(now),
            "K_REQ_CLASS": "btn btn-primary btn-sm" if kind == "requests" else "btn btn-ghost btn-sm",
            "K_POOL_CLASS": "btn btn-primary btn-sm" if kind == "pool" else "btn btn-ghost btn-sm",
            "K_REQ_ARIA": ' aria-current="true"' if kind == "requests" else "",
            "K_POOL_ARIA": ' aria-current="true"' if kind == "pool" else "",
            "SEL_REQUESTS": " selected" if kind == "requests" else "",
            "SEL_POOL": " selected" if kind == "pool" else "",
            "ROWS": _statement_rows(rows),
            "ROW_COUNT": f"{len(rows)} row" + ("" if len(rows) == 1 else "s"),
            "TOT_USD": _fmt_usd(totals.get("usd")),
            "TOT_MAZA": _fmt_maza(totals.get("maza")),
        },
    )
    return _page_doc(
        title=f"Statements · {kind_label}", body=body, user=user, csrf=csrf, nonce=nonce,
        active="statements",
    )


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
def _audit_rows(rows: list) -> str:
    out = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        detail = row.get("detail")
        if isinstance(detail, str):
            detail_text = detail
        else:
            detail_text = "" if detail is None else str(detail)
        if detail_text:
            detail_html = f'<br><span class="faint">{_txt(detail_text[:120])}</span>'
        else:
            detail_html = ""
        obj_type = row.get("object_type")
        obj_id = row.get("object_id")
        obj_cell = f'<span class="mono">{_txt(obj_type)}</span>'
        if obj_id:
            obj_cell += (
                f'<br><span class="mono faint" title="{_esc(obj_id)}">{_short_hash(obj_id)}</span>'
            )
        out.append(
            "        <tr>\n"
            f'          <td class="n">{_esc(row.get("id") if row.get("id") is not None else index)}</td>\n'
            f'          <td class="n">{_fmt_ts(row.get("ts"))}</td>\n'
            f"          <td>{_txt(row.get('actor_user_id') or 'system')}</td>\n"
            f'          <td><span class="mono">{_txt(row.get("action"))}</span>{detail_html}</td>\n'
            f"          <td>{obj_cell}</td>\n"
            f'          <td class="mono" title="{_esc(row.get("hash"))}">{_short_hash(row.get("hash"))}</td>\n'
            f'          <td class="mono" title="{_esc(row.get("prev_hash"))}">'
            f'{_short_hash(row.get("prev_hash"))}</td>\n'
            "        </tr>"
        )
    if not out:
        return (
            '        <tr><td colspan="7" class="faint">'
            "No audit rows match this filter.</td></tr>"
        )
    return "\n".join(out)


def render_audit(*, user: dict, csrf: str, nonce: str, data: dict) -> str:
    user = user or {}
    data = data or {}
    rows = [r for r in (data.get("rows") or []) if isinstance(r, dict)]
    object_type = str(data.get("object_type") or "")
    object_id = str(data.get("object_id") or "")
    try:
        limit = int(data.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 2000))

    if object_type or object_id:
        bits = []
        if object_type:
            bits.append(f'object_type = "{object_type}"')
        if object_id:
            bits.append(f'object_id = "{object_id}"')
        filter_note = (
            '    <p class="note">Filtered to '
            + _esc(" and ".join(bits))
            + ' · <a href="/treasury/audit">clear</a></p>'
        )
    else:
        filter_note = '    <p class="note">Showing every object type.</p>'

    body = _fill(
        _template("audit"),
        {
            "ROWS": _audit_rows(rows),
            "ROW_COUNT": f"{len(rows)} row" + ("" if len(rows) == 1 else "s"),
            "OBJ_TYPE": _esc(object_type),
            "OBJ_ID": _esc(object_id),
            "LIMIT": _esc(limit),
            "FILTER_NOTE": filter_note,
        },
    )
    return _page_doc(
        title="Audit trail", body=body, user=user, csrf=csrf, nonce=nonce, active="audit"
    )
