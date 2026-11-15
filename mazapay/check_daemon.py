"""check_daemon.py — plain-English MAZA daemon self-test for regular users.

Verifies that a maza wallet daemon can serve the MazaPay bridge, without
sending anything or needing an agent. Ships inside the bridge:

    python3 -m mazapay.check_daemon                    # auto-detect maza.conf
    python3 -m mazapay.check_daemon --conf ~/.maza/maza.conf
    python3 -m mazapay.check_daemon http://user:pass@host:12832
    python3 -m mazapay.check_daemon --json             # machine-readable

Checks (in order, stopping at the first blocker):
  1. daemon reachable          (TCP/HTTP to the RPC endpoint)
  2. RPC credentials accepted  (getblockchaininfo)
  3. wallet loaded             (getnewaddress — consumes one harmless address)
  4. receive tracking works    (getreceivedbyaddress)
  5. transaction history works (listtransactions)
  6. send surface present      (sendtoaddress probed with an INVALID amount —
                               verified WITHOUT sending anything)
  7. balance report            (warning only: vendor payments need MAZA in
                               this wallet; collections do not)

On success it prints the exact environment lines to paste into the bridge.
Secrets are never printed: the password is masked in every output line.
"""
import argparse
import base64
import configparser
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_PORT = 12832  # historic MazaCoin RPC port; maza.conf rpcport wins
MASKED = "***"


class CheckError(Exception):
    """A failed RPC call, bucketed for plain-language advice."""

    UNREACHABLE = "unreachable"
    AUTH = "auth"
    METHOD_MISSING = "method_missing"
    WALLET = "wallet"
    OTHER = "other"

    def __init__(self, message: str, category: str = "other"):
        super().__init__(message)
        self.category = category


def mask_url(url: str) -> str:
    """Mask the password with structured URL parsing — no regex guessing.

    Works for any credential content (including '@', '/', '#', '?') because
    urlsplit performs the same last-@ userinfo split the daemon ecosystem
    uses, and the host/port are taken from the parsed remainder.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host_port = parts.netloc.rsplit("@", 1)[-1]
    user = parts.username or ""
    netloc = f"{user}:***@{host_port}"
    return urllib.parse.urlunsplit(
        ("http", netloc, parts.path, parts.query, parts.fragment)
    )


def classify(exc: Exception) -> str:
    text = str(exc).lower()
    if "401" in text or "unauthorized" in text or "auth" in text:
        return CheckError.AUTH
    if "method not found" in text or "nosuchmethod" in text:
        return CheckError.METHOD_MISSING
    if "wallet" in text or "encrypted" in text or "locked" in text or "keypass" in text:
        return CheckError.WALLET
    if isinstance(exc, OSError) or "timed out" in text or "connection" in text or "refused" in text:
        return CheckError.UNREACHABLE
    return CheckError.OTHER


def make_caller(url: str, timeout: int = 10) -> Callable[[str, tuple], Any]:
    """Build an rpc(method, *params) function for one endpoint.

    Uses a proxy-free opener: daemon RPC is a direct local/private call and
    must never be routed through http_proxy environment settings.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(method: str, *params: Any) -> Any:
        payload = json.dumps(
            {"jsonrpc": "1.0", "id": "mazapay-check", "method": method, "params": list(params)}
        ).encode("utf-8")
        parts = urllib.parse.urlsplit(url)
        # credentials ride in the Authorization header — NEVER in the request
        # URL (Python's urllib resolves "user:pass@host" as a hostname and
        # fails with gaierror -2)
        host_port = parts.netloc.rsplit("@", 1)[-1]
        # urlsplit does NOT decode percent-escapes in userinfo; the URL was
        # built with quote(..., safe='') so decode here for the auth header
        user = urllib.parse.unquote(parts.username or "")
        password = urllib.parse.unquote(parts.password or "")
        raw = f"{user}:{password}".encode("utf-8")
        req = urllib.request.Request(
            urllib.parse.urlunsplit(("http", host_port, parts.path or "/", "", "")),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            },
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:
                pass
            exc.close()  # avoid ResourceWarning on the error response
            if exc.code in (401, 403):
                raise CheckError(
                    f"HTTP {exc.code}: credentials rejected {detail}".strip(),
                    CheckError.AUTH,
                ) from exc
            raise CheckError(
                f"HTTP {exc.code}: {detail or exc}", CheckError.OTHER
            ) from exc
        except OSError as exc:
            raise CheckError(f"cannot reach the daemon: {exc}", CheckError.UNREACHABLE) from exc
        except json.JSONDecodeError as exc:
            raise CheckError(f"daemon returned non-JSON: {exc}", CheckError.OTHER) from exc
        err = body.get("error")
        if err:
            raise CheckError(str(err), classify(str(err)))
        return body.get("result")

    return call


