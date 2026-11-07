"""Read-only RPC verification against a disposable SQLite backup of the session."""

import asyncio
from contextlib import closing
from dataclasses import replace
import logging
import os
from pathlib import Path
import sqlite3
import tempfile

from telethon import errors, functions

from .store import Store
from .telegram_api import check_username


def session_file(path: Path) -> Path:
    return path if path.suffix == ".session" else Path(f"{path}.session")


def backup_session(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError("Файл сессии не найден")
    # SQLite's online backup sees one consistent snapshot, including WAL,
    # without copying live -wal/-shm files or modifying the source database.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
    except BaseException:
        destination.unlink()
        raise


async def verify_transport(config, create_client, *, username: str | None = None,
                           reconnect: bool = False) -> int:
    """Probe only a copy; optional username result goes to isolated SQLite."""
    previous_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="username-monitor-probe-") as tmp:
            copied = Path(tmp) / "telegram.session"
            backup_session(session_file(config.session_path), copied)
            copied_config = replace(config, session_path=copied)
            client = create_client(copied_config, flood_sleep_threshold=0)
            try:
                await asyncio.wait_for(client.connect(), 20)
                if not await asyncio.wait_for(client.is_user_authorized(), 15):
                    print("session-copy: не авторизована")
                    return 1
                me = await asyncio.wait_for(client.get_me(), 15)
                if me is None:
                    print("get_me: нет ответа")
                    return 1
                print("get_me: PASS")
                await asyncio.wait_for(client(functions.help.GetConfigRequest()), 15)
                print("help.GetConfig: PASS")
                if username is not None:
                    status, note = await asyncio.wait_for(
                        check_username(client, username), config.check_timeout_seconds
                    )
                    with Store(Path(tmp) / "probe.sqlite3") as store:
                        store.record(username, status, note)
                        row = store.summary(1)[0]
                        if row[0] != username or row[1] != status:
                            raise RuntimeError("SQLite probe verification failed")
                    print(f"account.checkUsername: PASS ({status})")
                    print("isolated SQLite: PASS")
                if reconnect:
                    await asyncio.wait_for(client.disconnect(), 5)
                    client = create_client(copied_config, flood_sleep_threshold=0)
                    await asyncio.wait_for(client.connect(), 20)
                    if not await asyncio.wait_for(client.is_user_authorized(), 15):
                        print("reconnect: не авторизована")
                        return 1
                    await asyncio.wait_for(client(functions.help.GetConfigRequest()), 15)
                    print("reconnect: PASS")
                return 0
            finally:
                await asyncio.wait_for(client.disconnect(), 5)
    except errors.FloodWaitError as exc:
        print(f"verify-transport: FLOOD_WAIT ({exc.seconds} с)")
        return 1
    except Exception as exc:
        # Connection and RPC exceptions may include private endpoint details.
        print(f"verify-transport: FAIL ({type(exc).__name__})")
        return 1
    finally:
        logging.disable(previous_logging_level)
