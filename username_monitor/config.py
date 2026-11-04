"""Environment configuration, without storing credentials in source code."""
from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_path: Path
    db_path: Path
    usernames_path: Path
    bot_token: str
    chat_id: str
    delay_seconds: float
    confirm_delay_seconds: float
    cycle_pause_seconds: float
    max_per_cycle: int
    alert_first_free: bool


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
    if bool(bot_token) != bool(chat_id):
        raise ValueError("BOT_TOKEN и CHAT_ID нужно заполнить вместе или оставить оба пустыми")
    max_per_cycle = int(os.getenv("MAX_PER_CYCLE", "20"))
    if not 1 <= max_per_cycle <= 1000:
        raise ValueError("MAX_PER_CYCLE должен быть в диапазоне 1–1000")
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
        cycle_pause_seconds=_float_env("CYCLE_PAUSE_SECONDS", 600, 60),
        max_per_cycle=max_per_cycle,
        alert_first_free=os.getenv("ALERT_FIRST_FREE", "true").lower() in ("1", "true", "yes"),
    )
