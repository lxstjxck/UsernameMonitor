"""Command-line interface and bounded background monitoring loop."""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load_config
from .names import load_names
from .store import Store

LOG = logging.getLogger("username_monitor")


async def login(config) -> None:
    from telethon import TelegramClient
    config.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(config.session_path), config.api_id, config.api_hash)
    try:
        await client.start()
        me = await client.get_me()
        print(f"Сессия создана: user_id={me.id}. Файл храните только на VPS, не в Git.")
    finally:
        await client.disconnect()


async def one_cycle(client, config, store: Store, limit: int) -> float:
    from telethon import errors
    from .telegram_api import check_username
    from .notify import notify

    names, invalid = load_names(config.usernames_path)
    if invalid:
        LOG.warning("Пропущено некорректных имён: %d (%s)", len(invalid), ", ".join(invalid[:3]))
    selected = store.select(names, limit)
    if not selected:
        LOG.warning("Список имён пуст; проверять нечего")
        return config.cycle_pause_seconds
    LOG.info("Проверяем %d имён из %d (только чтение)", len(selected), len(names))
    for i, name in enumerate(selected):
        try:
            status, note = await check_username(client, name)
            if status == "free":
                # A second result reduces noisy one-off responses; still not a guarantee.
                await asyncio.sleep(config.confirm_delay_seconds)
                status, note = await check_username(client, name)
            previous, changed = store.record(name, status, note)
            LOG.info("@%s: %s%s", name, status, f" (было {previous})" if changed else "")
            if status == "free" and changed and (previous is not None or config.alert_first_free):
                await notify(config.bot_token, config.chat_id,
                             f"🟢 Возможный свободный username: @{name}\n"
                             f"До этого: {previous or 'не проверялся'}\n"
                             "Проверь и назначь вручную в Telegram. Доступность не гарантирована.")
        except errors.FloodWaitError as exc:
            cooldown = max(config.cycle_pause_seconds, float(exc.seconds) + 60)
            store.mark_error(name, "FloodWait")
            LOG.warning("FLOOD_WAIT: прекращаем цикл; следующий запуск не раньше чем через %.0f с", cooldown)
            return cooldown
        except Exception as exc:
            # Do not include raw exception messages (they may contain data).
            store.mark_error(name, type(exc).__name__)
            LOG.warning("@%s: временная ошибка %s", name, type(exc).__name__)
        if i + 1 < len(selected):
            await asyncio.sleep(config.delay_seconds)
    return config.cycle_pause_seconds


async def monitor(config, once: bool, limit: int) -> None:
    from telethon import TelegramClient
    config.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(config.session_path), config.api_id, config.api_hash,
                            flood_sleep_threshold=0)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия не авторизована: запустите python -m username_monitor login")
        with Store(config.db_path) as store:
            while True:
                pause = await one_cycle(client, config, store, limit)
                if once:
                    break
                LOG.info("Ожидание %.0f секунд до следующего цикла", pause)
                await asyncio.sleep(pause)
    finally:
        await client.disconnect()


def show_status(db_path: Path) -> None:
    if not db_path.exists():
        print("База ещё не создана, запустите check или run")
        return
    with Store(db_path) as store:
        rows = store.summary()
    for name, status, checked, detail in rows:
        print(f"@{name:32s} {status:12s} {checked or '-':28s} {detail}")
    if not rows:
        print("Проверок ещё не было")


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram username monitor (только проверка)")
    parser.add_argument("command", choices=["login", "check", "run", "status"])
    parser.add_argument("--limit", type=int, default=None, help="Количество имён за цикл (не более MAX_PER_CYCLE)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "status":
            from dotenv import load_dotenv
            import os
            load_dotenv(override=False)
            show_status(Path(os.getenv("DB_PATH", "data/monitor.sqlite3")))
            return 0
        config = load_config()
        limit = config.max_per_cycle if args.limit is None else args.limit
        if not (1 <= limit <= config.max_per_cycle):
            parser.error(f"--limit должен быть от 1 до {config.max_per_cycle}")
        if args.command == "login":
            asyncio.run(login(config))
        else:
            asyncio.run(monitor(config, once=args.command == "check", limit=limit))
        return 0
    except KeyboardInterrupt:
        print("Остановлено пользователем")
        return 130
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1
    except Exception as exc:
        LOG.error("Неожиданная ошибка: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
