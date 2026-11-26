"""Pluggable external anchoring of a crate's hash-chain head.

Providers
  mock     – deterministic local receipts (no network). Clearly flagged is_mock=True.
  ipfs     – pins the anchor document to an IPFS node via its HTTP RPC API.  Needs IPFS_API_URL (+ IPFS_API_AUTH for hosted nodes).
  polygon  – writes `TITAN1 || head_hash [|| cid]` as calldata of a 0-value self-transaction on Polygon PoS.
             Needs `pip install web3`, POLYGON_RPC_URL, POLYGON_CHAIN_ID (137 mainnet / 80002 Amoy) and a
             funded POLYGON_PRIVATE_KEY.  If IPFS_API_URL is also set, the doc is pinned first and the CID is embedded.

The live providers are implemented but cannot be exercised without those credentials.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .config import settings
from .models import AnchorReceipt, Crate
from .util import TitanError, iso, utcnow

MAGIC = b"TITAN1"


@dataclass
class Receipt:
    provider: str
    network: Optional[str] = None
    ipfs_cid: Optional[str] = None
    tx_hash: Optional[str] = None
    is_mock: bool = False


class AnchorProvider(ABC):
    name: str

    @abstractmethod
    def anchor(self, doc: dict) -> Receipt:
        """Publish `doc` (schema titan.anchor.v1) and return a receipt."""

    @abstractmethod
    def verify(self, receipt: Receipt, doc: dict) -> bool:
        """Independently confirm the receipt still matches `doc`."""


class MockAnchorProvider(AnchorProvider):
    name = "mock"

    @staticmethod
    def _digest(doc: dict, salt: str) -> str:
        return hashlib.sha256((audit.canonical_json(doc) + salt).encode()).hexdigest()

    def anchor(self, doc: dict) -> Receipt:
        return Receipt("mock", "local-mock", f"mock-cid-{self._digest(doc, 'cid')[:46]}",
                       "0x" + self._digest(doc, "tx"), True)

    def verify(self, receipt: Receipt, doc: dict) -> bool:
        return receipt.tx_hash == "0x" + self._digest(doc, "tx")


class IPFSAnchorProvider(AnchorProvider):
    name = "ipfs"

    def __init__(self, api_url: str, auth: str = ""):
        if not api_url:
            raise TitanError(503, "IPFS_API_URL is not configured")
        self.api_url, self.auth = api_url.rstrip("/"), auth

    def _headers(self) -> dict:
        return {"Authorization": self.auth} if self.auth else {}

    def anchor(self, doc: dict) -> Receipt:
        import requests
        body = audit.canonical_json(doc).encode()
        r = requests.post(f"{self.api_url}/api/v0/add", params={"pin": "true", "cid-version": 1},
                          files={"file": ("anchor.json", body)}, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return Receipt("ipfs", "ipfs", ipfs_cid=r.json()["Hash"])

    def verify(self, receipt: Receipt, doc: dict) -> bool:
        import requests
        r = requests.post(f"{self.api_url}/api/v0/cat", params={"arg": receipt.ipfs_cid},
                          headers=self._headers(), timeout=30)
        return r.ok and r.content == audit.canonical_json(doc).encode()


class PolygonAnchorProvider(AnchorProvider):
    name = "polygon"

    def __init__(self, rpc_url: str, private_key: str, chain_id: int, ipfs: Optional[IPFSAnchorProvider] = None):
        if not (rpc_url and private_key):
            raise TitanError(503, "POLYGON_RPC_URL and POLYGON_PRIVATE_KEY are required for polygon anchoring")
        self.rpc_url, self.private_key, self.chain_id, self.ipfs = rpc_url, private_key, chain_id, ipfs

    def _w3(self):
        try:
            from web3 import Web3
        except ImportError as e:  # pragma: no cover
            raise TitanError(503, "web3 is not installed (pip install web3)") from e
        return Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 30}))

    @staticmethod
    def _calldata(doc: dict, cid: Optional[str]) -> bytes:
        return MAGIC + bytes.fromhex(doc["head_hash"]) + (cid.encode() if cid else b"")

    def anchor(self, doc: dict) -> Receipt:
        w3 = self._w3()
        acct = w3.eth.account.from_key(self.private_key)
        cid = self.ipfs.anchor(doc).ipfs_cid if self.ipfs else None
        base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
        tip = max(w3.eth.max_priority_fee, w3.to_wei(30, "gwei"))  # Polygon enforces a ~25-30 gwei minimum tip
        tx = {"type": 2, "chainId": self.chain_id, "from": acct.address, "to": acct.address, "value": 0,
              "data": self._calldata(doc, cid), "nonce": w3.eth.get_transaction_count(acct.address),
              "maxPriorityFeePerGas": tip, "maxFeePerGas": base_fee * 2 + tip}
        tx["gas"] = w3.eth.estimate_gas(tx)
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction  # web3 v7 / v6
        tx_hash = w3.eth.send_raw_transaction(raw)
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if rcpt["status"] != 1:
            raise TitanError(502, f"Polygon anchor transaction reverted: {tx_hash.hex()}")
        h = tx_hash.hex()
        return Receipt("polygon", f"polygon-{self.chain_id}", cid, h if h.startswith("0x") else "0x" + h)

    def verify(self, receipt: Receipt, doc: dict) -> bool:
        w3 = self._w3()
        data = bytes(w3.eth.get_transaction(receipt.tx_hash)["input"])
        return data.startswith(MAGIC + bytes.fromhex(doc["head_hash"]))


def get_provider(name: Optional[str] = None) -> AnchorProvider:
    name = (name or settings.anchor_backend).lower()
    if name == "mock":
        return MockAnchorProvider()
    if name == "ipfs":
        return IPFSAnchorProvider(settings.ipfs_api_url, settings.ipfs_api_auth)
    if name == "polygon":
        ipfs = IPFSAnchorProvider(settings.ipfs_api_url, settings.ipfs_api_auth) if settings.ipfs_api_url else None
        return PolygonAnchorProvider(settings.polygon_rpc_url, settings.polygon_private_key,
                                     settings.polygon_chain_id, ipfs)
    raise TitanError(400, f"Unknown ANCHOR_BACKEND '{name}'")


def anchor_crate(db: Session, crate_id: int, provider: Optional[AnchorProvider] = None) -> AnchorReceipt:
    """Anchor the current chain head. Refuses to anchor an already-broken chain."""
    crate = db.scalars(select(Crate).where(Crate.crate_id == crate_id).with_for_update()).first()
    if crate is None:
        raise TitanError(404, f"Crate {crate_id} not found")
    chain = audit.verify_chain(db, crate_id)
    if chain.length == 0:
        raise TitanError(409, "Crate has no audit records to anchor")
    if not chain.valid:
        raise TitanError(409, f"Refusing to anchor a broken chain (#{chain.broken_at_seq}: {chain.reason})")

    now = utcnow()
    doc = {"schema": "titan.anchor.v1", "crate_id": crate_id, "chain_length": chain.length,
           "head_hash": chain.head_hash, "anchored_at": iso(now)}
    provider = provider or get_provider()
    try:
        rc = provider.anchor(doc)
    except TitanError:
        raise
    except Exception as e:  # network / RPC failures
        raise TitanError(502, f"Anchoring via '{provider.name}' failed: {e}") from e

    row = AnchorReceipt(crate_id=crate_id, chain_length=chain.length, head_hash=chain.head_hash,
                        provider=rc.provider, network=rc.network, ipfs_cid=rc.ipfs_cid,
                        tx_hash=rc.tx_hash, is_mock=rc.is_mock, anchored_at=now)
    db.add(row)
    db.flush()
    audit.append_event(db, crate_id=crate_id, event_type=audit.ANCHORED, dispatch_id=crate.current_dispatch_id,
                       payload={"anchor_id": row.anchor_id, "chain_length": chain.length, "head_hash": chain.head_hash,
                                "provider": rc.provider, "network": rc.network, "ipfs_cid": rc.ipfs_cid,
                                "tx_hash": rc.tx_hash, "is_mock": rc.is_mock})
    return row
