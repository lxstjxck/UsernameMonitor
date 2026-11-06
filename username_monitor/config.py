"""Environment configuration, without storing credentials in source code."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str = field(repr=False)
    session_path: Path
    db_path: Path
    usernames_path: Path
    bot_token: str = field(repr=False)
    chat_id: str
    delay_seconds: float
    confirm_delay_seconds: float
    check_timeout_seconds: float
    cycle_pause_seconds: float
    max_per_cycle: int
    alert_first_free: bool
    proxy_host: str | None = None
    proxy_port: int | None = None
    proxy_secret: str | None = field(default=None, repr=False)
    bot_proxy_url: str | None = None
    bot_proxy_secret: str | None = field(default=None, repr=False)


def _float_env(key: str, default: float, min_value: float) -> float:
    value = float(os.getenv(key, default))
    if not min_value <= value <= 86400:
        raise ValueError(f"{key} должен быть в диапазоне {min_value}–86400")
    return value


def load_config() -> Config:
    load_dotenv(override=False)
    raw_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    if not raw_id.isdecimal() or not api_hash:
        raise ValueError("Укажите TG_API_ID и TG_API_HASH в .env (my.telegram.org)")
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    bot_proxy_url = os.getenv("BOT_PROXY_URL", "").strip()
    bot_proxy_secret = os.getenv("BOT_PROXY_SECRET", "").strip()
    if bool(bot_proxy_url) != bool(bot_proxy_secret):
        raise ValueError("Укажите BOT_PROXY_URL и BOT_PROXY_SECRET вместе")
    if bot_proxy_url:
        try:
            parsed = urlsplit(bot_proxy_url)
            port = parsed.port
        except ValueError:
            port = -1
            parsed = None
        if (parsed is None or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or port == -1 or (port is not None and not 1 <= port <= 65535)
                or parsed.path not in ("", "/") or "?" in bot_proxy_url or "#" in bot_proxy_url):
            raise ValueError("BOT_PROXY_URL должен быть HTTPS-адресом Worker без пути, параметров и учётных данных")
        if not chat_id:
            raise ValueError("Укажите CHAT_ID для уведомлений через Worker")
    elif bool(bot_token) != bool(chat_id):
        raise ValueError("BOT_TOKEN и CHAT_ID нужно заполнить вместе или оставить оба пустыми")
    max_per_cycle = int(os.getenv("MAX_PER_CYCLE", "20"))
    if not 1 <= max_per_cycle <= 1000:
        raise ValueError("MAX_PER_CYCLE должен быть в диапазоне 1–1000")
    proxy_host = os.getenv("TG_PROXY_HOST", "").strip()
    raw_proxy_port = os.getenv("TG_PROXY_PORT", "").strip()
    proxy_secret = os.getenv("TG_PROXY_SECRET", "").strip()
    if any((proxy_host, raw_proxy_port, proxy_secret)) and not all((proxy_host, raw_proxy_port, proxy_secret)):
        raise ValueError("Укажите TG_PROXY_HOST, TG_PROXY_PORT и TG_PROXY_SECRET вместе")
    proxy_port = None
    if proxy_host:
        if not raw_proxy_port.isdecimal() or not 1 <= int(raw_proxy_port) <= 65535:
            raise ValueError("TG_PROXY_PORT должен быть целым числом от 1 до 65535")
        proxy_port = int(raw_proxy_port)
        if not re.fullmatch(r"(?:[0-9a-fA-F]{32}|[dD]{2}[0-9a-fA-F]{32})", proxy_secret):
            raise ValueError("TG_PROXY_SECRET должен содержать 32 шестнадцатеричных символа или dd и ещё 32")
        proxy_secret = proxy_secret.lower()
    return Config(
        api_id=int(raw_id),
        api_hash=api_hash,
        session_path=Path(os.getenv("SESSION_PATH", "data/telegram")),
        db_path=Path(os.getenv("DB_PATH", "data/monitor.sqlite3")),
        usernames_path=Path(os.getenv("USERNAMES_PATH", "usernames.txt")),
        bot_token=bot_token,
        chat_id=chat_id,
        delay_seconds=_float_env("CHECK_DELAY_SECONDS", 30, 5),
        confirm_delay_seconds=_float_env("CONFIRM_DELAY_SECONDS", 10, 5),
        check_timeout_seconds=_float_env("CHECK_TIMEOUT_SECONDS", 30, 1),
        cycle_pause_seconds=_float_env("CYCLE_PAUSE_SECONDS", 600, 60),
        max_per_cycle=max_per_cycle,
        alert_first_free=os.getenv("ALERT_FIRST_FREE", "true").lower() in ("1", "true", "yes"),
        proxy_host=proxy_host or None,
        proxy_port=proxy_port,
        proxy_secret=proxy_secret or None,
        bot_proxy_url=bot_proxy_url.rstrip("/") or None,
        bot_proxy_secret=bot_proxy_secret or None,
    )
