from __future__ import annotations

import asyncio
import json
import struct
import time
from typing import Any

import httpx
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import function_signature_to_4byte_selector, keccak, to_checksum_address
from py_clob_client.signing.hmac import build_hmac_signature

from src.shared.logging import get_logger

log = get_logger(__name__)

# Polygon chain ID
_CHAIN_ID = 137

# Gnosis Safe factory + multisend (Polygon mainnet, from builder-relayer-client)
_SAFE_FACTORY = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"
_SAFE_MULTISEND = "0xA238CBeb142c10Ef7Ad8442C6D1f9E89e07e7761"
_SAFE_INIT_CODE_HASH = bytes.fromhex(
    "2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf"
)

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Gnosis Safe operation types
_OP_CALL = 0
_OP_DELEGATE_CALL = 1

# Terminal states returned by the relayer polling endpoint
_STATES_OK = {"STATE_MINED", "STATE_CONFIRMED"}
_STATE_FAILED = "STATE_FAILED"

# HTTP and polling settings
_HTTP_TIMEOUT = 15.0
_DEFAULT_MAX_POLLS = 100
_DEFAULT_POLL_INTERVAL = 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_safe(from_address: str) -> str:
    """Compute the expected Gnosis Safe address for an EOA using CREATE2.

    salt = keccak256(abi.encode(address))
    safe = CREATE2(SafeFactory, salt, SAFE_INIT_CODE_HASH)
    """
    salt = keccak(abi_encode(["address"], [from_address]))
    payload = b"\xff" + bytes.fromhex(_SAFE_FACTORY[2:]) + salt + _SAFE_INIT_CODE_HASH
    return to_checksum_address(keccak(payload)[12:])


def _encode_multisend_data(txns: list[dict[str, Any]]) -> tuple[str, str, int]:
    """Pack multiple transactions into a Safe multisend call.

    Each transaction is encodePacked as:
        uint8 operation + address to + uint256 value + uint256 dataLen + bytes data

    Returns (to_address, data_hex, operation).
    """
    packed = b""
    for txn in txns:
        to_bytes = bytes.fromhex(txn["to"][2:])  # 20 bytes, no padding
        data_bytes = bytes.fromhex(
            txn["data"][2:] if txn["data"].startswith("0x") else txn["data"]
        )
        value = int(txn.get("value", "0"))

        packed += struct.pack("B", _OP_CALL)       # uint8: 1 byte
        packed += to_bytes                          # address: 20 bytes
        packed += value.to_bytes(32, "big")         # uint256: 32 bytes
        packed += len(data_bytes).to_bytes(32, "big")  # uint256: 32 bytes
        packed += data_bytes                        # bytes: variable

    selector = function_signature_to_4byte_selector("multiSend(bytes)")
    call_data = "0x" + (selector + abi_encode(["bytes"], [packed])).hex()
    return _SAFE_MULTISEND, call_data, _OP_DELEGATE_CALL


_DOMAIN_TYPE_HASH = keccak(
    b"EIP712Domain(uint256 chainId,address verifyingContract)"
)
_SAFE_TX_TYPE_HASH = keccak(
    b"SafeTx(address to,uint256 value,bytes data,uint8 operation,"
    b"uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,"
    b"address gasToken,address refundReceiver,uint256 nonce)"
)


def _sign_safe_tx(
    private_key: str,
    safe_address: str,
    to: str,
    value: str,
    data: str,
    operation: int,
    nonce: int,
) -> str:
    """Sign a Gnosis Safe transaction using the eth_sign (v+4) format.

    Matches the @polymarket/builder-relayer-client TS SDK exactly:
    1. Compute EIP-712 hash manually
    2. Personal-sign the hash (adds \\x19Ethereum Signed Message:\\n32 prefix)
    3. Pack r+s+(v+4) — Gnosis Safe eth_sign mode (v=31/32)
    """
    data_bytes = bytes.fromhex(data[2:] if data.startswith("0x") else data)

    # Domain separator
    domain_hash = keccak(
        abi_encode(["bytes32", "uint256", "address"], [_DOMAIN_TYPE_HASH, _CHAIN_ID, safe_address])
    )

    # SafeTx struct hash
    struct_hash = keccak(
        abi_encode(
            [
                "bytes32", "address", "uint256", "bytes32", "uint8",
                "uint256", "uint256", "uint256", "address", "address", "uint256",
            ],
            [
                _SAFE_TX_TYPE_HASH, to, int(value), keccak(data_bytes), operation,
                0, 0, 0, _ZERO_ADDRESS, _ZERO_ADDRESS, nonce,
            ],
        )
    )

    # EIP-712 hash (= hashTypedData in viem)
    eip712_hash = keccak(b"\x19\x01" + domain_hash + struct_hash)

    # Personal-sign the EIP-712 hash, then adjust v → v+4 (Gnosis Safe eth_sign)
    signed = Account.sign_message(encode_defunct(primitive=eip712_hash), private_key=private_key)
    packed = signed.r.to_bytes(32, "big") + signed.s.to_bytes(32, "big") + bytes([signed.v + 4])
    return "0x" + packed.hex()


