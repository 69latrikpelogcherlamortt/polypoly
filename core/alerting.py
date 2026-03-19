"""Multi-level alerting system with rate-limited Telegram delivery."""

import asyncio
import enum
import logging
import time
from typing import Any, Callable, Coroutine, Dict, Optional

log = logging.getLogger("alerting")


class AlertLevel(enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


_EMOJIS: Dict[AlertLevel, str] = {
    AlertLevel.WARNING: "⚠️",
    AlertLevel.CRITICAL: "🚨",
    AlertLevel.EMERGENCY: "🔴",
}

_RATE_LIMIT_SECONDS = 300  # 5 minutes


class AlertManager:
    """Routes alerts to the log and, when appropriate, to Telegram."""

    def __init__(
        self,
        send_fn: Callable[..., Coroutine[Any, Any, Any]],
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._send_fn = send_fn
        self._config = config or {}
        self._last_sent: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send(
        self,
        level: AlertLevel,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Dispatch an alert according to its severity level.

        * INFO      – log only
        * WARNING   – log + Telegram (rate-limited, 5 min cooldown)
        * CRITICAL  – log + Telegram (immediate)
        * EMERGENCY – log + Telegram (immediate)
        """
        self._log(level, title, body, data)

        if level == AlertLevel.INFO:
            return

        if level == AlertLevel.WARNING:
            if not self._rate_limit_ok(title):
                log.debug("Rate-limited WARNING alert: %s", title)
                return

        message = self._format_message(level, title, body, data)
        await self._send_fn(message)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(
        self,
        level: AlertLevel,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]],
    ) -> None:
        extra = f" | data={data}" if data else ""
        text = f"[{level.value}] {title}: {body}{extra}"

        if level == AlertLevel.INFO:
            log.info(text)
        elif level == AlertLevel.WARNING:
            log.warning(text)
        elif level == AlertLevel.CRITICAL:
            log.critical(text)
        elif level == AlertLevel.EMERGENCY:
            log.critical(text)

    def _rate_limit_ok(self, key: str) -> bool:
        """Return True if enough time has passed since the last send for *key*."""
        now = time.monotonic()
        last = self._last_sent.get(key, 0.0)
        if now - last < _RATE_LIMIT_SECONDS:
            return False
        self._last_sent[key] = now
        return True

    @staticmethod
    def _format_message(
        level: AlertLevel,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]],
    ) -> str:
        emoji = _EMOJIS.get(level, "")
        header = f"{emoji} {level.value}: {title}" if emoji else f"{level.value}: {title}"
        parts = [header, "", body]
        if data:
            parts.append("")
            for k, v in data.items():
                parts.append(f"  {k}: {v}")
        return "\n".join(parts)
