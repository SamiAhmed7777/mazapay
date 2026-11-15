"""Chain adapter: maza wallet JSON-RPC client + an in-memory MockChain.

Canonical MAZA wallet: https://github.com/MazaCoin/maza (release v0.16.3) —
a direct fork of Bitcoin Core 0.16.3, so the wallet speaks standard
Bitcoin-style JSON-RPC: getnewaddress / getreceivedbyaddress /
sendtoaddress / listtransactions / validateaddress. Set rpcport in
maza.conf and point MAZAPAY_MAZAD_RPC at it.

MockChain implements the same surface in-memory so the whole bridge —
quoting, watching, confirming, exporting — runs end-to-end with no node.
Set MAZAPAY_MAZAD_RPC to switch the API server to a real wallet.
"""
import base64
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

from . import config


class ChainError(Exception):
    pass


class MazadRPC:
    """Thin Bitcoin-style JSON-RPC client for the maza wallet daemon."""

    def __init__(
        self,
        url: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        self.url = url or config.MAZAD_RPC_URL
        self.user = user if user is not None else config.MAZAD_RPC_USER
        self.password = password if password is not None else config.MAZAD_RPC_PASSWORD
        self.timeout = timeout or config.MAZAD_RPC_TIMEOUT

    def rpc(self, method: str, *params: Any) -> Any:
        payload = json.dumps(
            {"jsonrpc": "1.0", "id": "mazapay", "method": method, "params": list(params)}
        ).encode("utf-8")
        raw = f"{self.user}:{self.password}".encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except OSError as exc:
            raise ChainError(f"rpc {method} transport error: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ChainError(f"rpc {method} bad JSON: {exc}") from exc
        if body.get("error"):
            raise ChainError(f"rpc {method} error: {body['error']}")
        return body.get("result")

    # convenience wrappers ---------------------------------------------------
    def getnewaddress(self, account: str = "") -> str:
        return str(self.rpc("getnewaddress", account))

    def getreceivedbyaddress(self, address: str, minconf: int = 1) -> float:
        return float(self.rpc("getreceivedbyaddress", address, int(minconf)))

    def sendtoaddress(self, address: str, amount: float, comment: str = "") -> str:
        return str(self.rpc("sendtoaddress", address, float(amount), comment))

    def listtransactions(self, count: int = 200) -> List[Dict[str, Any]]:
        return list(self.rpc("listtransactions", "*", int(count), 0) or [])

    def validateaddress(self, address: str) -> Dict[str, Any]:
        return dict(self.rpc("validateaddress", address) or {})

    def getinfo(self) -> Dict[str, Any]:
        # Bitcoin Core 0.16 removed `getinfo`; prefer getblockchaininfo and
        # fall back for older daemons.
        try:
            return dict(self.rpc("getblockchaininfo") or {})
        except ChainError:
            return dict(self.rpc("getinfo") or {})


class MockChain:
    """In-memory chain double: same surface, deterministic behaviour.

    Transactions are records of (address, amount, confirmations). Tests and
    the /orders/{id}/simulate_payment demo endpoint call fund() to imitate
    an incoming transaction reaching N confirmations.
    """

    def __init__(self) -> None:
        self.txs: List[Dict[str, Any]] = []
        self._counter = 0
        self._fail_send_pending = 0
        self._fail_send_broadcast_first = True
        self._fail_scan_pending = 0
        self.last_scan_count: Optional[int] = None

    def fail_next_scan(self, times: int = 1) -> None:
        """Arm simulated listtransactions failures (RPC unavailable)."""
        self._fail_scan_pending = int(times)

    def fail_next_send(self, times: int = 1, broadcast_first: bool = True) -> None:
        """Arm simulated sendtoaddress failures for tests.

        broadcast_first=True models an RPC timeout AFTER the transaction was
        relayed (the response was lost — the send may be on chain): the tx
        is recorded internally before raising, so reconcile can find it.
        broadcast_first=False models a pre-broadcast failure (nothing sent).
        """
        self._fail_send_pending = int(times)
        self._fail_send_broadcast_first = bool(broadcast_first)

    # -- surface shared with MazadRPC ----------------------------------------
    def getnewaddress(self, account: str = "") -> str:
        self._counter += 1
        return f"tMAZA-MOCK-{account or 'x'}-{uuid.uuid4().hex[:10]}"

    def getreceivedbyaddress(self, address: str, minconf: int = 1) -> float:
        return sum(
            tx["amount"]
            for tx in self.txs
            if tx["address"] == address and tx["confirmations"] >= minconf
        )

    def sendtoaddress(self, address: str, amount: float, comment: str = "") -> str:
        if self._fail_send_pending > 0:
            self._fail_send_pending -= 1
            if self._fail_send_broadcast_first:
                self._counter += 1
                txid = f"mock{self._counter:06d}{uuid.uuid4().hex[:10]}"
                self.txs.append(
                    {
                        "txid": txid,
                        "address": address,
                        "amount": -abs(float(amount)),  # wallet convention: outflow negative
                        "category": "send",
                        "confirmations": 1,
                        "comment": comment,
                    }
                )
                raise ChainError(
                    "rpc sendtoaddress transport error: timeout after broadcast (simulated)"
                )
            raise ChainError("rpc sendtoaddress error: pre-broadcast failure (simulated)")
        self._counter += 1
        txid = f"mock{self._counter:06d}{uuid.uuid4().hex[:10]}"
        self.txs.append(
            {
                "txid": txid,
                "address": address,
                "amount": -abs(float(amount)),  # wallet convention: outflow negative
                "category": "send",
                "confirmations": 1,
                "comment": comment,
            }
        )
        return txid

    def listtransactions(self, count: int = 200) -> List[Dict[str, Any]]:
        if self._fail_scan_pending > 0:
            self._fail_scan_pending -= 1
            raise ChainError("rpc listtransactions error: scan unavailable (simulated)")
        self.last_scan_count = int(count)
        return self.txs[-count:]

    def validateaddress(self, address: str) -> Dict[str, Any]:
        ok = bool(address) and (address.startswith("tMAZA") or address.startswith("M"))
        return {"isvalid": bool(ok), "address": address}

    def getinfo(self) -> Dict[str, Any]:
        return {"chain": "mock", "blocks": 0, "connections": 0}

    # -- test/demo helpers ----------------------------------------------------
    def fund(self, address: str, amount: float, confirmations: int) -> str:
        self._counter += 1
        txid = f"mock{self._counter:06d}{uuid.uuid4().hex[:10]}"
        self.txs.append(
            {
                "txid": txid,
                "address": address,
                "amount": abs(float(amount)),
                "category": "receive",
                "confirmations": int(confirmations),
            }
        )
        return txid


_MOCK: Optional[MockChain] = None
_RPC: Optional[MazadRPC] = None


def get_chain() -> Tuple[Any, str]:
    """Return (chain, mode) where mode is 'rpc' or 'mock'."""
    global _MOCK, _RPC
    if config.MAZAD_RPC_URL:
        if _RPC is None:
            _RPC = MazadRPC()
        return _RPC, "rpc"
    if _MOCK is None:
        _MOCK = MockChain()
    return _MOCK, "mock"


def is_mock(chain: Any) -> bool:
    return isinstance(chain, MockChain)
