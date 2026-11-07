"""Optional Telegram Bot API notifications using only the Python standard library."""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib import request, parse, error

LOGGER = logging.getLogger(__name__)
HTTP_TIMEOUT_SECONDS = 15


@dataclass
class HttpAttempt:
    """Set only when the HTTP opener is actually invoked (no credentials stored)."""
    started_at: str | None = None
    duration_ms: float | None = None


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the Worker authorization header elsewhere.


def _send(bot_token: str, chat_id: str, message: str,
          bot_proxy_url: str | None = None, bot_proxy_secret: str | None = None,
          attempt: HttpAttempt | None = None) -> None:
    if bot_proxy_url:
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
        req = request.Request(
            f"{bot_proxy_url}/bot/sendMessage", data=payload, method="POST",
            headers={"Authorization": f"Bearer {bot_proxy_secret}", "Content-Type": "application/json",
                     "User-Agent": "UsernameMonitor/0.1"},
        )
        opener = request.build_opener(_NoRedirect)
        open_request = opener.open
    else:
        payload = parse.urlencode({"chat_id": chat_id, "text": message,
                                   "disable_web_page_preview": "true"}).encode("utf-8")
        req = request.Request(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=payload,
                              method="POST")
        open_request = request.urlopen
    started = time.monotonic()
    if attempt is not None:
        attempt.started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    try:
        # Never print request URLs, headers, bodies or exception messages.
        with open_request(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError("HTTP-статус уведомления неуспешен")
            result = json.loads(response.read())
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise RuntimeError("Сервис уведомлений ответил ok=false")
    finally:
        if attempt is not None:
            attempt.duration_ms = max(0, (time.monotonic() - started) * 1000)


async def notify(bot_token: str, chat_id: str, message: str, *,
                 bot_proxy_url: str | None = None, bot_proxy_secret: str | None = None,
                 attempt: HttpAttempt | None = None) -> bool:
    if not bot_proxy_url and not bot_token:
        LOGGER.warning("Уведомление не отправлено: способ доставки не настроен")
        return False
    try:
        await asyncio.to_thread(_send, bot_token, chat_id, message, bot_proxy_url, bot_proxy_secret, attempt)
    except error.HTTPError as exc:
        LOGGER.error("Ошибка уведомления: HTTP %s", exc.code)
        return False
    except Exception as exc:
        LOGGER.error("Ошибка уведомления: %s", type(exc).__name__)
        return False
    return True
