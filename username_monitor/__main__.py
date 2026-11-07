"""Command-line interface and bounded background monitoring loop."""
import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .names import USERNAME_PATTERN, load_names
from .store import Store
from .telemetry import Telemetry, safe_record

LOG = logging.getLogger("username_monitor")


def create_telegram_client(config, *, flood_sleep_threshold=None):
    from telethon import TelegramClient
    from .faketls import connection_for_mtproxy

    options = {
        "request_retries": 1, "connection_retries": 2, "retry_delay": 1,
        "connection": connection_for_mtproxy(config.mtproxy_host, config.mtproxy_port, config.mtproxy_secret),
    }
    if flood_sleep_threshold is not None:
        options["flood_sleep_threshold"] = flood_sleep_threshold
    return TelegramClient(str(config.session_path), config.api_id, config.api_hash, **options)


class OnDemandTelegramClient:
    """Use one session owner, with a connection only while an RPC is active."""

    def __init__(self, client):
        self.client = client
        self._lock = asyncio.Lock()

    async def __call__(self, request):
        # The same SQLite session must never be used by concurrent connections.
        async with self._lock:
            try:
                await self.client.connect()
                return await self.client(request)
            finally:
                # Telethon cancels its receive, update and keepalive tasks here.
                # asyncio.wait_for in one_cycle also waits for this on timeout.
                await self.client.disconnect()


async def login(config) -> None:
    config.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = create_telegram_client(config)
    try:
        await client.start()
        me = await client.get_me()
        print(f"Сессия создана: user_id={me.id}. Файл храните только на VPS, не в Git.")
    finally:
        await client.disconnect()


async def _check_measured(client, name, timeout, phase, telemetry, *, monotonic=time.monotonic):
    from telethon import errors
    from .telegram_api import check_username

    started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    started = monotonic()
    result, flood_seconds = "other_error", None
    try:
        status, note = await asyncio.wait_for(check_username(client, name), timeout)
        result = status
        return status, note
    except errors.FloodWaitError as exc:
        result, flood_seconds = "flood_wait", exc.seconds
        raise
    except asyncio.TimeoutError:
        result = "timeout"
        raise
    except errors.RPCError:
        result = "rpc_error"
        raise
    except OSError:
        result = "network_error"
        raise
    finally:
        safe_record(telemetry, "rpc", started_at, (monotonic() - started) * 1000,
                    phase, result, flood_seconds)


async def one_cycle(client, config, store: Store, limit: int, telemetry=None, *, monotonic=time.monotonic) -> float:
    from telethon import errors
    from .outbox import deliver_due
    from .discovery import pool_for

    names, invalid = load_names(config.usernames_path)
    if invalid:
        LOG.warning("Пропущено некорректных имён: %d (%s)", len(invalid), ", ".join(invalid[:3]))
    enabled = getattr(config, "auto_discovery_enabled", False)
    pool = pool_for(store) if enabled else None
    if pool is not None:
        pool.recover()
        pool.fill(config.auto_discovery_pool_size)
    store.cancel_unwatched(names, pool.active_names() if pool is not None else [])
    selected_manual = store.select(names, limit)
    selected = [(name, False) for name in selected_manual]
    if pool is not None and config.auto_discovery_per_cycle and len(selected) < limit:
        auto_name = pool.pick(names, config.auto_discovery_daily_limit,
                              market=getattr(config, "market_discovery_enabled", False))
        if auto_name is not None:
            selected.append((auto_name, True))
    if not selected:
        LOG.warning("Список имён пуст; проверять нечего")
        return config.cycle_pause_seconds
    if pool is None:
        LOG.info("Проверяем %d имён из %d (только чтение)", len(selected), len(names))
    else:
        LOG.info("Проверяем %d ручных и %d автоматических имён (только чтение)",
                 len(selected_manual), len(selected) - len(selected_manual))
    for i, (name, is_auto) in enumerate(selected):
        if is_auto and not pool.begin(name, config.auto_discovery_daily_limit):
            continue
        try:
            status, note = await _check_measured(client, name, config.check_timeout_seconds, "primary", telemetry,
                                                 monotonic=monotonic)
            if status == "free":
                # A second result reduces noisy one-off responses; still not a guarantee.
                await asyncio.sleep(config.confirm_delay_seconds)
                status, note = await _check_measured(client, name, config.check_timeout_seconds,
                                                     "confirmation", telemetry, monotonic=monotonic)
            previous, changed = store.record(name, status, note,
                                             alert_first_free=True if is_auto else config.alert_first_free,
                                             notification_source="auto" if is_auto else "manual",
                                             auto_candidate=is_auto)
            LOG.info("@%s: %s%s", name, status, f" (было {previous})" if changed else "")
        except errors.FloodWaitError as exc:
            cooldown = max(config.cycle_pause_seconds, float(exc.seconds) + 60)
            if is_auto:
                pool.temporary_error(name, "flood_wait", cooldown)
            store.mark_error(name, "FloodWait")
            LOG.warning("FLOOD_WAIT: прекращаем цикл; следующий запуск не раньше чем через %.0f с", cooldown)
            return cooldown
        except asyncio.TimeoutError:
            if is_auto:
                pool.temporary_error(name, "timeout", config.cycle_pause_seconds)
            store.mark_error(name, "TimeoutError")
            LOG.warning("@%s: превышено время проверки; цикл остановлен", name)
            return config.cycle_pause_seconds
        except Exception as exc:
            # Do not include raw exception messages (they may contain data).
            if is_auto:
                pool.temporary_error(name, type(exc).__name__, config.cycle_pause_seconds)
            store.mark_error(name, type(exc).__name__)
            LOG.warning("@%s: временная ошибка %s", name, type(exc).__name__)
        # Re-read the watch list immediately before delivery; it may have
        # changed while this cycle was checking another username.
        await deliver_due(config, store, telemetry)
        if i + 1 < len(selected):
            await asyncio.sleep(config.delay_seconds)
    safe_record(telemetry, "cycle")
    return config.cycle_pause_seconds