def run_checks(call: Callable[[str, tuple], Any]) -> Dict[str, Any]:
    """Run the check battery. Returns a machine-readable result dict."""
    results: List[Dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str, fix: str = "") -> None:
        results.append({"check": name, "ok": ok, "detail": detail, "fix": fix})

    # 1+2. reachability + credentials
    try:
        info = call("getblockchaininfo")
        record(
            "daemon_reachable_and_authenticated",
            True,
            f"chain='{info.get('chain', '?')}' blocks={info.get('blocks', '?')}",
        )
    except CheckError as exc:
        fixes = {
            CheckError.UNREACHABLE: "Is mazad running? Check the host/port, and that "
            "maza.conf has server=1.",
            CheckError.AUTH: "RPC rejected the credentials - re-check rpcuser and "
            "rpcpassword in maza.conf.",
            CheckError.METHOD_MISSING: "This daemon does not expose getblockchaininfo - "
            "the bridge needs a Bitcoin-style RPC (canonical v0.16.3).",
            CheckError.WALLET: "The daemon reported a wallet problem before answering - "
            "load/decrypt the wallet.",
            CheckError.OTHER: str(exc),
        }
        record("daemon_reachable_and_authenticated", False, str(exc), fixes.get(exc.category, str(exc)))
        return {"ready": False, "checks": results, "env": ""}

    # 3. wallet loaded (consumes one harmless address in their wallet)
    try:
        address = call("getnewaddress", "mazapay-check")
        record("wallet_loaded", True, f"fresh address ok ({address})",
               "This consumed one address in the wallet - harmless.")
    except CheckError as exc:
        fixes = {
            CheckError.WALLET: "The wallet is not loaded or is locked/encrypted. "
            "Unlock it for RPC, or create one: run mazad and create a wallet, "
            "then back up wallet.dat.",
            CheckError.METHOD_MISSING: "getnewaddress missing - the bridge needs a "
            "Bitcoin-style wallet RPC (canonical v0.16.3).",
            CheckError.OTHER: str(exc),
        }
        record("wallet_loaded", False, str(exc), fixes.get(exc.category, str(exc)))
        return {"ready": False, "checks": results, "env": ""}

    # 3b. address validation (the bridge calls this before every payment)
    try:
        va = call("validateaddress", address)
        is_valid = bool(va.get("isvalid")) if isinstance(va, dict) else False
        record(
            "validateaddress",
            is_valid,
            f"isvalid={va.get('isvalid')!r}" if isinstance(va, dict) else f"reply={va!r}",
            "" if is_valid else
            "validateaddress reported the fresh address invalid - the bridge "
            "would refuse payments against this daemon's addresses.",
        )
    except CheckError as exc:
        record("validateaddress", False, str(exc),
               "validateaddress missing/failed - the bridge calls it before "
               "every payment, so this must work.")

    # 4. receive tracking
    try:
        received = call("getreceivedbyaddress", address, 1)
        record("receive_tracking", True, f"getreceivedbyaddress ok ({received})")
    except CheckError as exc:
        record("receive_tracking", False, str(exc),
               "getreceivedbyaddress failed - collections would not be trackable.")

    # 5. transaction history
    try:
        txs = call("listtransactions", "*", 10, 0)
        record("transaction_history", True, f"listtransactions ok ({len(txs)} recent)")
    except CheckError as exc:
        record("transaction_history", False, str(exc),
               "listtransactions failed - reconcile/retry would be blind.")

    # 6. send surface present (probe with an INVALID amount - nothing is sent)
    try:
        call("sendtoaddress", "check-invalid-address", -1)
        record("send_capability", True, "sendtoaddress unexpectedly accepted bad args")
    except CheckError as exc:
        if exc.category == CheckError.METHOD_MISSING:
            record("send_capability", False, str(exc),
                   "sendtoaddress missing - vendor payments would not be possible.")
        else:
            # invalid amount/address/debit errors all prove the method exists
            record("send_capability", True,
                   "sendtoaddress present (rejected the invalid probe - nothing sent)")

    # 7. balance warning (non-blocking)
    try:
        balance = call("getbalance")
        if balance is None:
            raise CheckError("daemon returned null for getbalance", CheckError.OTHER)
        if float(balance) > 0:
            record("wallet_balance", True, f"balance={balance}")
        else:
            record("wallet_balance", True, "balance is 0",
                   "Collections work with a zero balance, but vendor payments need "
                   "MAZA in this wallet. Top it up before enabling disbursements.")
    except (CheckError, TypeError, ValueError) as exc:
        record("wallet_balance", True, f"getbalance unavailable ({exc}) - non-blocking")

    ready = all(r["ok"] for r in results)
    return {"ready": ready, "checks": results, "env": ""}


# --- maza.conf autodetection -------------------------------------------------

CONF_LOCATIONS = [
    os.path.expanduser("~/.maza/maza.conf"),
    os.path.expanduser("~/.mazacoin/mazacoin.conf"),
    os.path.expanduser("~/Library/Application Support/Maza/maza.conf"),
    os.path.expanduser("~/Library/Application Support/Mazacoin/mazacoin.conf"),
    os.path.expandvars(os.path.join(os.environ.get("APPDATA", "%APPDATA%"), "Maza", "maza.conf")),
]


