# Polymarket Claimer

Automated bot that claims resolved Polymarket positions on Polygon. Runs on a configurable interval, batches redemptions into Gnosis Safe transactions, and optionally sends Telegram notifications.

## Setup

```bash
cp .env.example .env
# Fill in .env with your credentials
uv sync
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PRIVATE_KEY` | Yes | — | EOA private key (signs Safe transactions) |
| `PROXY_WALLET` | Yes | — | Polymarket proxy wallet (Safe) address |
| `BUILDER_API_KEY` | Yes | — | Polymarket Builder API key |
| `BUILDER_SECRET` | Yes | — | Polymarket Builder API secret |
| `BUILDER_PASSPHRASE` | Yes | — | Polymarket Builder API passphrase |
| `RPC_URL` | No | public Polygon node | Polygon RPC endpoint |
| `TELEGRAM_BOT_TOKEN` | No | — | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | No | — | Telegram chat ID for notifications |
| `CLAIM_INTERVAL_SEC` | No | `300` | Seconds between claim cycles |
| `LOG_LEVEL` | No | `info` | Log level: `debug` / `info` / `warning` |
| `DRY_RUN` | No | `false` | Fetch positions without submitting transactions |

Builder API credentials: [polymarket.com/settings?tab=builder](https://polymarket.com/settings?tab=builder)

## Usage

```bash
make help   # Show all available commands
make dry    # Test run — fetch positions without submitting transactions
make run    # Production run
make test   # Run tests
make lint   # Lint code
```

## Architecture

```
src/
├── apps/claimer/
│   ├── __main__.py           # CLI entry point (--dry-run flag)
│   └── process.py            # Main claim loop, transaction builders
├── infrastructure/
│   ├── polymarket/
│   │   └── relayer_client.py # Gnosis Safe signing + relayer HTTP client
│   └── notifications/
│       └── telegram.py       # Telegram notifier
└── shared/
    ├── settings.py           # Pydantic settings (reads from .env)
    └── logging.py            # Structured logging via structlog
```

### Claim cycle

Each cycle (every `CLAIM_INTERVAL_SEC`):

1. Fetch all redeemable positions (paginated) from `data-api.polymarket.com`
2. Build and submit a batched Gnosis Safe transaction via the Polymarket relayer
3. Detect any pending USDC deposit on the proxy wallet
4. If deposit found: activate wallet approvals if needed, then confirm the deposit

On error, retries up to 3 times before waiting for the next cycle. A Telegram notification is sent once after all retries are exhausted.

### Transaction types

- **CTF redeem**: `redeemPositions(USDC, 0x0, conditionId, [1, 2])` — one tx per conditionId
- **NegRisk redeem**: `redeemPositions(conditionId, amounts)` — one tx per position
- **Wallet activation**: 4 approval txs (USDC + CTF for both Exchange and NegRiskExchange)
- **Deposit confirmation**: `approve` + `deposit` batch for pending USDC on proxy wallet

All multi-step flows are batched into a single Gnosis Safe multisend transaction.
