from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.apps.claimer.process import (
    ClaimProcess,
    Position,
    _build_activation_transactions,
    _build_ctf_redeem_data,
    _build_deposit_transactions,
    _build_neg_risk_redeem_data,
    _build_transactions,
    _fetch_usdc_allowance,
    _fetch_usdc_balance,
    _format_claim_message,
)
from src.infrastructure.notifications.telegram import TelegramNotifier
from src.shared.settings import Settings


def _make_settings(**kwargs) -> Settings:
    defaults = {
        "proxy_wallet": "0xABCD",
        "private_key": "0x" + "ab" * 32,
        "relayer_url": "https://relayer.example.com",
        "builder_api_key": "key",
        "builder_secret": "c2VjcmV0",  # base64 "secret"
        "builder_passphrase": "pass",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "claim_interval_sec": 300,
        "dry_run": False,
    }
    defaults.update(kwargs)
    return Settings(**defaults)


# Standard CTF position: $9 spent, $10 received → $1 profit
_POSITION_CTF = Position(
    conditionId="0x" + "aa" * 32,
    size=10.0,
    title="BTC 5min Up/Down",
    outcome="Up",
    outcomeIndex=0,
    initialValue=9.0,
    currentValue=10.0,
)

# NegRisk position: outcome index 1 (Down), $4 spent, $5 received → $1 profit
_POSITION_NEG_RISK = Position(
    conditionId="0x" + "bb" * 32,
    size=5.0,
    title="BTC 15min Market",
    outcome="Down",
    outcomeIndex=1,
    negativeRisk=True,
    initialValue=4.0,
    currentValue=5.0,
)


class TestBuildTransactions:
    """Tests for _build_transactions: routes positions to the correct contract and deduplicates CTF."""

    def test_ctf_single_position(self) -> None:
        txns = _build_transactions([_POSITION_CTF])
        assert len(txns) == 1
        assert txns[0]["to"] == "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        assert txns[0]["value"] == "0"
        assert txns[0]["data"].startswith("0x01b7037c")  # redeemPositions selector

    def test_ctf_deduplicates_same_condition_id(self) -> None:
        # Two positions sharing the same conditionId should produce only one CTF tx
        positions = [_POSITION_CTF, _POSITION_CTF.model_copy(update={"outcomeIndex": 1})]
        txns = _build_transactions(positions)
        assert len(txns) == 1

    def test_neg_risk_position(self) -> None:
        # NegRisk goes to the adapter contract, not the CTF contract
        txns = _build_transactions([_POSITION_NEG_RISK])
        assert len(txns) == 1
        assert txns[0]["to"] == "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

    def test_neg_risk_does_not_deduplicate(self) -> None:
        # NegRisk amounts differ per outcome, so each position gets its own tx
        positions = [_POSITION_NEG_RISK, _POSITION_NEG_RISK.model_copy(update={"outcomeIndex": 0})]
        txns = _build_transactions(positions)
        assert len(txns) == 2

    def test_mixed_positions(self) -> None:
        txns = _build_transactions([_POSITION_CTF, _POSITION_NEG_RISK])
        assert len(txns) == 2

    def test_empty_returns_empty(self) -> None:
        assert _build_transactions([]) == []


class TestBuildRedeemData:
    """Tests for ABI-encoded calldata: correct selectors and argument encoding."""

    def test_ctf_redeem_selector(self) -> None:
        data = _build_ctf_redeem_data("0x" + "aa" * 32)
        assert data.startswith("0x01b7037c")

    def test_ctf_contains_usdc(self) -> None:
        # First argument to redeemPositions must be the USDC collateral address
        data = _build_ctf_redeem_data("0x" + "aa" * 32)
        assert "2791bca1f2de4661ed88a30c99a7a9449aa84174" in data.lower()

    def test_neg_risk_redeem_outcome_0(self) -> None:
        data = _build_neg_risk_redeem_data("0x" + "bb" * 32, outcome_index=0, size=10.0)
        assert data.startswith("0x")
        assert len(data) > 10

    def test_neg_risk_redeem_outcome_1(self) -> None:
        # outcome_index changes the amounts array [size, 0] vs [0, size], so calldata must differ
        data0 = _build_neg_risk_redeem_data("0x" + "bb" * 32, outcome_index=0, size=10.0)
        data1 = _build_neg_risk_redeem_data("0x" + "bb" * 32, outcome_index=1, size=10.0)
        assert data0 != data1


