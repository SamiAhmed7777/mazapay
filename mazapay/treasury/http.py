"""Treasury HTTP layer: one ThreadingHTTPServer, one router, no framework.

House style is the bridge's (``mazapay/api.py``): stdlib only, an explicit
header set on every response, an exact-filename static whitelist and a single
access-log line per request. Differences are the ones the contract pins for
this product: a per-response CSP nonce, cookie sessions, double-submit CSRF on
every POST, and a JSON error envelope ``{"error": {"code", "message"}}``.

Routes (contract §9)::

    pages   /  /treasury/{login,auth/*,logout,accounts,requests,requests/{id},
            pool,statements,audit,users,payslips/{id},users/{id}/deactivate}  /static/{name}  /healthz
    api     /api/{me,accounts,requests,requests/{id},requests/{id}/{action},
            pool,pool/donations,pool/stipends,statements,audit,users}

Domain logic is NOT here. Every mutation goes through ``treasury.service`` and
every identity decision through ``treasury.auth``. Both are imported lazily so
a sibling lane landing late cannot stop this module from importing (a missing
module yields 503 / a plain "not available" card, never a stack trace in the
browser).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import layout, settings

__all__ = ["Handler", "TreasuryHTTPServer", "make_server", "serve", "start"]

_ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets"
)

# EXACT-FILENAME whitelist, mirroring mazapay/api.py: a request can never name a
# path, so traversal, absolute paths, encoded separators and NUL are all just
# 404s and the filesystem never sees client-controlled text.
_ASSETS: Dict[str, Tuple[str, str]] = {
    "treasury.css": ("text/css; charset=utf-8", "treasury.css"),
    "treasury.js": ("text/javascript; charset=utf-8", "treasury.js"),
    "mazacoin-180.png": ("image/png", "mazacoin-180.png"),
    "mazacoin-wordmark.png": ("image/png", "mazacoin-wordmark.png"),
    "mazacoin-accepted.png": ("image/png", "mazacoin-accepted.png"),
    "mazacoin-64.png": ("image/png", "mazacoin-64.png"),
}

_CSP = (
    "default-src 'none'; style-src 'self' 'nonce-{nonce}'; "
    "script-src 'self' 'nonce-{nonce}'; img-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)

_MAX_BODY = 1024 * 1024  # contract §9: 1 MiB

_ACCESS_LOG = logging.getLogger("mazapay.treasury.access")
_ERROR_LOG = logging.getLogger("mazapay.treasury.errors")
_ACCESS_LOG.addHandler(logging.NullHandler())
_ERROR_LOG.addHandler(logging.NullHandler())

# Demo-mode magic-link handoff (see _demo_link_store/_demo_link_take).
_DEMO_LINKS: Dict[str, Tuple[str, float]] = {}
_DEMO_LOCK = threading.Lock()

# Request ids / refs / item ids: bounded, path-safe alphabet only.
_ID = r"[A-Za-z0-9_-]{1,64}"

_ROUTES: Tuple[Tuple[str, str, str], ...] = (
    ("GET", r"^/$", "_h_root"),
    ("GET", r"^/healthz$", "_h_healthz"),
    ("GET", r"^/static/(?P<name>[^/]+)$", "_h_static"),
    # --- auth pages
    ("GET", r"^/treasury/login$", "_h_login_get"),
    ("POST", r"^/treasury/login$", "_h_login_post"),
    ("GET", r"^/treasury/auth/(?P<token>[A-Za-z0-9_-]{1,128})$", "_h_auth_consume"),
    ("POST", r"^/treasury/logout$", "_h_logout"),
    # --- core screens (lane 3)
    ("GET", r"^/treasury/accounts$", "_h_accounts"),
    ("GET", r"^/treasury/requests$", "_h_requests"),
    ("GET", r"^/treasury/requests/(?P<rid>" + _ID + r")$", "_h_request"),
    ("POST", r"^/treasury/requests/(?P<rid>" + _ID + r")/(?P<action>process|submit|cancel|complete|sign)$",
     "_h_request_action"),
    ("GET", r"^/treasury/payslips/(?P<iid>" + _ID + r")$", "_h_payslip"),
    # --- sibling screens (lane 4 renderers, dispatched here)
    ("GET", r"^/treasury/pool$", "_h_pool"),
    ("GET", r"^/treasury/statements$", "_h_statements"),
    ("GET", r"^/treasury/audit$", "_h_audit"),
    ("GET", r"^/treasury/users$", "_h_users"),
    ("POST", r"^/treasury/users$", "_h_users_create"),
    ("POST", r"^/treasury/users/(?P<uid>" + _ID + r")/deactivate$", "_h_user_deactivate"),
    # --- JSON API
    ("GET", r"^/api/me$", "_a_me"),
    ("GET", r"^/api/accounts$", "_a_accounts"),
    ("GET", r"^/api/requests$", "_a_requests"),
    ("POST", r"^/api/requests$", "_a_request_create"),
    ("GET", r"^/api/requests/(?P<rid>" + _ID + r")$", "_a_request"),
    ("POST", r"^/api/requests/(?P<rid>" + _ID + r")/(?P<action>process|submit|cancel|complete|sign)$",
     "_a_request_action"),
    ("GET", r"^/api/pool$", "_a_pool"),
    ("POST", r"^/api/pool/donations$", "_a_donation"),
    ("GET", r"^/api/pool/stipends$", "_a_stipends"),
    ("GET", r"^/api/statements$", "_a_statements"),
    ("GET", r"^/api/audit$", "_a_audit"),
    ("GET", r"^/api/users$", "_a_users"),
    ("POST", r"^/api/users$", "_a_user_create"),
    ("POST", r"^/api/users/(?P<uid>" + _ID + r")/deactivate$", "_a_user_deactivate"),
)

_COMPILED: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = tuple(
    (method, re.compile(pattern), handler) for method, pattern, handler in _ROUTES
)

_ALLOWED_STATUS = {
    "prepared",
    "processed",
    "pending_signatures",
    "authorized",
    "completed",
    "completed_partial",
    "cancelled",
    "declined",
}

_SIGNER_ROLES = {"treasurer", "executive", "council_treasurer"}


# --------------------------------------------------------------------- errors --
class HttpError(Exception):
    """Transport-level refusal; always shaped into the contract envelope."""

    def __init__(self, status: int, code: str, message: str, *, page: bool = False):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.page = page


class Redirect(Exception):
    def __init__(self, location: str, status: int = 303):
        super().__init__(location)
        self.location = location
        self.status = int(status)


# -------------------------------------------------------------- lazy imports --
def _service():
    from . import service  # noqa: PLC0415 - sibling lane, imported on demand

    return service


def _auth():
    from . import auth  # noqa: PLC0415

    return auth


def _statements():
    from . import statements  # noqa: PLC0415

    return statements


def _demo_link_store(link: str) -> str:
    """Demo-mode only: park a magic link under a short one-time key.

    The link is never placed in a redirect URL (it would land in browser
    history and access logs); the key is, and the landing page shows the link
    once. Entries expire in 120s and the map is capped.
    """
    key = secrets.token_urlsafe(9)
    now = time.time()
    with _DEMO_LOCK:
        for stale in [k for k, (_, exp) in _DEMO_LINKS.items() if exp < now]:
            _DEMO_LINKS.pop(stale, None)
        if len(_DEMO_LINKS) >= 50:
            _DEMO_LINKS.pop(next(iter(_DEMO_LINKS)), None)
        _DEMO_LINKS[key] = (link, now + 120)
    return key


def _demo_link_take(key: str) -> Optional[str]:
    with _DEMO_LOCK:
        entry = _DEMO_LINKS.pop(key, None)
    if not entry:
        return None
    link, expires = entry
    return link if expires >= time.time() else None


def _pages():
    from . import pages_pool  # noqa: PLC0415

    return pages_pool


def _treasury_error_class():
    try:
        return _service().TreasuryError
    except Exception:  # noqa: BLE001 - module missing or broken import
        return None


def _ensure_db() -> None:
    """Idempotent schema bring-up (contract §4: server start and every test)."""
    try:
        from . import store as treasury_store  # noqa: PLC0415

        treasury_store.init_db()
    except Exception:  # noqa: BLE001
        _ERROR_LOG.exception("treasury store init failed")
    try:
        _auth().init_db()
    except Exception:  # noqa: BLE001 - auth owns its own tables; store covers them
        pass


# ------------------------------------------------------------------- helpers --
def _now() -> float:
    return time.time()


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error_payload(code: str, message: str) -> Dict[str, Any]:
    return {"error": {"code": str(code), "message": str(message)}}


def _mask_path(path: str) -> str:
    """Never log a magic-link token (it is a bearer credential)."""
    if path.startswith("/treasury/auth/"):
        return "/treasury/auth/[redacted]"
    return path


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


# -------------------------------------------------------------------- server --
class TreasuryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # pragma: no cover - last resort
        _ERROR_LOG.exception("unhandled connection error from %s", client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "MazaPayTreasury"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # BaseHTTPRequestHandler's stderr chatter is replaced by the access log
        # written in _dispatch (one structured line, no headers, no cookies).
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self._dispatch("POST")

    def version_string(self) -> str:
        return self.server_version

    # -- per-request state ----------------------------------------------------
    def _reset(self) -> None:
        self._nonce = secrets.token_urlsafe(16)
        self._extra_cookies: List[str] = []
        self._status = 0
        self._info: Any = _UNSET
        self._session_token: Optional[str] = None
        self._jar: Optional[Dict[str, str]] = None

    def _dispatch(self, method: str) -> None:
        self._reset()
        started = time.perf_counter()
        try:
            self._route(method)
        except Redirect as redirect:
            self._send_redirect(redirect.location, redirect.status)
        except HttpError as exc:
            self._send_http_error(exc)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            treasury_error = _treasury_error_class()
            if treasury_error is not None and isinstance(exc, treasury_error):
                self._send_domain_error(exc)
            elif isinstance(exc, (ValueError, KeyError, TypeError)):
                self._send_http_error(HttpError(400, "invalid_request", str(exc)))
            else:
                _ERROR_LOG.exception("unhandled error serving %s %s", method, _mask_path(urlparse(self.path).path))
                self._send_http_error(HttpError(500, "internal_error", "internal server error"))
        finally:
            status = self._status or 0
            level = logging.WARNING if status in (400, 401, 403, 404, 405, 408, 413, 429, 503) else logging.INFO
            _ACCESS_LOG.log(
                level,
                '%s "%s %s" %s %.3fs',
                self.client_address[0] if self.client_address else "-",
                method,
                _mask_path(urlparse(self.path).path),
                status,
                time.perf_counter() - started,
            )

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        for route_method, pattern, handler_name in _COMPILED:
            match = pattern.match(path)
            if not match:
                continue
            if route_method != method:
                continue
            getattr(self, handler_name)(match.groupdict(), query)
            return
        # Path exists under another method -> 405, otherwise 404. Both JSON.
        if any(pattern.match(path) for _m, pattern, _h in _COMPILED):
            raise HttpError(405, "method_not_allowed", f"{method} is not allowed on {path}")
        raise HttpError(404, "not_found", f"no route for {method} {path}")

    # -- cookies / session ----------------------------------------------------
    def _cookies(self) -> Dict[str, str]:
        if self._jar is not None:
            return self._jar
        jar: Dict[str, str] = {}
        raw = self.headers.get("Cookie") or ""
        if raw:
            parsed = SimpleCookie()
            try:
                parsed.load(raw)
            except CookieError:
                parsed = SimpleCookie()
            for key, morsel in parsed.items():
                jar[key] = morsel.value
        self._jar = jar
        return jar

    def _session_info(self) -> Optional[Dict[str, Any]]:
        if self._info is not _UNSET:
            return self._info
        token = self._cookies().get(settings.SESSION_COOKIE)
        if not token:
            self._info = None
            return None
        self._session_token = token
        try:
            self._info = _auth().resolve_session(token)
        except ImportError:
            self._info = None
        except Exception:  # noqa: BLE001 - a broken session table is not a 500 page
            _ERROR_LOG.exception("session resolve failed")
            self._info = None
        return self._info

    def _user(self) -> Optional[Dict[str, Any]]:
        info = self._session_info()
        return (info or {}).get("user") if isinstance(info, dict) else None

    def _csrf_cookie(self) -> str:
        """Current CSRF token, issuing/repairing the cookie as needed.

        For a signed-in session the token is derived from the session token
        hash by ``auth.check_csrf``'s own rule, so the double-submit works
        across restarts without storing a CSRF column.
        """
        jar = self._cookies()
        current = jar.get(settings.CSRF_COOKIE) or ""
        info = self._session_info()
        if isinstance(info, dict) and info.get("session"):
            expected = str(info["session"].get("csrf") or "")
            if expected and current != expected:
                jar[settings.CSRF_COOKIE] = expected
                self._queue_cookie(settings.CSRF_COOKIE, expected, http_only=False)
            return expected or current
        if current:
            return current
        try:
            token = _auth().new_csrf_token()
        except Exception:  # noqa: BLE001 - auth lane missing
            token = secrets.token_urlsafe(32)
        jar[settings.CSRF_COOKIE] = token
        self._queue_cookie(settings.CSRF_COOKIE, token, http_only=False)
        return token

    def _queue_cookie(self, name: str, value: str, *, http_only: bool = True, max_age: Optional[int] = None) -> None:
        parts = [f"{name}={value}", "Path=/", "SameSite=Lax"]
        if http_only:
            parts.append("HttpOnly")
        if settings.COOKIE_SECURE:
            parts.append("Secure")
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        self._extra_cookies.append("; ".join(parts))

    def _check_csrf(self, submitted: Optional[str]) -> bool:
        if not submitted:
            return False
        cookie = self._cookies().get(settings.CSRF_COOKIE) or ""
        if not cookie:
            return False
        if not hmac.compare_digest(cookie, str(submitted)):
            return False
        info = self._session_info()
        session = (info or {}).get("session") if isinstance(info, dict) else None
        if session:
            try:
                if not _auth().check_csrf(session, cookie, str(submitted)):
                    return False
            except Exception:  # noqa: BLE001 - double-submit already matched
                _ERROR_LOG.warning("auth.check_csrf unavailable; double-submit only")
        return True

    def _require_page_session(self) -> Tuple[Dict[str, Any], str]:
        user = self._user()
        if not user:
            raise Redirect("/treasury/login")
        return user, self._csrf_cookie()

    def _require_api_session(self) -> Dict[str, Any]:
        user = self._user()
        if not user:
            raise HttpError(401, "not_authenticated", "sign-in required")
        return user

    def _require_admin(self) -> Dict[str, Any]:
        user = self._require_api_session()
        if str(user.get("role") or "") != "admin":
            raise HttpError(403, "insufficient_role", "administrator role required", page=True)
        return user

    # -- bodies ---------------------------------------------------------------
    def _read_raw(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            # Chunked bodies have no Content-Length to cap; refuse rather than
            # read a streaming body with no 1 MiB guarantee.
            self.close_connection = True
            raise HttpError(411, "length_required", "Content-Length is required")
        raw_length = self.headers.get("Content-Length")
        if raw_length in (None, ""):
            return b""
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self.close_connection = True
            raise HttpError(400, "invalid_request", "invalid Content-Length") from None
        if length <= 0:
            return b""
        if length > _MAX_BODY:
            # Refuse BEFORE buffering: a 2 GB declared body must not be read.
            self.close_connection = True
            raise HttpError(413, "payload_too_large", "request body exceeds 1 MiB")
        try:
            return self.rfile.read(length)
        except (TimeoutError, OSError):
            self.close_connection = True
            raise HttpError(400, "invalid_request", "request body read failed") from None

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_raw()
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise HttpError(400, "invalid_json", "invalid JSON body") from None
        if not isinstance(payload, dict):
            raise HttpError(400, "invalid_json", "JSON object body required")
        return payload

    def _read_form(self) -> Dict[str, str]:
        raw = self._read_raw()
        if not raw:
            return {}
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HttpError(400, "invalid_request", "form body must be UTF-8") from None
        parsed = parse_qs(text, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    # -- responses ------------------------------------------------------------
    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", _CSP.format(nonce=self._nonce))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")

    def _send(
        self,
        status: int,
        body: bytes = b"",
        content_type: str = "text/html; charset=utf-8",
        *,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._status = int(status)
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        if "Cache-Control" not in (extra_headers or {}) and (
            content_type.startswith("text/html") or content_type.startswith("application/json")
        ):
            # Authenticated pages and API payloads must not sit in a shared cache.
            self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        for cookie in self._extra_cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _html(self, status: int, document: str) -> None:
        self._send(status, document.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, status: int, payload: Any, *, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json", extra_headers=extra_headers)

    def _redirect(self, location: str, status: int = 303) -> None:
        self._send(status, b"", "text/html; charset=utf-8", extra_headers={"Location": location})

    # Redirect is raised; this is the sender used by _dispatch.
    _send_redirect = _redirect

    def _send_http_error(self, exc: HttpError) -> None:
        if exc.page:
            self._render_page_error(exc.status, exc.code, exc.message)
            return
        self._json(exc.status, _error_payload(exc.code, exc.message))

    def _render_page_error(self, status: int, code: str, message: str) -> None:
        try:
            user, csrf = self._require_page_session()
        except Redirect:
            self._json(status, _error_payload(code, message))
            return
        except HttpError:
            self._json(status, _error_payload(code, message))
            return
        document = layout.render_error_page(
            title="Not available",
            user=user,
            csrf=csrf,
            nonce=self._nonce,
            nav_active="",
            message=message,
        )
        self._html(status, document)

    def _send_domain_error(self, exc: Exception) -> None:
        """TreasuryError -> the contract envelope, status normalised defensively.

        Sibling lanes construct the class slightly differently at the edges
        (``status`` occasionally arrives as the message), so the status is
        coerced to a real 4xx/5xx and the message falls back to ``str(exc)``.
        """
        status = _as_int(getattr(exc, "status", 400), 400)
        if not 400 <= status <= 599:
            status = 400
        code = str(getattr(exc, "code", "error") or "error")
        raw_message = getattr(exc, "message", None)
        message = raw_message if isinstance(raw_message, str) and raw_message else str(exc)
        path = urlparse(self.path).path
        if path.startswith("/treasury/") and status in (403, 409, 404):
            self._render_page_error(status, code, message)
            return
        self._json(status, _error_payload(code, message))

    # -- handlers: infrastructure --------------------------------------------
    def _h_root(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        self._redirect("/treasury/accounts")

    def _h_healthz(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        self._json(200, {"ok": True, "mode": "demo" if settings.DEMO_MODE else "live", "service": "mazapay-treasury"})

    def _h_static(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        name = groups.get("name") or ""
        entry = _ASSETS.get(name)
        if entry is None:
            raise HttpError(404, "not_found", "no such asset")
        content_type, filename = entry
        try:
            with open(os.path.join(_ASSETS_DIR, filename), "rb") as fh:
                body = fh.read()
        except OSError:
            raise HttpError(404, "not_found", "no such asset") from None
        self._send(200, body, content_type, extra_headers={"Cache-Control": "public, max-age=604800"})

    # -- handlers: auth pages -------------------------------------------------
    def _h_login_get(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        csrf = self._csrf_cookie()
        sent = (query.get("sent") or [""])[0] == "1"
        raw_error = (query.get("error") or [""])[0]
        messages = {
            "invalid": "That sign-in link is invalid, expired or already used.",
            "csrf": "Your sign-in page expired. Reload and try again.",
            "unavailable": "Sign-in is not configured yet.",
        }
        error = messages.get(raw_error, raw_error or None)
        demo_link = None
        if settings.DEMO_MODE:
            key = (query.get("dl") or [""])[0]
            if key:
                demo_link = _demo_link_take(key)
        try:
            pages = _pages()
        except ImportError:
            self._html(200, layout.render_unavailable(
                title="Sign in · MazaPay Treasury", user=None, csrf=csrf,
                nonce=self._nonce, nav_active="", message="The sign-in screen is not available yet.",
            ))
            return
        document = pages.render_login(csrf=csrf, nonce=self._nonce, sent=sent, error=error, demo_link=demo_link)
        self._html(200, document)

    def _h_login_post(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        form = self._read_form()
        if not self._check_csrf(form.get("csrf")):
            raise HttpError(403, "csrf_failed", "the sign-in form expired; reload and try again", page=True)
        email = (form.get("email") or "").strip()
        link = None
        try:
            result = _auth().request_magic_link(email, settings.BASE_URL)
            if isinstance(result, dict):
                link = result.get("link")
        except ImportError:
            self._redirect("/treasury/login?error=unavailable")
            return
        except Exception as exc:  # noqa: BLE001
            treasury_error = _treasury_error_class()
            if treasury_error is not None and isinstance(exc, treasury_error):
                raise
            _ERROR_LOG.exception("magic link request failed")
            self._redirect("/treasury/login?error=unavailable")
            return
        # Always the same landing page: no user enumeration, no email disclosure.
        # In demo mode the minted link is handed back ONCE under an opaque key
        # so staging can be driven without a mail server.
        if settings.DEMO_MODE and link:
            self._redirect("/treasury/login?sent=1&dl=" + _demo_link_store(link))
            return
        self._redirect("/treasury/login?sent=1")

    def _h_auth_consume(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        token = groups.get("token") or ""
        try:
            result = _auth().consume_magic_link(token)
        except ImportError:
            self._redirect("/treasury/login?error=unavailable")
            return
        if not result:
            self._redirect("/treasury/login?error=invalid")
            return
        session_token = str(result.get("session_token") or "")
        if not session_token:
            self._redirect("/treasury/login?error=invalid")
            return
        self._queue_cookie(settings.SESSION_COOKIE, session_token, http_only=True)
        csrf = ""
        try:
            info = _auth().resolve_session(session_token) or {}
            csrf = str((info.get("session") or {}).get("csrf") or "")
        except Exception:  # noqa: BLE001
            csrf = ""
        if not csrf:
            try:
                csrf = _auth().new_csrf_token()
            except Exception:  # noqa: BLE001
                csrf = secrets.token_urlsafe(32)
        self._queue_cookie(settings.CSRF_COOKIE, csrf, http_only=False)
        self._redirect("/treasury/accounts")

    def _h_logout(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        form = self._read_form()
        if not self._check_csrf(form.get("csrf")):
            raise HttpError(403, "csrf_failed", "the sign-out form expired; reload and try again", page=True)
        token = self._session_token
        if token:
            try:
                _auth().revoke_session(token)
            except Exception:  # noqa: BLE001 - logout must always succeed client-side
                _ERROR_LOG.exception("session revoke failed")
        self._queue_cookie(settings.SESSION_COOKIE, "", http_only=True, max_age=0)
        self._queue_cookie(settings.CSRF_COOKIE, "", http_only=False, max_age=0)
        self._redirect("/treasury/login")

    # -- handlers: core screens ----------------------------------------------
    def _page_guard(self, fn, *, title: str, nav_active: str, user, csrf: str):
        """Turn a domain/sibling failure into a readable card, never a 500 page."""
        try:
            return fn()
        except HttpError:
            raise
        except Exception as exc:  # noqa: BLE001
            treasury_error = _treasury_error_class()
            if treasury_error is not None and isinstance(exc, treasury_error):
                message = str(getattr(exc, "message", None) or exc)
                status = _as_int(getattr(exc, "status", 400), 400)
                code = str(getattr(exc, "code", "error"))
            else:
                _ERROR_LOG.exception("page render failed: %s", title)
                message, status, code = "The treasury could not build this page.", 500, "internal_error"
            self._html(status if 400 <= status <= 599 else 400,
                       layout.render_error_page(title=title, user=user, csrf=csrf,
                                                nonce=self._nonce, nav_active=nav_active,
                                                message=message))
            return None

    def _accounts_data(self, user: Dict[str, Any]) -> Dict[str, Any]:
        svc = _service()
        accounts = svc.list_accounts(user)
        requests = svc.list_requests(user, limit=200)
        accounts_by_id = {str(a.get("id")): a for a in accounts if isinstance(a, dict)}
        needs: List[Dict[str, Any]] = []
        if str(user.get("role") or "") in _SIGNER_ROLES:
            for row in requests:
                if str(row.get("status") or "") != "pending_signatures":
                    continue
                try:
                    detail = svc.get_request(user, row["id"])
                except Exception:  # noqa: BLE001 - one bad row must not hide the rest
                    continue
                payload = dict(detail.get("request") or detail)
                signatures = detail.get("signatures") or []
                account = detail.get("account") or accounts_by_id.get(str(row.get("account_id")))
                if layout.user_can_sign(user, dict(payload, signatures=signatures), account):
                    needs.append(payload)
                if len(needs) >= 20:
                    break
        month = time.strftime("%Y-%m", time.gmtime())
        month_count = 0
        month_requests: List[Dict[str, Any]] = []
        for row in requests:
            try:
                stamp = float(row.get("prepared_ts") or 0)
            except (TypeError, ValueError):
                continue
            if time.strftime("%Y-%m", time.gmtime(stamp)) == month:
                month_count += 1
                month_requests.append(row)
        return {
            "accounts": accounts,
            "needs_signature": needs,
            "activity": svc.audit_rows(user, limit=8),
            "pool": svc.pool_summary(user),
            "month_count": month_count,
            "month_requests": month_requests,
        }

    def _h_accounts(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, csrf = self._require_page_session()
        data = self._page_guard(lambda: self._accounts_data(user), title="Accounts", nav_active="accounts",
                                user=user, csrf=csrf)
        if data is None:
            return
        self._html(200, layout.render_accounts_page(user=user, csrf=csrf, nonce=self._nonce, data=data))

    def _h_requests(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, csrf = self._require_page_session()
        status = (query.get("status") or [""])[0] or None
        if status and status not in _ALLOWED_STATUS:
            status = None
        account_id = (query.get("account_id") or [""])[0] or None

        def build():
            return {"requests": _service().list_requests(user, status=status, account_id=account_id, limit=100)}

        data = self._page_guard(build, title="Requests", nav_active="requests", user=user, csrf=csrf)
        if data is None:
            return
        self._html(200, layout.render_requests_page(user=user, csrf=csrf, nonce=self._nonce, data=data))

    def _request_data(self, user: Dict[str, Any], rid: str) -> Dict[str, Any]:
        detail = _service().get_request(user, rid)
        payload = dict(detail)
        request = detail.get("request")
        payload["request"] = dict(request) if isinstance(request, dict) else dict(detail)
        payload["people"] = self._people(user)
        return payload

    @staticmethod
    def _people(user: Dict[str, Any]) -> Dict[str, str]:
        """id -> display name for audit/signature rows (any signed-in role may read)."""
        try:
            rows = _auth().list_users(user)
        except Exception:  # noqa: BLE001 - names are cosmetic, ids still render
            return {}
        return {
            str(person.get("id")): str(person.get("name") or person.get("email") or "")
            for person in rows
            if isinstance(person, dict)
        }

    def _h_request(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, csrf = self._require_page_session()
        rid = groups.get("rid") or ""
        data = self._page_guard(lambda: self._request_data(user, rid), title="Request",
                                nav_active="requests", user=user, csrf=csrf)
        if data is None:
            return
        flash = None
        if (query.get("error") or [""])[0]:
            flash = ("bad", _human_error((query.get("error") or [""])[0]))
        elif (query.get("ok") or [""])[0]:
            flash = ("ok", _human_ok((query.get("ok") or [""])[0]))
        data["flash"] = flash
        self._html(200, layout.render_request_page(user=user, csrf=csrf, nonce=self._nonce, data=data))

    def _h_request_action(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, csrf = self._require_page_session()
        form = self._read_form()
        if not self._check_csrf(form.get("csrf")):
            raise HttpError(403, "csrf_failed", "the form expired; reload the request and try again", page=True)
        rid = groups.get("rid") or ""
        action = groups.get("action") or ""
        back = f"/treasury/requests/{rid}"
        try:
            _apply_request_action(_service(), user, rid, action, form)
        except Exception as exc:  # noqa: BLE001
            treasury_error = _treasury_error_class()
            if treasury_error is not None and isinstance(exc, treasury_error):
                code = str(getattr(exc, "code", "error"))
                self._redirect(f"{back}?error={code}")
                return
            _ERROR_LOG.exception("request action %s failed", action)
            self._redirect(f"{back}?error=internal_error")
            return
        self._redirect(f"{back}?ok={action}")

    def _h_payslip(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, csrf = self._require_page_session()
        iid = groups.get("iid") or ""

        def build():
            slip = _statements().payslip(iid)
            request_id = str(slip.get("request_id") or "")
            audit: List[Dict[str, Any]] = []
            if request_id:
                try:
                    audit = _service().audit_rows(user, object_type="request", object_id=request_id, limit=10)
                except Exception:  # noqa: BLE001 - the slip is still readable
                    audit = []
            return {
                "payslip": slip,
                "audit": audit,
                "people": self._people(user),
                "request": {
                    "id": request_id,
                    "ref": slip.get("ref"),
                    "purpose": slip.get("purpose"),
                    "rate": slip.get("rate"),
                    "rate_source": slip.get("rate_source"),
                },
                "item": {
                    "payee_name": slip.get("recipient"),
                    "category": slip.get("category") or "—",
                    "usd_amount": slip.get("usd"),
                    "maza_amount": slip.get("maza"),
                    "txid": slip.get("txid"),
                    "date": slip.get("date"),
                    "status": slip.get("status"),
                },
            }

        data = self._page_guard(build, title="Payslip", nav_active="requests", user=user, csrf=csrf)
        if data is None:
            return
        self._html(200, layout.render_payslip_page(user=user, csrf=csrf, nonce=self._nonce, data=data))

    # -- handlers: sibling screens -------------------------------------------
    def _pool_data(self, user: Dict[str, Any]) -> Dict[str, Any]:
        svc = _service()
        return {
            "summary": svc.pool_summary(user),
            "stipends": svc.list_stipends(user, limit=50),
            "requests": svc.list_requests(user, limit=50),
            "can_donate": str(user.get("role") or "") in ("admin", "treasurer"),
        }

    def _h_pool(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, csrf = self._require_page_session()
        data = self._page_guard(lambda: self._pool_data(user), title="Pool", nav_active="pool",
                                user=user, csrf=csrf)
        if data is None:
            return
        self._html(200, _render_sibling("render_pool", user=user, csrf=csrf, nonce=self._nonce,
                                        nav_active="pool", title="Pool", data=data))

    def _h_statements(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, csrf = self._require_page_session()
        kind = (query.get("kind") or ["requests"])[0]
        month = (query.get("month") or [""])[0]
        data = self._page_guard(lambda: _statement_payload(kind, month), title="Statements",
                                nav_active="statements", user=user, csrf=csrf)
        if data is None:
            return
        self._html(200, _render_sibling("render_statements", user=user, csrf=csrf, nonce=self._nonce,
                                        nav_active="statements", title="Statements", data=data))

    def _h_audit(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, csrf = self._require_page_session()
        object_type = (query.get("object_type") or [""])[0] or None
        object_id = (query.get("object_id") or [""])[0] or None
        limit = _as_int((query.get("limit") or ["200"])[0], 200)

        def build():
            rows = _service().audit_rows(user, object_type=object_type, object_id=object_id, limit=limit)
            return {"rows": rows, "object_type": object_type or "", "object_id": object_id or "", "limit": limit}

        data = self._page_guard(build, title="Audit", nav_active="audit", user=user, csrf=csrf)
        if data is None:
            return
        self._html(200, _render_sibling("render_audit", user=user, csrf=csrf, nonce=self._nonce,
                                        nav_active="audit", title="Audit trail", data=data))

    def _h_users(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, csrf = self._require_page_session()
        if str(user.get("role") or "") != "admin":
            raise HttpError(403, "insufficient_role", "administrator role required", page=True)
        error_code = (query.get("error") or [""])[0]
        data = self._page_guard(lambda: {"users": _auth().list_users(user)}, title="Users",
                                nav_active="users", user=user, csrf=csrf)
        if data is None:
            return
        self._html(200, layout.render_users_page(user=user, csrf=csrf, nonce=self._nonce, data=data,
                                                 error=_human_error(error_code) if error_code else None))

    def _h_users_create(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, _csrf = self._require_page_session()
        if str(user.get("role") or "") != "admin":
            raise HttpError(403, "insufficient_role", "administrator role required", page=True)
        form = self._read_form()
        if not self._check_csrf(form.get("csrf")):
            raise HttpError(403, "csrf_failed", "the form expired; reload and try again", page=True)
        try:
            _auth().create_user(user, email=form.get("email") or "", name=form.get("name") or "",
                                role=form.get("role") or "")
        except Exception as exc:  # noqa: BLE001
            treasury_error = _treasury_error_class()
            if treasury_error is not None and isinstance(exc, treasury_error):
                self._redirect("/treasury/users?error=" + str(getattr(exc, "code", "error")))
                return
            _ERROR_LOG.exception("create user failed")
            self._redirect("/treasury/users?error=internal_error")
            return
        self._redirect("/treasury/users")

    def _h_user_deactivate(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user, _csrf = self._require_page_session()
        if str(user.get("role") or "") != "admin":
            raise HttpError(403, "insufficient_role", "administrator role required", page=True)
        form = self._read_form()
        if not self._check_csrf(form.get("csrf")):
            raise HttpError(403, "csrf_failed", "the form expired; reload and try again", page=True)
        uid = groups.get("uid") or ""
        try:
            _auth().deactivate_user(user, uid)
        except Exception as exc:  # noqa: BLE001
            treasury_error = _treasury_error_class()
            if treasury_error is not None and isinstance(exc, treasury_error):
                self._redirect("/treasury/users?error=" + str(getattr(exc, "code", "error")))
                return
            _ERROR_LOG.exception("deactivate user failed")
            self._redirect("/treasury/users?error=internal_error")
            return
        self._redirect("/treasury/users")

    # -- handlers: JSON API ---------------------------------------------------
    def _api_post_guard(self, body: Dict[str, Any]) -> None:
        """Session + double-submit CSRF for every JSON POST (contract §9)."""
        self._require_api_session()
        header = self.headers.get("X-CSRF-Token")
        if not self._check_csrf(header):
            raise HttpError(403, "csrf_failed", "missing or invalid X-CSRF-Token")

    def _a_me(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user = self._require_api_session()
        self._json(200, {"user": user, "csrf": self._csrf_cookie()})

    def _a_accounts(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user = self._require_api_session()
        self._json(200, {"accounts": _service().list_accounts(user)})

    def _a_requests(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user = self._require_api_session()
        status = (query.get("status") or [""])[0] or None
        account_id = (query.get("account_id") or [""])[0] or None
        limit = _as_int((query.get("limit") or ["100"])[0], 100)
        rows = _service().list_requests(user, status=status, account_id=account_id, limit=limit)
        self._json(200, {"requests": rows})

    def _a_request_create(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        body = self._read_json()
        self._api_post_guard(body)
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise HttpError(400, "invalid_request", "items must be a non-empty list")
        request = _service().create_request(
            self._require_api_session(),
            account_id=body.get("account_id") or "",
            kind=body.get("kind") or "",
            purpose=body.get("purpose") or "",
            items=items,
            award_ref=body.get("award_ref"),
            note=body.get("note"),
        )
        self._json(201, {"request": request})

    def _a_request(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user = self._require_api_session()
        detail = _service().get_request(user, groups.get("rid") or "")
        request = detail.get("request")
        if not isinstance(request, dict):
            request = {k: v for k, v in detail.items() if not isinstance(v, (list, dict))}
        self._json(
            200,
            {
                "request": request,
                "items": list(detail.get("items") or []),
                "signatures": list(detail.get("signatures") or []),
                "audit": list(detail.get("audit") or []),
            },
        )

    def _a_request_action(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        body = self._read_json()
        self._api_post_guard(body)
        user = self._require_api_session()
        rid = groups.get("rid") or ""
        action = groups.get("action") or ""
        request = _apply_request_action(_service(), user, rid, action, body)
        self._json(200, {"request": request})

    def _a_pool(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user = self._require_api_session()
        self._json(200, _service().pool_summary(user))

    def _a_donation(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        body = self._read_json()
        self._api_post_guard(body)
        donation = _service().record_donation(
            self._require_api_session(),
            donor_label=body.get("donor_label") or "",
            usd_amount=body.get("usd_amount") or "",
            txid=body.get("txid"),
            note=body.get("note"),
        )
        self._json(201, {"donation": donation})

    def _a_stipends(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user = self._require_api_session()
        limit = _as_int((query.get("limit") or ["100"])[0], 100)
        self._json(200, {"stipends": _service().list_stipends(user, limit=limit)})

    def _a_statements(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user = self._require_api_session()
        kind = (query.get("kind") or ["requests"])[0]
        month = (query.get("month") or [""])[0]
        fmt = ((query.get("format") or ["json"])[0] or "json").lower()
        payload = _statement_payload(kind, month)
        if fmt == "csv":
            csv_body = _statements().statement_csv(payload["rows"]).encode("utf-8")
            self._send(200, csv_body, "text/csv; charset=utf-8")
            return
        self._json(200, payload)

    def _a_audit(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user = self._require_api_session()
        object_type = (query.get("object_type") or [""])[0] or None
        object_id = (query.get("object_id") or [""])[0] or None
        limit = _as_int((query.get("limit") or ["200"])[0], 200)
        rows = _service().audit_rows(user, object_type=object_type, object_id=object_id, limit=limit)
        self._json(200, {"audit": rows})

    def _a_users(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        user = self._require_api_session()
        if str(user.get("role") or "") != "admin":
            raise HttpError(403, "insufficient_role", "administrator role required")
        self._json(200, {"users": _auth().list_users(user)})

    def _a_user_create(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        body = self._read_json()
        self._api_post_guard(body)
        user = self._require_api_session()
        if str(user.get("role") or "") != "admin":
            raise HttpError(403, "insufficient_role", "administrator role required")
        created = _auth().create_user(user, email=body.get("email") or "",
                                      name=body.get("name") or "", role=body.get("role") or "")
        self._json(201, {"user": created})

    def _a_user_deactivate(self, groups: Dict[str, str], query: Dict[str, List[str]]) -> None:
        body = self._read_json()
        self._api_post_guard(body)
        user = self._require_api_session()
        if str(user.get("role") or "") != "admin":
            raise HttpError(403, "insufficient_role", "administrator role required")
        updated = _auth().deactivate_user(user, groups.get("uid") or "")
        self._json(200, {"user": updated})


_UNSET = object()


# ------------------------------------------------------------------ helpers --
def _apply_request_action(svc: Any, user: Dict[str, Any], rid: str, action: str, body: Dict[str, Any]) -> Any:
    if action == "process":
        return svc.process_request(user, rid)
    if action == "submit":
        return svc.submit_request(user, rid)
    if action == "complete":
        return svc.complete_request(user, rid)
    if action == "cancel":
        return svc.cancel_request(user, rid, reason=body.get("reason"))
    if action == "sign":
        decision = str(body.get("decision") or "").strip().lower()
        if decision not in ("approve", "decline"):
            raise HttpError(400, "invalid_decision", "decision must be 'approve' or 'decline'")
        return svc.sign_request(user, rid, decision=decision, note=body.get("note"))
    raise HttpError(404, "not_found", f"unknown request action {action!r}")


def _statement_payload(kind: str, month: str) -> Dict[str, Any]:
    module = _statements()
    kind = (kind or "requests").strip().lower()
    if kind in ("request", "requests"):
        kind = "requests"
    elif kind in ("pool", "donation", "donations"):
        kind = "pool"
    else:
        raise HttpError(400, "invalid_kind", "kind must be 'requests' or 'pool'")
    try:
        month = module.resolve_month(month or None)
    except ValueError as exc:
        raise HttpError(400, "invalid_month", str(exc)) from None
    rows = module.statement_rows(kind=kind, month=month)
    usd = sum((_decimal_or_zero(r.get("usd")) for r in rows), Decimal("0"))
    maza = sum((_decimal_or_zero(r.get("maza")) for r in rows), Decimal("0"))
    return {
        "kind": kind,
        "month": month,
        "rows": rows,
        "totals": {"usd": f"{usd:.2f}", "maza": f"{maza:.2f}"},
    }


def _render_sibling(name: str, *, user, csrf: str, nonce: str, nav_active: str, title: str, data: Dict[str, Any]) -> str:
    """Call Lane 4's renderer; a missing/broken module yields a plain card."""
    try:
        pages = _pages()
        renderer = getattr(pages, name)
        document = renderer(user=user, csrf=csrf, nonce=nonce, data=data)
    except ImportError:
        return layout.render_unavailable(title=title, user=user, csrf=csrf, nonce=nonce, nav_active=nav_active)
    except AttributeError:
        return layout.render_unavailable(title=title, user=user, csrf=csrf, nonce=nonce, nav_active=nav_active)
    except Exception:  # noqa: BLE001 - a sibling bug must not take the router down
        _ERROR_LOG.exception("sibling renderer %s failed", name)
        return layout.render_error_page(title=title, user=user, csrf=csrf, nonce=nonce,
                                       nav_active=nav_active,
                                       message="This screen could not be rendered.")
    if not isinstance(document, str) or not document.strip():
        return layout.render_unavailable(title=title, user=user, csrf=csrf, nonce=nonce, nav_active=nav_active)
    return document


