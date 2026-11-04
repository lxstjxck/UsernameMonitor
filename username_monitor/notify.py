"""Optional Telegram Bot API notifications using only the Python standard library."""
import asyncio
import json
import logging
from urllib import request, parse, error

LOGGER = logging.getLogger(__name__)


def _send(bot_token: str, chat_id: str, message: str) -> None:
    payload = parse.urlencode({"chat_id": chat_id, "text": message,
                               "disable_web_page_preview": "true"}).encode("utf-8")
    req = request.Request(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=payload,
                          method="POST")
    # Do not print URLs or exception messages: they may contain the bot token.
    with request.urlopen(req, timeout=15) as response:
        result = json.loads(response.read())
        if not result.get("ok"):
            raise RuntimeError("Bot API ответил ok=false")


async def notify(bot_token: str, chat_id: str, message: str) -> None:
    if not bot_token:
        LOGGER.warning("УВЕДОМЛЕНИЕ (бот не настроен): %s", message)
        return
    try:
        await asyncio.to_thread(_send, bot_token, chat_id, message)
    except error.HTTPError as exc:
        LOGGER.error("Ошибка уведомления: Bot API HTTP %s", exc.code)
    except Exception as exc:
        LOGGER.error("Ошибка уведомления: %s", type(exc).__name__)