class TestFormatClaimMessage:
    """Tests for the Telegram HTML message builder."""

    def test_includes_position_count(self) -> None:
        msg = _format_claim_message([_POSITION_CTF])
        assert "Positions: 1" in msg

    def test_includes_profit(self) -> None:
        # _POSITION_CTF: currentValue=10, initialValue=9 → profit $1.00
        msg = _format_claim_message([_POSITION_CTF])
        assert "$1.00" in msg

    def test_no_tx_link(self) -> None:
        # Claim message should not include block explorer links
        msg = _format_claim_message([_POSITION_CTF])
        assert "polygonscan" not in msg

    def test_includes_market_line(self) -> None:
        msg = _format_claim_message([_POSITION_CTF])
        assert "BTC 5min Up/Down" in msg
        assert "Up" in msg


class TestFetchUsdcBalance:
    """Tests for on-chain USDC balance decoding (eth_call → hex → float)."""

    async def test_decodes_hex_balance(self) -> None:
        # 0x989680 = 10_000_000 raw (6 decimals) = 10.0 USDC
        hex_result = "0x" + "0" * 57 + "989680"
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"result": hex_result})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            balance = await _fetch_usdc_balance("https://polygon-rpc.com", "0xABCD1234")

        assert balance == pytest.approx(10.0)

    async def test_zero_balance(self) -> None:
        hex_result = "0x" + "0" * 64
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"result": hex_result})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            balance = await _fetch_usdc_balance("https://polygon-rpc.com", "0xABCD1234")

        assert balance == 0.0


class TestClaimProcess:
    """Integration-style tests for the main claim loop (_claim_once)."""

    def _make_notifier(self) -> TelegramNotifier:
        return TelegramNotifier(bot_token="", chat_id="")

    async def test_dry_run_skips_submission(self) -> None:
        # In dry_run mode the relayer is never created and no tx is submitted
        settings = _make_settings(dry_run=True)
        notifier = self._make_notifier()
        process = ClaimProcess(settings, notifier)
        assert process._relayer is None

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=[_POSITION_CTF])

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        process._data_api = mock_http
        process._check_pending_deposit = AsyncMock()

        await process._claim_once()

        mock_resp.json.assert_called_once()

    async def test_no_positions_skips_all(self) -> None:
        settings = _make_settings(dry_run=True)
        notifier = self._make_notifier()
        process = ClaimProcess(settings, notifier)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=[])

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        process._data_api = mock_http
        process._check_pending_deposit = AsyncMock()

        await process._claim_once()

    async def test_pending_deposit_sends_notification(self) -> None:
        # If USDC balance > 0 in dry_run, a Telegram alert is sent
        settings = _make_settings(dry_run=True)
        notifier = self._make_notifier()
        notifier._enabled = True
        mock_send = AsyncMock()
        notifier.send = mock_send
        process = ClaimProcess(settings, notifier)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=[])
        process._data_api = AsyncMock(get=AsyncMock(return_value=mock_resp))

        with patch("src.apps.claimer.process._fetch_usdc_balance", AsyncMock(return_value=25.5)):
            await process._claim_once()

        mock_send.assert_called_once()
        assert "25.50" in mock_send.call_args[0][0]

    async def test_pending_deposit_zero_no_notification(self) -> None:
        settings = _make_settings(dry_run=True)
        notifier = self._make_notifier()
        notifier._enabled = True
        mock_send = AsyncMock()
        notifier.send = mock_send
        process = ClaimProcess(settings, notifier)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=[])
        process._data_api = AsyncMock(get=AsyncMock(return_value=mock_resp))

        with patch("src.apps.claimer.process._fetch_usdc_balance", AsyncMock(return_value=0.0)):
            await process._claim_once()

        mock_send.assert_not_called()

    async def test_pending_deposit_error_does_not_crash(self) -> None:
        # RPC failure during balance check must be swallowed — the loop must continue
        settings = _make_settings(dry_run=True)
        notifier = self._make_notifier()
        process = ClaimProcess(settings, notifier)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=[])
        process._data_api = AsyncMock(get=AsyncMock(return_value=mock_resp))

        with patch("src.apps.claimer.process._fetch_usdc_balance", AsyncMock(side_effect=Exception("rpc down"))):
            await process._claim_once()  # should not raise

    async def test_claim_success_sends_telegram(self) -> None:
        # Full happy-path: positions found → tx confirmed → claim notification sent immediately
        settings = _make_settings(dry_run=False)
        notifier = self._make_notifier()
        notifier._enabled = True

        mock_send = AsyncMock()
        notifier.send = mock_send

        # Use __new__ to skip __init__ (avoids real RelayerClient creation)
        process = ClaimProcess.__new__(ClaimProcess)
        process._settings = settings
        process._notifier = notifier

        mock_data_resp = MagicMock()
        mock_data_resp.raise_for_status = MagicMock()
        mock_data_resp.json = MagicMock(return_value=[_POSITION_CTF])

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_data_resp)
        process._data_api = mock_http

        mock_relayer = AsyncMock()
        mock_relayer.is_safe_deployed = AsyncMock(return_value=True)
        mock_relayer.submit = AsyncMock(return_value="tx-123")
        mock_relayer.wait_for_confirmation = AsyncMock(
            return_value={"transactionHash": "0xabc", "state": "STATE_CONFIRMED"},
        )
        mock_relayer._safe = "0xSafe"
        process._relayer = mock_relayer

        process._check_pending_deposit = AsyncMock()

        await process._claim_once()

        # Claim notification sent immediately after redeem, not deferred to deposit
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "BTC 5min Up/Down" in msg
        assert "Profit" in msg

    async def test_safe_not_deployed_skips_submission(self) -> None:
        # If the Gnosis Safe is not yet deployed, no tx should be submitted
        settings = _make_settings(dry_run=False)
        notifier = self._make_notifier()

        process = ClaimProcess.__new__(ClaimProcess)
        process._settings = settings
        process._notifier = notifier

        mock_data_resp = MagicMock()
        mock_data_resp.raise_for_status = MagicMock()
        mock_data_resp.json = MagicMock(return_value=[_POSITION_CTF])

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_data_resp)
        process._data_api = mock_http
        process._check_pending_deposit = AsyncMock()

        mock_relayer = AsyncMock()
        mock_relayer.is_safe_deployed = AsyncMock(return_value=False)
        mock_relayer._safe = "0xSafe"
        process._relayer = mock_relayer

        await process._claim_once()

        mock_relayer.submit.assert_not_called()