_OK_MESSAGES = {
    "process": "Request processed: the quote is locked.",
    "submit": "Submitted for signature.",
    "sign": "Signature recorded.",
    "complete": "Request completed.",
    "cancel": "Request cancelled.",
}

_ERROR_MESSAGES = {
    "insufficient_role": "Your role cannot perform that action.",
    "separation_of_duties": "You prepared or processed this request, so you cannot sign it.",
    "invalid_state": "That action is not available at this stage of the request.",
    "quote_expired": "The rate quote expired; process the request again.",
    "cap_per_request": "This request exceeds the per-request cap for the account.",
    "cap_daily": "This request exceeds today's cap for the account.",
    "insufficient_pool": "The pool balance does not cover this stipend run.",
    "federal_award_documentary_only": "Federal award funds are documentary only — nothing is broadcast.",
    "not_found": "That record no longer exists.",
    "csrf_failed": "Your form expired; reload and try again.",
    "internal_error": "Something went wrong on the treasury server.",
}


def _human_ok(code: str) -> str:
    return _OK_MESSAGES.get(code, "Action completed.")


def _human_error(code: str) -> str:
    return _ERROR_MESSAGES.get(code, "That action was refused (" + str(code) + ").")


# -------------------------------------------------------------------- entry --
def make_server(host: Optional[str] = None, port: Optional[int] = None) -> TreasuryHTTPServer:
    """Build (but do not start) the treasury server. Port 0 is fine for tests."""
    bind_host = host if host is not None else settings.HOST
    bind_port = settings.PORT if port is None else int(port)
    _ensure_db()
    return TreasuryHTTPServer((bind_host, bind_port), Handler)


def start(host: Optional[str] = None, port: Optional[int] = None) -> Tuple[TreasuryHTTPServer, threading.Thread]:
    """Start the server on a daemon thread and return it (tests use this)."""
    server = make_server(host, port)
    thread = threading.Thread(target=server.serve_forever, name="treasury-http", daemon=True)
    thread.start()
    return server, thread


def serve(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Blocking entrypoint used by ``python3 -m mazapay.treasury.server``."""
    server = make_server(host, port)
    _ACCESS_LOG.info("treasury listening on %s:%s", server.server_address[0], server.server_address[1])
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:  # pragma: no cover - operator path
        pass
    finally:
        server.server_close()
