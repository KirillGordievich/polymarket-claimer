from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import BaseModel, model_validator

from src.infrastructure.notifications.telegram import TelegramNotifier
from src.infrastructure.polymarket.relayer_client import RelayerClient
from src.shared.logging import get_logger, setup_logging
from src.shared.settings import Settings

log = get_logger(__name__)

_DATA_API = "https://data-api.polymarket.com"

# Polygon contract addresses
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
_NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
# Polymarket deposit contract — receives approve+deposit batch for pending deposits
_POLY_DEPOSIT_CONTRACT = "0x93070a847efef7f70739046a929d47a521f5b8ee"
_ZERO_BYTES32 = b"\x00" * 32
_MAX_UINT256 = 2**256 - 1

# balanceOf(address) / allowance(address,address) selectors
_BALANCE_OF_SELECTOR = "0x70a08231"
_ALLOWANCE_SELECTOR = "0xdd62ed3e"
# Selector of the deposit function on _POLY_DEPOSIT_CONTRACT: deposit(address,address,uint256)
_POLY_DEPOSIT_SELECTOR = bytes.fromhex("62355638")

# USDC has 6 decimal places
_USDC_DECIMALS = 1_000_000
# Standard CTF outcome partition indices passed to redeemPositions
_CTF_PARTITION = [1, 2]

# HTTP timeouts (seconds)
_DATA_API_TIMEOUT = 15.0
_ETH_CALL_TIMEOUT = 10.0

# Positions fetch parameters
_POSITIONS_PAGE_SIZE = 100
_POSITIONS_SIZE_THRESHOLD = 0.1

# On error, retry the claim cycle this many times before waiting for the next interval
_MAX_CLAIM_RETRIES = 3


class Position(BaseModel):
    """A redeemable Polymarket position as returned by the data-api."""

    conditionId: str
    size: float
    negativeRisk: bool = False
    outcomeIndex: int = 0
    title: str = ""
    outcome: str = ""
    initialValue: float = 0.0
    currentValue: float = 0.0
    avgPrice: float = 0.0
    price: float = 0.0

    @model_validator(mode="after")
    def _fill_initial_value(self) -> Position:
        """API sometimes returns initialValue=0; fall back to size * avgPrice."""
        if not self.initialValue and self.size and self.avgPrice:
            self.initialValue = self.size * self.avgPrice
        return self