class TestFetchUsdcAllowance:
    """Tests for on-chain USDC allowance decoding (eth_call → hex → int)."""

    async def test_decodes_hex_allowance(self) -> None:
        # 0xff * 32 bytes = MAX_UINT256, returned when approve(MAX) was called
        hex_result = "0x" + "ff" * 32
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"result": hex_result})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            allowance = await _fetch_usdc_allowance(
                "https://polygon-rpc.com", "0xOwner", "0xSpender"
            )

        assert allowance == 2**256 - 1

    async def test_zero_allowance(self) -> None:
        hex_result = "0x" + "0" * 64
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"result": hex_result})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            allowance = await _fetch_usdc_allowance(
                "https://polygon-rpc.com", "0xOwner", "0xSpender"
            )

        assert allowance == 0


class TestBuildActivationTransactions:
    """Tests for the 4-tx wallet activation batch (USDC + CTF approvals for both exchanges)."""

    def test_returns_four_transactions(self) -> None:
        txns = _build_activation_transactions()
        assert len(txns) == 4

    def test_usdc_approve_targets(self) -> None:
        # Two USDC approvals: one for Exchange, one for NegRiskExchange
        txns = _build_activation_transactions()
        usdc_txns = [t for t in txns if t["to"] == "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"]
        assert len(usdc_txns) == 2

    def test_ctf_approval_targets(self) -> None:
        # Two setApprovalForAll calls: one for Exchange, one for NegRiskExchange
        txns = _build_activation_transactions()
        ctf_txns = [t for t in txns if t["to"] == "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"]
        assert len(ctf_txns) == 2

    def test_approve_selector(self) -> None:
        txns = _build_activation_transactions()
        usdc_txns = [t for t in txns if t["to"] == "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"]
        for t in usdc_txns:
            assert t["data"].startswith("0x095ea7b3")  # approve(address,uint256)

    def test_set_approval_selector(self) -> None:
        txns = _build_activation_transactions()
        ctf_txns = [t for t in txns if t["to"] == "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"]
        for t in ctf_txns:
            assert t["data"].startswith("0xa22cb465")  # setApprovalForAll(address,bool)

    def test_all_values_zero(self) -> None:
        txns = _build_activation_transactions()
        assert all(t["value"] == "0" for t in txns)