async def monitor(config, once: bool, limit: int) -> None:
    from .outbox import deliver_due, wait_with_outbox
    config.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = create_telegram_client(config, flood_sleep_threshold=0)
    try:
        try:
            await asyncio.wait_for(client.connect(), config.check_timeout_seconds)
            authorized = await asyncio.wait_for(client.is_user_authorized(), config.check_timeout_seconds)
        finally:
            await client.disconnect()
        if not authorized:
            raise RuntimeError("Сессия не авторизована: запустите python -m username_monitor login")
        on_demand = OnDemandTelegramClient(client)
        with Store(config.db_path) as store, Telemetry(config.db_path) as telemetry:
            while True:
                pause = await one_cycle(on_demand, config, store, limit, telemetry)
                await deliver_due(config, store, telemetry)
                if once:
                    break
                LOG.info("Ожидание %.0f секунд до следующего цикла", pause)
                await wait_with_outbox(config, store, pause, telemetry=telemetry)
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


def show_notifications_status(db_path: Path) -> None:
    import sqlite3
    if not db_path.exists():
        print("Очередь уведомлений: база ещё не создана")
        return
    db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_outbox'").fetchone():
            print("Очередь уведомлений: таблица ещё не создана")
            return
        counts = dict(db.execute("SELECT status,COUNT(*) FROM notification_outbox GROUP BY status"))
        for status in ("pending", "delivered", "cancelled"):
            print(f"{status}: {counts.get(status, 0)}")
        next_at = db.execute("SELECT MIN(next_attempt_at) FROM notification_outbox WHERE status='pending'").fetchone()[0]
        print(f"next_attempt_at: {next_at or '-'}")
        for code, count in db.execute("""
            SELECT last_error_code,COUNT(*) FROM notification_outbox
            WHERE status='pending' AND last_error_code IS NOT NULL GROUP BY last_error_code
        """):
            # Only fixed safe categories written by this application are shown.
            if code == "delivery_failed":
                print(f"delivery_failed: {count}")
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram username monitor (только проверка)")
    parser.add_argument("command", choices=["login", "check", "run", "status", "notifications-status", "metrics-status", "candidates", "market", "diagnose", "verify-transport"])
    parser.add_argument("candidate_action", nargs="?", choices=["generate", "list", "top", "status", "rescore",
                                                        "refresh", "compare", "explain", "score"])
    # The positional action is shared with candidates; validate market actions below.
    parser.add_argument("--limit", type=int, default=None, help="Количество имён за цикл (не более MAX_PER_CYCLE)")
    parser.add_argument("--username", help="Имя для diagnose или verify-transport (не выводится в отчёте)")
    parser.add_argument("--reconnect", action="store_true", help="Принудительный reconnect в verify-transport")
    parser.add_argument("--idle-probe", action="store_true", help="Проверка 25 секунд простоя и периодических RPC на копии сессии")
    parser.add_argument("--hours", type=int, default=24, help="Период metrics-status в часах (по умолчанию 24)")
    parser.add_argument("--json", action="store_true", help="JSON-отчёт metrics-status")
    parser.add_argument("--count", type=int, default=300, help="Количество кандидатов для генерации")
    parser.add_argument("--seed", type=int, help="Seed для воспроизводимой генерации")
    parser.add_argument("--top", type=int, default=None, help="Количество строк candidates list/top")
    parser.add_argument("--category", choices=["startup", "short"], help="Фильтр категории candidates list")
    parser.add_argument("--state", choices=["queued", "checking", "checked", "confirmed_free", "discarded", "retry"],
                        help="Фильтр состояния candidates list")
    parser.add_argument("--as-of", help="UTC cutoff для сравнения, ISO 8601")
    parser.add_argument("--username-market", help="Имя кандидата для market explain")
    parser.add_argument("--file", help="Локальный JSON снимок для market refresh (не нужен для сети)")
    parser.add_argument("--pool", choices=["test", "base"], help="Пул для market compare")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.idle_probe and args.command != "verify-transport":
            parser.error("--idle-probe доступен только для verify-transport")
        if args.command == "candidates" and args.candidate_action not in ("generate", "list", "top", "status", "rescore"):
            parser.error("для candidates укажите generate, list, top, status или rescore")
        if args.command == "market" and args.candidate_action not in ("generate", "status", "refresh", "compare", "explain", "score"):
            parser.error("для market укажите generate, status, refresh, compare, explain или score")
        if args.command not in ("candidates", "market") and args.candidate_action is not None:
            parser.error("позиционное действие доступно только для candidates и market")
        if args.command in ("status", "notifications-status", "metrics-status", "candidates", "market"):
            from dotenv import load_dotenv
            import os
            load_dotenv(override=False)
            db_path = Path(os.getenv("DB_PATH", "data/monitor.sqlite3"))
            if args.command == "market":
                import json
                from contextlib import closing
                from .market_data import connect, fetch_daily, import_daily, market_status, utc
                from .market_reporting import compare, explain, score_unchecked
                if args.candidate_action in ("generate", "refresh", "score"):
                    with closing(connect(db_path)) as db:
                        if args.candidate_action == "generate":
                            from .market_generation import persist_test_pool
                            rows = persist_test_pool(db, args.count, args.seed)
                            print(f"Тестовый рыночный пул: {len(rows)}; production queue не изменена")
                        elif args.candidate_action == "refresh":
                            try:
                                if args.file:
                                    from .market_data import MAX_BYTES
                                    with open(args.file, "rb") as source:
                                        raw = source.read(MAX_BYTES + 1)
                                else:
                                    raw = fetch_daily()
                                added = import_daily(db, raw)
                                print(f"Добавлено рыночных снимков: {added}")
                            except (OSError, ValueError):
                                print("Источник недоступен или данные некорректны; используем последний кеш")
                            print(json.dumps(market_status(db_path), ensure_ascii=False))
                        else:
                            print(f"Рыночные оценки непроверенных кандидатов: {score_unchecked(db)}")
                else:
                    if args.candidate_action == "status":
                        report = market_status(db_path)
                    elif not db_path.exists():
                        report = {"status": "no_data"}
                    else:
                        with closing(connect(db_path, readonly=True)) as db:
                            if args.candidate_action == "explain":
                                name = (args.username_market or "").removeprefix("@").lower()
                                if not USERNAME_PATTERN.fullmatch(name):
                                    parser.error("market explain требует имя через --username-market")
                                report = explain(db, name)
                            else:
                                report = compare(db, as_of=utc(args.as_of) if args.as_of else None,
                                                 top=args.top or 20, pool=args.pool or "auto")
                    print(json.dumps(report, ensure_ascii=False, indent=2))
            elif args.command == "candidates":
                from .discovery import CandidatePool, read_pool
                if args.candidate_action in ("generate", "rescore"):
                    import sqlite3
                    from types import SimpleNamespace
                    pool_size = int(os.getenv("AUTO_DISCOVERY_POOL_SIZE", "300"))
                    if not 1 <= pool_size <= 1000:
                        raise ValueError("AUTO_DISCOVERY_POOL_SIZE должен быть от 1 до 1000")
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    with sqlite3.connect(db_path) as db:
                        pool = CandidatePool(SimpleNamespace(db=db, clock=lambda: datetime.now(timezone.utc)))
                        if args.candidate_action == "generate":
                            added = pool.generate(args.count, seed=args.seed, pool_size=pool_size)
                            print(f"Добавлено кандидатов: {added}")
                        else:
                            changed = pool.rescore_unchecked()
                            print(f"Переоценено непроверенных кандидатов: {changed}")
                else:
                    print(read_pool(db_path, action=args.candidate_action,
                                    top=args.top if args.top is not None else (10 if args.candidate_action == "top" else 20),
                                    category=args.category, status=args.state,
                                    daily_limit=int(os.getenv("AUTO_DISCOVERY_DAILY_LIMIT", "12"))))
            elif args.command == "status":
                show_status(db_path)
            elif args.command == "notifications-status":
                show_notifications_status(db_path)
            else:
                from .telemetry import show_report
                show_report(db_path, args.hours, args.json)
            return 0
        config = load_config()
        if args.command == "verify-transport":
            from .transport_probe import verify_transport
            name = (args.username or "").removeprefix("@").lower()
            if args.username and not USERNAME_PATTERN.fullmatch(name):
                parser.error("для verify-transport укажите корректное имя через --username")
            if args.reconnect and not args.username:
                parser.error("--reconnect требует тестовое имя через --username")
            return asyncio.run(verify_transport(
                config, create_telegram_client, username=name or None, reconnect=args.reconnect,
                idle_probe=args.idle_probe,
            ))
        if args.command == "diagnose":
            from .diagnostics import diagnose

            name = (args.username or "").removeprefix("@").lower()
            if not USERNAME_PATTERN.fullmatch(name):
                parser.error("для diagnose укажите корректное имя через --username")
            return asyncio.run(diagnose(config, name, create_telegram_client))
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