def find_conf(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for path in CONF_LOCATIONS:
        if os.path.isfile(path):
            return path
    return None


def read_conf(path: str) -> Dict[str, str]:
    """Parse maza.conf (rpcuser/rpcpassword/rpcport lines; # comments)."""
    conf: Dict[str, str] = {}
    # interpolation=None: rpcpassword values may legitimately contain '%'
    # and default BasicInterpolation would raise InterpolationSyntaxError.
    parser = configparser.ConfigParser(strict=False, allow_no_value=True,
                                      interpolation=None)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            parser.read_string("[conf]\n" + fh.read())
        for key in ("rpcuser", "rpcpassword", "rpcport", "rpcbind", "rpcconnect"):
            val = parser.get("conf", key, fallback=None)
            if val:
                conf[key] = val.strip()
    except (OSError, configparser.Error):
        pass
    return conf


def build_url(host: str, port: str, user: str, password: str) -> str:
    """Build an RPC URL, percent-encoding credentials.

    Passwords may legitimately contain '/', '#', '?' or '@'; raw insertion
    would corrupt the URL (a '/' reads as the path start, '@' shifts the
    userinfo split). quote(..., safe='') encodes every reserved character so
    urlsplit on the receiving side reconstructs the exact credentials.
    """
    host = host or "127.0.0.1"
    port = port or str(DEFAULT_PORT)
    return "http://{}:{}@{}:{}".format(
        urllib.parse.quote(user or "", safe=""),
        urllib.parse.quote(password or "", safe=""),
        host,
        port,
    )


def prompt_for_url() -> Optional[str]:
    """Interactive path for regular users: ask, with sensible defaults."""
    print("No RPC URL given and no maza.conf found - answering a few questions:")
    try:
        host = input(f"Daemon host [127.0.0.1]: ").strip() or "127.0.0.1"
        port = input(f"RPC port [{DEFAULT_PORT}]: ").strip() or str(DEFAULT_PORT)
        user = input("RPC user: ").strip()
        password = getpass.getpass("RPC password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return None
    if not user or not password:
        print("RPC user and password are required (set them in maza.conf first).")
        return None
    return build_url(host, port, user, password)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m mazapay.check_daemon",
        description="Check that a MAZA daemon can serve the MazaPay bridge "
        "(no funds are moved).",
    )
    parser.add_argument("url", nargs="?", default="",
                        help="http://user:password@host:port (optional if maza.conf exists)")
    parser.add_argument("--conf", default=None, help="path to maza.conf")
    parser.add_argument("--json", action="store_true", help="emit JSON only "
                        "(configuration errors are JSON error objects)")
    parser.add_argument("--timeout", type=int, default=10, help="RPC timeout seconds")
    args = parser.parse_args(argv)

    url = args.url
    conf_used = None
    if not url:
        conf_path = find_conf(args.conf)
        if conf_path:
            conf_used = conf_path
            conf = read_conf(conf_path)
            if not conf.get("rpcuser") or not conf.get("rpcpassword"):
                msg = "maza.conf is missing rpcuser/rpcpassword - add them and enable server=1."
                if args.json:
                    print(json.dumps({"ready": False, "error": msg, "conf": conf_path}))
                else:
                    print(f"Using {conf_path}")
                    print(msg)
                return 2
            url = build_url(
                conf.get("rpcconnect", "127.0.0.1"),
                conf.get("rpcport", str(DEFAULT_PORT)),
                conf.get("rpcuser", ""),
                conf.get("rpcpassword", ""),
            )
        else:
            msg = "no RPC URL given and no maza.conf found"
            if args.json:
                print(json.dumps({"ready": False, "error": msg}))
                return 2
            url = prompt_for_url()
            if not url:
                return 2

    outcome = run_checks(make_caller(url, args.timeout))
    outcome["conf"] = conf_used
    outcome["url_masked"] = mask_url(url)

    if args.json:
        print(json.dumps(outcome, indent=2))
        return 0 if outcome["ready"] else 1

    if conf_used:
        print(f"Using {conf_used}")
    print(f"Checking daemon at {mask_url(url)}\n")
    for r in outcome["checks"]:
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"  [{mark}] {r['check']}: {r['detail']}")
        if not r["ok"] and r.get("fix"):
            print(f"         fix: {r['fix']}")
        elif r["ok"] and r.get("fix"):
            print(f"         note: {r['fix']}")
    print()
    if outcome["ready"]:
        parts = urllib.parse.urlsplit(url)
        host_port = parts.netloc.rsplit("@", 1)[-1]
        user = parts.username or ""
        print("READY - this daemon can serve the MazaPay bridge.")
        print("Bridge settings (the password is intentionally not displayed):")
        print(f"  MAZAPAY_MAZAD_RPC=http://{host_port}")
        if user:
            print(f"  MAZAPAY_MAZAD_RPC_USER={user}")
        print("  MAZAPAY_MAZAD_RPC_PASSWORD=<type your rpcpassword here>")
        return 0
    print("NOT READY - fix the FAIL items above and run again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