async def _eth_call(rpc_url: str, to: str, data: str) -> str:
    """Execute an eth_call and return the raw hex result."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"data": data, "to": to}, "latest"],
    }
    async with httpx.AsyncClient(timeout=_ETH_CALL_TIMEOUT) as client:
        resp = await client.post(rpc_url, json=payload)
        resp.raise_for_status()
        return resp.json()["result"]


async def _fetch_usdc_balance(rpc_url: str, wallet_address: str) -> float:
    """Return USDC balance of wallet_address on Polygon (6 decimals)."""
    addr = wallet_address.lower().removeprefix("0x").zfill(64)
    result = await _eth_call(rpc_url, _USDC_ADDRESS, f"{_BALANCE_OF_SELECTOR}{addr}")
    return int(result, 16) / _USDC_DECIMALS


async def _fetch_usdc_allowance(rpc_url: str, owner: str, spender: str) -> int:
    """Return USDC allowance from owner to spender (raw, 6 decimals)."""
    owner_pad = owner.lower().removeprefix("0x").zfill(64)
    spender_pad = spender.lower().removeprefix("0x").zfill(64)
    result = await _eth_call(rpc_url, _USDC_ADDRESS, f"{_ALLOWANCE_SELECTOR}{owner_pad}{spender_pad}")
    return int(result, 16)


def _build_deposit_transactions(amount_raw: int, proxy_wallet: str) -> list[dict[str, Any]]:
    """Build the 2-transaction batch for confirming a pending USDC deposit.

    Matches exactly what the Polymarket UI sends:
    1. USDC.approve(DepositContract, amount)  — exact amount, not MAX_UINT256
    2. DepositContract.0x62355638(USDC, proxyWallet, amount)
    """
    from eth_abi import encode as abi_encode
    from eth_utils import function_signature_to_4byte_selector

    approve_sel = function_signature_to_4byte_selector("approve(address,uint256)")
    approve_data = "0x" + (
        approve_sel + abi_encode(["address", "uint256"], [_POLY_DEPOSIT_CONTRACT, amount_raw])
    ).hex()

    deposit_data = "0x" + (
        _POLY_DEPOSIT_SELECTOR
        + abi_encode(
            ["address", "address", "uint256"],
            [_USDC_ADDRESS, proxy_wallet, amount_raw],
        )
    ).hex()

    return [
        {"to": _USDC_ADDRESS, "data": approve_data, "value": "0"},
        {"to": _POLY_DEPOSIT_CONTRACT, "data": deposit_data, "value": "0"},
    ]


def _build_activation_transactions() -> list[dict[str, Any]]:
    """Build the 4 approval transactions needed to activate a proxy wallet for trading.

    1. USDC.approve(Exchange, MAX_UINT256)
    2. CTF.setApprovalForAll(Exchange, true)
    3. USDC.approve(NegRiskExchange, MAX_UINT256)
    4. CTF.setApprovalForAll(NegRiskExchange, true)
    """
    from eth_abi import encode as abi_encode
    from eth_utils import function_signature_to_4byte_selector

    approve_sel = function_signature_to_4byte_selector("approve(address,uint256)")
    set_approval_sel = function_signature_to_4byte_selector("setApprovalForAll(address,bool)")

    def approve_data(spender: str) -> str:
        return "0x" + (approve_sel + abi_encode(["address", "uint256"], [spender, _MAX_UINT256])).hex()

    def set_approval_data(operator: str) -> str:
        return "0x" + (set_approval_sel + abi_encode(["address", "bool"], [operator, True])).hex()

    return [
        {"to": _USDC_ADDRESS, "data": approve_data(_EXCHANGE), "value": "0"},
        {"to": _CTF_ADDRESS, "data": set_approval_data(_EXCHANGE), "value": "0"},
        {"to": _USDC_ADDRESS, "data": approve_data(_NEG_RISK_EXCHANGE), "value": "0"},
        {"to": _CTF_ADDRESS, "data": set_approval_data(_NEG_RISK_EXCHANGE), "value": "0"},
    ]


def _build_ctf_redeem_data(condition_id: str) -> str:
    """ABI-encode redeemPositions(USDC, 0x0, conditionId, [1, 2]) for CTF contract."""
    from eth_abi import encode as abi_encode
    from eth_utils import function_signature_to_4byte_selector

    selector = function_signature_to_4byte_selector(
        "redeemPositions(address,bytes32,bytes32,uint256[])"
    )
    cid = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)
    args = abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [_USDC_ADDRESS, _ZERO_BYTES32, cid, _CTF_PARTITION],
    )
    return "0x" + (selector + args).hex()


def _build_neg_risk_redeem_data(condition_id: str, outcome_index: int, size: float) -> str:
    """ABI-encode redeemPositions(conditionId, amounts) for NegRisk adapter."""
    from eth_abi import encode as abi_encode
    from eth_utils import function_signature_to_4byte_selector

    selector = function_signature_to_4byte_selector("redeemPositions(bytes32,uint256[])")
    cid = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)
    amount = int(size * _USDC_DECIMALS)
    amounts = [amount, 0] if outcome_index == 0 else [0, amount]
    args = abi_encode(["bytes32", "uint256[]"], [cid, amounts])
    return "0x" + (selector + args).hex()


def _build_transactions(positions: list[Position]) -> list[dict[str, Any]]:
    """Build redeem transactions from redeemable positions.

    Deduplicates by conditionId (first occurrence wins for CTF;
    for NegRisk each position is its own transaction since amounts differ).
    """
    transactions: list[dict[str, Any]] = []
    seen_ctf: set[str] = set()

    for pos in positions:
        if pos.negativeRisk:
            # NegRisk: one transaction per position (amounts vary)
            data = _build_neg_risk_redeem_data(pos.conditionId, pos.outcomeIndex, pos.size)
            transactions.append({"to": _NEG_RISK_ADAPTER, "data": data, "value": "0"})
        else:
            # Standard CTF: one transaction per conditionId (deduplicate)
            if pos.conditionId in seen_ctf:
                continue
            seen_ctf.add(pos.conditionId)
            data = _build_ctf_redeem_data(pos.conditionId)
            transactions.append({"to": _CTF_ADDRESS, "data": data, "value": "0"})

    return transactions


def _format_claim_message(positions: list[Position]) -> str:
    """Build the Telegram HTML notification for a completed claim."""
    total_spend = sum(p.initialValue for p in positions)
    total_reward = sum(p.currentValue for p in positions)
    total_profit = total_reward - total_spend

    lines = [
        f"• {p.title} — {p.outcome} — +${p.currentValue - p.initialValue:.2f}"
        for p in positions
    ]

    parts = [
        "Claim completed",
        "",
        f"Positions: {len(positions)}",
        f"Profit: <b>${total_profit:.2f}</b> (${total_spend:.2f} \u2192 ${total_reward:.2f})",
    ]
    if lines:
        parts += ["", *lines]

    return "\n".join(parts)


class ClaimProcess:
    """Background process that redeems resolved Polymarket positions every N seconds.

    In dry_run mode fetches positions and logs them but does not submit any transactions.
    """

    def __init__(self, settings: Settings, notifier: TelegramNotifier) -> None:
        self._settings = settings
        self._notifier = notifier
        self._data_api = httpx.AsyncClient(base_url=_DATA_API, timeout=_DATA_API_TIMEOUT)
        self._relayer: RelayerClient | None = None
        if not settings.dry_run:
            self._relayer = RelayerClient(
                relayer_url=settings.relayer_url,
                private_key=settings.private_key,
                builder_api_key=settings.builder_api_key,
                builder_secret=settings.builder_secret,
                builder_passphrase=settings.builder_passphrase,
                proxy_wallet=settings.proxy_wallet,
            )

    async def run(self) -> None:
        """Main loop: claim once (with retries), then sleep claim_interval_sec, repeat."""
        log.info("claim_process_started", interval_sec=self._settings.claim_interval_sec)
        while True:
            for attempt in range(1, _MAX_CLAIM_RETRIES + 1):
                try:
                    await self._claim_once()
                    break
                except Exception:
                    log.exception("claim_cycle_error", attempt=attempt, max_retries=_MAX_CLAIM_RETRIES)
                    if attempt == _MAX_CLAIM_RETRIES:
                        await self._notifier.send(
                            f"Claim failed after {_MAX_CLAIM_RETRIES} attempts, waiting for next cycle"
                        )
            await asyncio.sleep(self._settings.claim_interval_sec)

    async def _fetch_positions(self) -> list[Position]:
        """Fetch all redeemable positions from Polymarket data-api (paginated)."""
        all_positions: list[Position] = []
        offset = 0
        while True:
            resp = await self._data_api.get(
                "/positions",
                params={
                    "user": self._settings.proxy_wallet,
                    "redeemable": "true",
                    "sizeThreshold": str(_POSITIONS_SIZE_THRESHOLD),
                    "limit": str(_POSITIONS_PAGE_SIZE),
                    "offset": str(offset),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # API may return a list directly or {data: [...]}
            raw = data if isinstance(data, list) else data.get("data", [])
            all_positions.extend(Position.model_validate(p) for p in raw)
            if len(raw) < _POSITIONS_PAGE_SIZE:
                break
            offset += _POSITIONS_PAGE_SIZE
        return all_positions

    async def _check_pending_deposit(self) -> None:
        """Detect pending USDC deposit, activate the wallet if needed, and confirm the deposit."""
        try:
            balance = await _fetch_usdc_balance(self._settings.rpc_url, self._settings.proxy_wallet)
        except Exception as exc:
            log.warning("pending_deposit_check_error", error=type(exc).__name__, detail=str(exc))
            return

        if balance <= 0:
            return

        log.warning("pending_deposit_detected", usdc=round(balance, 2), wallet=self._settings.proxy_wallet)

        if self._settings.dry_run or self._relayer is None:
            await self._notifier.send(
                f"Pending deposit: <b>${balance:.2f} USDC</b> on proxy wallet (dry-run, skipping activation)"
            )
            return

        await self._activate_wallet(balance)
        await self._confirm_deposit(balance)

    async def _activate_wallet(self, deposit_usdc: float) -> None:
        """Submit activation transactions if the wallet is not yet approved for trading."""
        try:
            allowance = await _fetch_usdc_allowance(
                self._settings.rpc_url, self._settings.proxy_wallet, _EXCHANGE
            )
        except Exception as exc:
            log.warning("activation_allowance_check_error", error=type(exc).__name__, detail=str(exc))
            return

        if allowance > 0:
            log.debug("wallet_already_activated", allowance=allowance)
            return

        deployed = await self._relayer.is_safe_deployed()
        if not deployed:
            log.error("activation_safe_not_deployed", safe=self._relayer._safe)
            await self._notifier.send(
                f"Pending deposit <b>${deposit_usdc:.2f} USDC</b>: Safe not deployed, activation skipped"
            )
            return

        log.info("wallet_activation_starting", usdc=round(deposit_usdc, 2))
        transactions = _build_activation_transactions()

        try:
            tx_id = await self._relayer.submit(transactions, metadata="Wallet activation")
            result = await self._relayer.wait_for_confirmation(tx_id)
        except Exception as exc:
            log.error("wallet_activation_submit_error", error=type(exc).__name__, detail=str(exc))
            await self._notifier.send(f"Wallet activation failed: {type(exc).__name__}")
            return

        if result:
            tx_hash = result.get("transactionHash")
            log.info("wallet_activation_success", tx_hash=tx_hash, usdc=round(deposit_usdc, 2))
            await self._notifier.send(
                f"Wallet activated!\n\n"
                f"Deposit: <b>${deposit_usdc:.2f} USDC</b>\n"
                f"Tx: <code>{tx_hash}</code>"
            )
        else:
            log.error("wallet_activation_failed", tx_id=tx_id)
            await self._notifier.send("Wallet activation transaction failed")

    async def _confirm_deposit(self, deposit_usdc: float) -> None:
        """Submit the deposit batch to move USDC from proxy wallet into the exchange."""
        amount_raw = round(deposit_usdc * _USDC_DECIMALS)
        txns = _build_deposit_transactions(amount_raw, self._settings.proxy_wallet)

        log.info("deposit_confirmation_starting", usdc=round(deposit_usdc, 2))
        try:
            tx_id = await self._relayer.submit(txns, metadata="Confirm deposit")
            result = await self._relayer.wait_for_confirmation(tx_id)
        except Exception as exc:
            log.error("deposit_confirmation_submit_error", error=type(exc).__name__, detail=str(exc))
            await self._notifier.send(f"Deposit confirmation failed: {type(exc).__name__}")
            return

        if result:
            tx_hash = result.get("transactionHash")
            log.info("deposit_confirmed", tx_hash=tx_hash, usdc=round(deposit_usdc, 2))
            # await self._notifier.send(f"Deposit confirmed: <b>${deposit_usdc:.2f} USDC</b>")
        else:
            log.error("deposit_confirmation_failed", tx_id=tx_id)
            await self._notifier.send("Deposit confirmation transaction failed")

    async def _redeem_positions(self) -> list[Position] | None:
        """Fetch and redeem resolved positions. Returns the positions on success, else None."""
        positions = await self._fetch_positions()

        if not positions:
            log.debug("claim_no_positions")
            return None

        log.info("claim_positions_found", count=len(positions))
        for pos in positions:
            log.debug(
                "claim_position_raw",
                title=pos.title,
                initialValue=pos.initialValue,
                currentValue=pos.currentValue,
                size=pos.size,
                avgPrice=pos.avgPrice,
                price=pos.price,
            )

        if self._settings.dry_run:
            for pos in positions:
                log.info(
                    "claim_dry_run_position",
                    title=pos.title,
                    outcome=pos.outcome,
                    size=pos.size,
                    profit=pos.currentValue - pos.initialValue,
                )
            return None

        transactions = _build_transactions(positions)
        if not transactions:
            return None

        assert self._relayer is not None

        deployed = await self._relayer.is_safe_deployed()
        if not deployed:
            log.error("claim_safe_not_deployed", safe=self._relayer._safe)
            return None

        tx_id = await self._relayer.submit(transactions, metadata="Batch redeem positions")
        result = await self._relayer.wait_for_confirmation(tx_id)

        if result:
            tx_hash = result.get("transactionHash")
            log.info("claim_success", count=len(positions), tx_hash=tx_hash)
            await self._notifier.send(_format_claim_message(positions))
            return positions
        else:
            log.error("claim_failed", tx_id=tx_id)
            return None

    async def _claim_once(self) -> None:
        # 1. Redeem resolved positions first so their USDC lands in the Safe.
        await self._redeem_positions()
        # 2. Confirm any pending USDC deposit (includes USDC from step 1).
        await self._check_pending_deposit()

    async def _close(self) -> None:
        await self._data_api.aclose()
        if self._relayer is not None:
            await self._relayer.close()


def run_claim_process(settings: Settings) -> None:
    """Entry point."""
    setup_logging(level=settings.log_level, bot_name="claimer")
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    log.info("telegram_status", enabled=notifier.enabled)
    process = ClaimProcess(settings, notifier)

    async def _run() -> None:
        try:
            await process.run()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await process._close()

    import contextlib
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())