def _builder_headers(
    api_key: str,
    secret: str,
    passphrase: str,
    method: str,
    path: str,
    body: str,
) -> dict[str, str]:
    """Build POLY_BUILDER_* HMAC authentication headers."""
    timestamp = int(time.time())
    signature = build_hmac_signature(secret, timestamp, method, path, body)
    return {
        "POLY_BUILDER_API_KEY": api_key,
        "POLY_BUILDER_PASSPHRASE": passphrase,
        "POLY_BUILDER_SIGNATURE": signature,
        "POLY_BUILDER_TIMESTAMP": str(timestamp),
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class RelayerClient:
    """Async client for the Polymarket relayer (Gnosis Safe transaction type).

    Handles:
    - Safe address derivation from EOA private key
    - Multisend encoding for batched transactions
    - EIP-712 SafeTx signing
    - POLY_BUILDER_* HMAC authentication
    - Transaction submission and status polling
    """

    def __init__(
        self,
        relayer_url: str,
        private_key: str,
        builder_api_key: str,
        builder_secret: str,
        builder_passphrase: str,
        proxy_wallet: str = "",
    ) -> None:
        self._url = relayer_url.rstrip("/")
        self._private_key = private_key
        self._api_key = builder_api_key
        self._secret = builder_secret
        self._passphrase = builder_passphrase

        account = Account.from_key(private_key)
        self._from = account.address
        # Use the known proxy_wallet address when available;
        # fall back to CREATE2 derivation only if not provided.
        self._safe = proxy_wallet if proxy_wallet else _derive_safe(self._from)

        self._http = httpx.AsyncClient(base_url=self._url, timeout=_HTTP_TIMEOUT)
        log.info("relayer_client_ready", from_addr=self._from, safe=self._safe)

    async def is_safe_deployed(self) -> bool:
        resp = await self._http.get("/deployed", params={"address": self._safe})
        resp.raise_for_status()
        return bool(resp.json().get("deployed", False))

    async def _get_nonce(self) -> int:
        resp = await self._http.get(
            "/nonce", params={"address": self._from, "type": "SAFE"}
        )
        resp.raise_for_status()
        return int(resp.json()["nonce"])

    async def submit(
        self, transactions: list[dict[str, Any]], metadata: str = ""
    ) -> str:
        """Sign and submit a batch of transactions through the relayer.

        Each transaction is ``{to, data, value}``.
        Returns the relayer transaction ID.
        """
        if not transactions:
            raise ValueError("no transactions to submit")

        if len(transactions) == 1:
            to = transactions[0]["to"]
            data = transactions[0]["data"]
            value = str(transactions[0].get("value", "0"))
            operation = _OP_CALL
        else:
            to, data, operation = _encode_multisend_data(transactions)
            value = "0"

        nonce = await self._get_nonce()
        signature = _sign_safe_tx(
            private_key=self._private_key,
            safe_address=self._safe,
            to=to,
            value=value,
            data=data,
            operation=operation,
            nonce=nonce,
        )

        request: dict[str, Any] = {
            "from": self._from,
            "to": to,
            "proxyWallet": self._safe,
            "data": data,
            "nonce": str(nonce),  # relayer expects string, not integer
            "signature": signature,
            "signatureParams": {
                "gasPrice": "0",
                "operation": str(operation),
                "safeTxnGas": "0",
                "baseGas": "0",
                "gasToken": _ZERO_ADDRESS,
                "refundReceiver": _ZERO_ADDRESS,
            },
            "type": "SAFE",
            "metadata": metadata,
        }

        body_str = json.dumps(request, separators=(",", ":"))
        headers = _builder_headers(
            self._api_key, self._secret, self._passphrase, "POST", "/submit", body_str
        )

        log.debug("relayer_submit_request", body=request)
        resp = await self._http.post("/submit", content=body_str, headers=headers)
        if resp.status_code >= 400:
            log.error(
                "relayer_submit_error",
                status=resp.status_code,
                body=resp.text,
                request_from=self._from,
                request_safe=self._safe,
                request_nonce=nonce,
            )
        resp.raise_for_status()

        resp_data = resp.json()
        tx_id: str = resp_data["transactionID"]
        log.info("relayer_submitted", tx_id=tx_id, state=resp_data.get("state"))
        return tx_id

    async def wait_for_confirmation(
        self,
        tx_id: str,
        max_polls: int = _DEFAULT_MAX_POLLS,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
    ) -> dict[str, Any] | None:
        """Poll the relayer until the transaction reaches a terminal state.

        Returns the transaction dict on success (STATE_MINED / STATE_CONFIRMED),
        or ``None`` on failure (STATE_FAILED) or timeout.
        """
        for poll in range(max_polls):
            await asyncio.sleep(poll_interval)
            resp = await self._http.get("/transaction", params={"id": tx_id})
            resp.raise_for_status()
            txns = resp.json()

            if txns:
                txn = txns[0]
                state: str = txn.get("state", "")
                if state in _STATES_OK:
                    log.info(
                        "relayer_confirmed",
                        tx_id=tx_id,
                        state=state,
                        tx_hash=txn.get("transactionHash"),
                    )
                    return txn
                if state == _STATE_FAILED:
                    log.error(
                        "relayer_tx_failed",
                        tx_id=tx_id,
                        tx_hash=txn.get("transactionHash"),
                    )
                    return None
                log.debug("relayer_polling", tx_id=tx_id, state=state, poll=poll)

        log.warning("relayer_poll_timeout", tx_id=tx_id, max_polls=max_polls)
        return None

    async def close(self) -> None:
        await self._http.aclose()
