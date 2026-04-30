from __future__ import annotations

import httpx

from src.shared.logging import get_logger

log = get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """Async Telegram notification client.

    Silently disabled when bot_token or chat_id is empty.
    Never raises — notification failures are logged and swallowed,
    so a Telegram outage never crashes the caller.

    Usage anywhere::

        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        await notifier.send("Hello!")
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._enabled = bool(bot_token and chat_id)
        self._url = f"{_TELEGRAM_API}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, text: str, parse_mode: str = "HTML") -> None:
        """Send a Telegram message.

        No-op if the notifier is disabled (no token/chat_id configured).
        """
        if not self._enabled:
            return

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self._url,
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                    },
                )
                resp.raise_for_status()
            log.debug("telegram_sent", chars=len(text))
        except Exception as exc:
            log.warning("telegram_send_failed", error=type(exc).__name__, detail=str(exc))
