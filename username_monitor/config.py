"""Environment configuration, without storing credentials in source code."""
from dataclasses import dataclass, field
import base64
import binascii
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
    mtproxy_host: str
    mtproxy_port: int
    mtproxy_secret: bytes = field(repr=False)
    bot_proxy_url: str | None = None
    bot_proxy_secret: str | None = field(default=None, repr=False)
    auto_discovery_enabled: bool = False
    auto_discovery_per_cycle: int = 1
    auto_discovery_daily_limit: int = 12
    auto_discovery_pool_size: int = 300


def _float_env(key: str, default: float, min_value: float) -> float:
    value = float(os.getenv(key, default))
    if not min_value <= value <= 86400:
        raise ValueError(f"{key} должен быть в диапазоне {min_value}–86400")
    return value


def _int_env(key: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(key, str(default))
    if not raw.isdecimal() or not minimum <= int(raw) <= maximum:
        raise ValueError(f"{key} должен быть целым числом от {minimum} до {maximum}")
    return int(raw)


def load_config() -> Config:
    # Capture before dotenv: the FakeTLS secret must come from the service's
    # protected environment, never from a project-local .env file.
    protected_mtproxy_secret = os.environ.get("TG_MTPROXY_SECRET_B64", "")
    load_dotenv(override=False)
    if not protected_mtproxy_secret:
        os.environ.pop("TG_MTPROXY_SECRET_B64", None)
    raw_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    if not raw_id.isdecimal() or not api_hash:
        raise ValueError("Укажите TG_API_ID и TG_API_HASH в .env (my.telegram.org)")
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    primary_chat_id = os.getenv("CHAT_ID", "").strip()
    legacy_chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if primary_chat_id and legacy_chat_id and primary_chat_id != legacy_chat_id:
        raise ValueError("CHAT_ID и TG_CHAT_ID заданы с разными значениями")
    chat_id = primary_chat_id or legacy_chat_id
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
    mtproxy_host = os.getenv("TG_MTPROXY_HOST", "").strip()
    raw_port = os.getenv("TG_MTPROXY_PORT", "").strip()
    raw_secret = protected_mtproxy_secret.strip()
    if not mtproxy_host or not raw_port or not raw_secret:
        raise ValueError("Укажите TG_MTPROXY_HOST, TG_MTPROXY_PORT и TG_MTPROXY_SECRET_B64 вместе")
    if not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65535:
        raise ValueError("TG_MTPROXY_PORT должен быть целым числом от 1 до 65535")
    try:
        mtproxy_secret = base64.b64decode(raw_secret, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("TG_MTPROXY_SECRET_B64 имеет неверный формат") from None
    # Marker + 16-byte key + ASCII SNI. Never include source bytes in errors.
    if len(mtproxy_secret) != 22 or mtproxy_secret[0] != 0xEE or not re.fullmatch(
        rb"[A-Za-z0-9.-]{5}", mtproxy_secret[17:]
    ):
        raise ValueError("TG_MTPROXY_SECRET_B64 должен кодировать 22 байта EE FakeTLS")
    mtproxy_port = int(raw_port)
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
        bot_proxy_url=bot_proxy_url.rstrip("/") or None,
        bot_proxy_secret=bot_proxy_secret or None,
        mtproxy_host=mtproxy_host,
        mtproxy_port=mtproxy_port,
        mtproxy_secret=mtproxy_secret,
        auto_discovery_enabled=os.getenv("AUTO_DISCOVERY_ENABLED", "false").lower() in ("1", "true", "yes"),
        auto_discovery_per_cycle=_int_env("AUTO_DISCOVERY_PER_CYCLE", 1, 0, 1),
        auto_discovery_daily_limit=_int_env("AUTO_DISCOVERY_DAILY_LIMIT", 12, 1, 12),
        auto_discovery_pool_size=_int_env("AUTO_DISCOVERY_POOL_SIZE", 300, 1, 1000),
    )
