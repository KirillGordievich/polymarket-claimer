from __future__ import annotations

import json
import logging
import sys
from decimal import Decimal
from typing import Any

import structlog


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__!r} is not JSON serializable")


def _extract_component(name: str) -> str:
    """Derive a short component label from a module path.

    Examples:
        src.apps.bot.process            → "bot"
        src.apps.syncer.process         → "syncer"
        src.core.strategy.simple        → "strategy"
        src.core.order.dry_run          → "order"
        src.core.position.manager       → "position"
        src.infrastructure.websocket.*  → "websocket"
        src.main                        → "main"
    """
    parts = name.split(".")
    if len(parts) >= 3 and parts[0] == "src" and parts[1] in ("apps", "core", "infrastructure"):
        return parts[2]
    if len(parts) >= 2 and parts[0] == "src":
        return parts[1]
    return parts[-1]


def _merge_src(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Merge ``bot`` + ``component`` into a single ``src`` field.

    Deduplicates redundant cases so logs stay compact:

    * ``bot=syncer, component=syncer``      → ``src=syncer``
    * ``bot=syncer, component=websocket``   → ``src=syncer/websocket``
    * ``bot=btc_5m, component=bot``         → ``src=btc_5m``
    * ``bot=btc_5m, component=strategy``    → ``src=btc_5m/strategy``
    * ``component=main`` (no bot)           → ``src=main``
    """
    bot = event_dict.pop("bot", None)
    component = event_dict.pop("component", None)
    if bot and component and component != bot and component != "bot":
        event_dict["src"] = f"{bot}/{component}"
    elif bot:
        event_dict["src"] = bot
    elif component:
        event_dict["src"] = component
    return event_dict


_ANSI = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
}
_RESET = "\033[0m"


def _make_renderer() -> Any:
    """Build a JSON renderer with optional ANSI color wrapping.

    Color is controlled by a ``_color`` key in the event dict (e.g.
    ``log.info("event", _color="green")``).  The key is stripped before
    serialisation so JSON stays clean.  Colors are only applied when stderr
    is a TTY.
    """
    json_render = structlog.processors.JSONRenderer(
        serializer=lambda *a, **kw: json.dumps(*a, **{**kw, "default": _json_default}),
    )

    if not sys.stderr.isatty():
        def plain(logger: Any, method_name: str, event_dict: dict[str, Any]) -> str:
            event_dict.pop("_color", None)
            return json_render(logger, method_name, event_dict)
        return plain

    def colored(logger: Any, method_name: str, event_dict: dict[str, Any]) -> str:
        color = event_dict.pop("_color", None)
        rendered: str = json_render(logger, method_name, event_dict)
        if color and color in _ANSI:
            return f"{_ANSI[color]}{rendered}{_RESET}"
        return rendered

    return colored


def setup_logging(level: str = "info", bot_name: str | None = None) -> None:
    """Configure structlog with JSON output.

    Args:
        level: Log level string (debug, info, warning, error).
        bot_name: Optional bot name to bind to all log entries in this process.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _merge_src,
            _make_renderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    if bot_name:
        structlog.contextvars.bind_contextvars(bot=bot_name)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a named logger instance with component pre-bound.

    Uses structlog's ``initial_values`` kwarg so the logger remains a lazy
    proxy and is materialized only at first use — this matters because modules
    create ``log = get_logger(__name__)`` at import time, before
    :func:`setup_logging` runs. Calling ``.bind()`` here would materialize the
    logger with the default (non-configured) structlog settings.
    """
    return structlog.get_logger(name, component=_extract_component(name))