class TestActivateWallet:
    """Tests for _activate_wallet: skips if already approved, handles safe not deployed, notifies on result."""

    def _make_process(self, **kwargs) -> ClaimProcess:
        settings = _make_settings(**kwargs)
        notifier = TelegramNotifier(bot_token="", chat_id="")
        process = ClaimProcess.__new__(ClaimProcess)
        process._settings = settings
        process._notifier = notifier
        return process

    async def test_skips_if_already_activated(self) -> None:
        # Non-zero allowance means wallet is already approved — skip activation entirely
        process = self._make_process()
        mock_relayer = AsyncMock()
        mock_relayer._safe = "0xSafe"
        process._relayer = mock_relayer

        with patch("src.apps.claimer.process._fetch_usdc_allowance", AsyncMock(return_value=2**256 - 1)):
            await process._activate_wallet(50.0)

        mock_relayer.is_safe_deployed.assert_not_called()
        mock_relayer.submit.assert_not_called()

    async def test_skips_if_safe_not_deployed(self) -> None:
        process = self._make_process()
        notifier_send = AsyncMock()
        process._notifier.send = notifier_send

        mock_relayer = AsyncMock()
        mock_relayer._safe = "0xSafe"
        mock_relayer.is_safe_deployed = AsyncMock(return_value=False)
        process._relayer = mock_relayer

        with patch("src.apps.claimer.process._fetch_usdc_allowance", AsyncMock(return_value=0)):
            await process._activate_wallet(50.0)

        mock_relayer.submit.assert_not_called()
        notifier_send.assert_called_once()
        assert "not deployed" in notifier_send.call_args[0][0].lower() or "Safe" in notifier_send.call_args[0][0]

    async def test_activation_success_sends_telegram(self) -> None:
        process = self._make_process()
        notifier_send = AsyncMock()
        process._notifier.send = notifier_send

        mock_relayer = AsyncMock()
        mock_relayer._safe = "0xSafe"
        mock_relayer.is_safe_deployed = AsyncMock(return_value=True)
        mock_relayer.submit = AsyncMock(return_value="tx-activate-001")
        mock_relayer.wait_for_confirmation = AsyncMock(
            return_value={"transactionHash": "0xdeadbeef", "state": "STATE_CONFIRMED"}
        )
        process._relayer = mock_relayer

        with patch("src.apps.claimer.process._fetch_usdc_allowance", AsyncMock(return_value=0)):
            await process._activate_wallet(75.7)

        mock_relayer.submit.assert_called_once()
        call_args = mock_relayer.submit.call_args
        assert call_args[1]["metadata"] == "Wallet activation" or call_args[0][1] == "Wallet activation"
        notifier_send.assert_called_once()
        msg = notifier_send.call_args[0][0]
        assert "75.70" in msg
        assert "0xdeadbeef" in msg

    async def test_activation_confirmation_failed_notifies(self) -> None:
        # wait_for_confirmation returns None when tx fails or times out
        process = self._make_process()
        notifier_send = AsyncMock()
        process._notifier.send = notifier_send

        mock_relayer = AsyncMock()
        mock_relayer._safe = "0xSafe"
        mock_relayer.is_safe_deployed = AsyncMock(return_value=True)
        mock_relayer.submit = AsyncMock(return_value="tx-activate-002")
        mock_relayer.wait_for_confirmation = AsyncMock(return_value=None)
        process._relayer = mock_relayer

        with patch("src.apps.claimer.process._fetch_usdc_allowance", AsyncMock(return_value=0)):
            await process._activate_wallet(50.0)

        notifier_send.assert_called_once()
        assert "failed" in notifier_send.call_args[0][0].lower()

    async def test_activation_submit_error_notifies(self) -> None:
        process = self._make_process()
        notifier_send = AsyncMock()
        process._notifier.send = notifier_send

        mock_relayer = AsyncMock()
        mock_relayer._safe = "0xSafe"
        mock_relayer.is_safe_deployed = AsyncMock(return_value=True)
        mock_relayer.submit = AsyncMock(side_effect=Exception("network error"))
        process._relayer = mock_relayer

        with patch("src.apps.claimer.process._fetch_usdc_allowance", AsyncMock(return_value=0)):
            await process._activate_wallet(50.0)  # should not raise

        notifier_send.assert_called_once()
        assert "activation failed" in notifier_send.call_args[0][0].lower()


