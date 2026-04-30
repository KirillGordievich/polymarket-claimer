from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # RPC
    rpc_url: str = "https://polygon-bor-rpc.publicnode.com"
    log_level: str = "info"

    # Wallet
    proxy_wallet: str = ""
    private_key: str = ""

    # Relayer
    relayer_url: str = "https://relayer-v2.polymarket.com"

    # Builder (https://polymarket.com/settings?tab=builder)
    builder_api_key: str = ""
    builder_secret: str = ""
    builder_passphrase: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Claim job
    claim_interval_sec: int = 300

    # Mode
    dry_run: bool = False


def get_settings() -> Settings:
    return Settings()
