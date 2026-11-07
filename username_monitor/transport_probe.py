"""Read-only RPC verification against a disposable SQLite backup of the session."""

import asyncio
from contextlib import closing
from dataclasses import replace
import logging
import os
from pathlib import Path
import sqlite3
import tempfile

from telethon import functions


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


async def verify_transport(config, create_client) -> int:
    """No login, username check, notification, or production session writes."""
    previous_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="username-monitor-probe-") as tmp:
            copied = Path(tmp) / "telegram.session"
            backup_session(session_file(config.session_path), copied)
            client = create_client(replace(config, session_path=copied), flood_sleep_threshold=0)
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
                return 0
            finally:
                await asyncio.wait_for(client.disconnect(), 5)
    except (Exception,) as exc:
        # Connection and RPC exceptions may include private endpoint details.
        print(f"verify-transport: FAIL ({type(exc).__name__})")
        return 1
    finally:
        logging.disable(previous_logging_level)