class TestBuildDepositTransactions:
    """Tests for the 2-tx deposit batch: approve exact amount + call deposit contract."""

    _PROXY = "0x8Cde572F2F0a5Afb021Ce0ed39d497927D31cbEb"

    def test_returns_two_transactions(self) -> None:
        txns = _build_deposit_transactions(45_000_000, self._PROXY)
        assert len(txns) == 2

    def test_first_tx_approves_usdc(self) -> None:
        txns = _build_deposit_transactions(45_000_000, self._PROXY)
        assert txns[0]["to"] == "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        assert txns[0]["data"].startswith("0x095ea7b3")  # approve(address,uint256)

    def test_approve_targets_deposit_contract(self) -> None:
        txns = _build_deposit_transactions(45_000_000, self._PROXY)
        assert "93070a847efef7f70739046a929d47a521f5b8ee" in txns[0]["data"].lower()

    def test_approve_exact_amount(self) -> None:
        # Deposit uses exact amount, not MAX_UINT256
        txns = _build_deposit_transactions(45_000_000, self._PROXY)
        assert hex(45_000_000)[2:] in txns[0]["data"]

    def test_second_tx_targets_deposit_contract(self) -> None:
        txns = _build_deposit_transactions(45_000_000, self._PROXY)
        assert txns[1]["to"] == "0x93070a847efef7f70739046a929d47a521f5b8ee"

    def test_second_tx_deposit_selector(self) -> None:
        txns = _build_deposit_transactions(45_000_000, self._PROXY)
        assert txns[1]["data"].startswith("0x62355638")  # deposit(address,address,uint256)

    def test_second_tx_encodes_proxy_wallet(self) -> None:
        txns = _build_deposit_transactions(45_000_000, self._PROXY)
        assert self._PROXY[2:].lower() in txns[1]["data"].lower()

    def test_all_values_zero(self) -> None:
        txns = _build_deposit_transactions(45_000_000, self._PROXY)
        assert all(t["value"] == "0" for t in txns)


class TestConfirmDeposit:
    """Tests for _confirm_deposit: submits approve+deposit batch and sends notifications."""

    def _make_process(self) -> ClaimProcess:
        settings = _make_settings(dry_run=False, proxy_wallet="0x8Cde572F2F0a5Afb021Ce0ed39d497927D31cbEb")
        notifier = TelegramNotifier(bot_token="", chat_id="")
        process = ClaimProcess.__new__(ClaimProcess)
        process._settings = settings
        process._notifier = notifier
        return process

    async def test_success_submits_correct_transactions(self) -> None:
        process = self._make_process()
        notifier_send = AsyncMock()
        process._notifier.send = notifier_send

        mock_relayer = AsyncMock()
        mock_relayer.submit = AsyncMock(return_value="tx-dep-001")
        mock_relayer.wait_for_confirmation = AsyncMock(
            return_value={"transactionHash": "0xcafebabe", "state": "STATE_CONFIRMED"}
        )
        process._relayer = mock_relayer

        await process._confirm_deposit(45.0)

        txns = mock_relayer.submit.call_args[0][0]
        assert len(txns) == 2
        assert txns[0]["to"] == "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        assert txns[1]["to"] == "0x93070a847efef7f70739046a929d47a521f5b8ee"

    async def test_confirmation_failed_notifies(self) -> None:
        process = self._make_process()
        notifier_send = AsyncMock()
        process._notifier.send = notifier_send

        mock_relayer = AsyncMock()
        mock_relayer.submit = AsyncMock(return_value="tx-dep-002")
        mock_relayer.wait_for_confirmation = AsyncMock(return_value=None)
        process._relayer = mock_relayer

        await process._confirm_deposit(45.0)

        notifier_send.assert_called_once()
        assert "failed" in notifier_send.call_args[0][0].lower()

    async def test_submit_error_notifies(self) -> None:
        process = self._make_process()
        notifier_send = AsyncMock()
        process._notifier.send = notifier_send

        mock_relayer = AsyncMock()
        mock_relayer.submit = AsyncMock(side_effect=Exception("network error"))
        process._relayer = mock_relayer

        await process._confirm_deposit(45.0)  # should not raise

        notifier_send.assert_called_once()
        assert "failed" in notifier_send.call_args[0][0].lower()

    async def test_amount_converted_correctly(self) -> None:
        # float USDC → raw int with 6 decimals: 75.123456 → 75_123_456
        process = self._make_process()
        process._notifier.send = AsyncMock()

        mock_relayer = AsyncMock()
        mock_relayer.submit = AsyncMock(return_value="tx-dep-003")
        mock_relayer.wait_for_confirmation = AsyncMock(return_value={"transactionHash": "0xabc"})
        process._relayer = mock_relayer

        await process._confirm_deposit(75.123456)

        txns = mock_relayer.submit.call_args[0][0]
        assert hex(75_123_456)[2:] in txns[0]["data"]
