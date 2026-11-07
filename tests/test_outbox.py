import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError

from username_monitor.__main__ import main, one_cycle
from username_monitor.outbox import deliver_due, wait_with_outbox
from username_monitor.store import Store


NAME = "first123"
WORKER = "https://worker.example"


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 7, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)
        self.elapsed += seconds


def response():
    value = MagicMock()
    value.status = 200
    value.read.return_value = b'{"ok":true}'
    value.__enter__.return_value = value
    return value


class OutboxCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "monitor.sqlite3"
        self.names = Path(self.temp.name) / "usernames.txt"
        self.names.write_text(NAME + "\n")
        self.clock = Clock()
        self.config = SimpleNamespace(usernames_path=self.names, bot_token="",
                                      chat_id="123", bot_proxy_url=WORKER,
                                      bot_proxy_secret="synthetic-private-token",
                                      alert_first_free=True, check_timeout_seconds=1,
                                      confirm_delay_seconds=0, delay_seconds=0,
                                      cycle_pause_seconds=600)

    def store(self):
        return Store(self.path, clock=self.clock.now)

    async def test_change_and_outbox_are_atomic_even_when_insert_fails(self):
        with self.store() as store:
            store.record(NAME, "occupied")
            store.db.execute("""
                CREATE TRIGGER reject_outbox BEFORE INSERT ON notification_outbox
                BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END
            """)
            store.db.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                store.record(NAME, "free")
            self.assertEqual(store.summary()[0][1], "occupied")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 1)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)

    async def test_free_transitions_uniqueness_cancellation_and_rpc_error(self):
        with self.store() as store:
            store.record(NAME, "free", alert_first_free=False)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
            store.record(NAME, "occupied")
            store.record(NAME, "free")
            store.record(NAME, "free")
            store.mark_error(NAME, "TimeoutError")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
            self.assertEqual(store.due_notifications(10)[0].username, NAME)
            store.record(NAME, "occupied")
            self.assertEqual(store.db.execute("SELECT status FROM notification_outbox").fetchone()[0], "cancelled")
            store.record(NAME, "free")
            rows = store.db.execute("SELECT change_id,status FROM notification_outbox ORDER BY id").fetchall()
            self.assertEqual([x[1] for x in rows], ["cancelled", "pending"])
            self.assertNotEqual(rows[0][0], rows[1][0])
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute("""
                    INSERT INTO notification_outbox(change_id,username,status,created_at,next_attempt_at)
                    VALUES (?,?,'pending',?,?)
                """, (rows[1][0], NAME, store.now(), store.now()))
            store.db.rollback()

    async def test_worker_success_and_http_errors_keep_event(self):
        with self.store() as store:
            store.record(NAME, "free", alert_first_free=True)
            opener = MagicMock()
            for status, delay in ((403, 30), (429, 60), (502, 120)):
                opener.open.side_effect = HTTPError(WORKER, status, "private response", {}, None)
                with patch("username_monitor.notify.request.build_opener", return_value=opener), \
                        self.assertLogs("username_monitor", level="WARNING") as logs:
                    self.assertEqual(await deliver_due(self.config, store), 1)
                self.assertNotIn("private response", " ".join(logs.output))
                self.assertNotIn(self.config.bot_proxy_secret, " ".join(logs.output))
                self.assertEqual(store.db.execute("SELECT status FROM notification_outbox").fetchone()[0], "pending")
                self.assertEqual(store.next_notification_at(), self.clock.now() + timedelta(seconds=delay))
                self.clock.advance(delay)
            opener.open.side_effect = None
            opener.open.return_value = response()
            with patch("username_monitor.notify.request.build_opener", return_value=opener):
                self.assertEqual(await deliver_due(self.config, store), 1)
            row = store.db.execute("SELECT status,attempts,delivered_at,last_error_code FROM notification_outbox").fetchone()
            self.assertEqual((row[0], row[1], row[3]), ("delivered", 4, None))
            self.assertIsNotNone(row[2])
            self.assertEqual(opener.open.call_args.args[0].full_url, WORKER + "/bot/sendMessage")
            self.assertEqual(json.loads(opener.open.call_args.args[0].data)["text"],
                             "🟢 Возможный свободный username: @first123\n"
                             "До этого: не проверялся\n"
                             "Проверь и назначь вручную в Telegram. Доступность не гарантирована.")

    async def test_timeout_connection_error_restart_and_backoff(self):
        with self.store() as store:
            store.record(NAME, "free", alert_first_free=True)
            opener = MagicMock()
            for error, delay in ((TimeoutError("private data"), 30), (OSError("private data"), 60)):
                opener.open.side_effect = error
                with patch("username_monitor.notify.request.build_opener", return_value=opener), \
                        self.assertLogs("username_monitor", level="WARNING") as logs:
                    await deliver_due(self.config, store)
                self.assertNotIn("private data", " ".join(logs.output))
                self.assertEqual(store.db.execute("SELECT attempts FROM notification_outbox").fetchone()[0], 1 if delay == 30 else 2)
                self.assertEqual(store.next_notification_at(), self.clock.now() + timedelta(seconds=delay))
                self.assertEqual(await deliver_due(self.config, store), 0)
                self.clock.advance(delay)
        with self.store() as reopened:
            self.assertEqual(reopened.due_notifications(10)[0].attempts, 2)
            with patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock, return_value=True):
                await deliver_due(self.config, reopened)
            self.assertEqual(reopened.db.execute("SELECT status,attempts FROM notification_outbox").fetchone(),
                             ("delivered", 3))

    async def test_backoff_cap_and_batch_limit(self):
        with self.store() as store:
            for i in range(12):
                store.record(f"name{i:05d}", "free", alert_first_free=True)
            self.names.write_text("\n".join(f"name{i:05d}" for i in range(12)))
            with patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock,
                       return_value=False) as notify:
                with self.assertLogs("username_monitor", level="WARNING"):
                    self.assertEqual(await deliver_due(self.config, store), 10)
                self.assertEqual(notify.await_count, 10)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox WHERE attempts=0").fetchone()[0], 2)
            event = store.db.execute("SELECT id,username,previous_status,attempts FROM notification_outbox WHERE attempts=1 LIMIT 1").fetchone()
            from username_monitor.store import OutboxEvent
            for attempts in range(2, 10):
                current = OutboxEvent(event[0], event[1], event[2], attempts)
                store.failed(current)
            self.assertEqual(store.next_notification_at(), self.clock.now())  # two untouched rows
            next_at = store.db.execute("SELECT next_attempt_at FROM notification_outbox WHERE id=?", (event[0],)).fetchone()[0]
            self.assertEqual(datetime.fromisoformat(next_at), self.clock.now() + timedelta(seconds=3600))

    async def test_direct_bot_api_uses_same_outbox(self):
        self.config.bot_proxy_url = None
        self.config.bot_proxy_secret = None
        self.config.bot_token = "synthetic-bot-token"
        with self.store() as store:
            store.record(NAME, "free", alert_first_free=True)
            with patch("username_monitor.notify.request.urlopen", return_value=response()) as urlopen:
                self.assertEqual(await deliver_due(self.config, store), 1)
            self.assertEqual(store.db.execute("SELECT status FROM notification_outbox").fetchone()[0], "delivered")
            self.assertTrue(urlopen.call_args.args[0].full_url.endswith("/sendMessage"))

    async def test_removed_username_and_stale_status_are_cancelled(self):
        with self.store() as store:
            store.record(NAME, "free", alert_first_free=True)
            self.names.write_text("other123\n")
            with patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock) as notify:
                await deliver_due(self.config, store)
                notify.assert_not_awaited()
            self.assertEqual(store.db.execute("SELECT status FROM notification_outbox").fetchone()[0], "cancelled")

    async def test_queue_during_cycle_pause_has_no_mtproto(self):
        with self.store() as store:
            store.record(NAME, "free", alert_first_free=True)
            delays = []
            async def fake_sleep(seconds):
                delays.append(seconds)
                self.clock.advance(seconds)
            with patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock,
                       side_effect=[False, True]) as notify, \
                    patch("username_monitor.__main__.create_telegram_client") as client:
                await wait_with_outbox(self.config, store, 600, sleep=fake_sleep,
                                       monotonic=lambda: self.clock.elapsed)
            self.assertEqual(notify.await_count, 2)
            client.assert_not_called()
            self.assertEqual(delays, [30, 570])
            self.assertEqual(store.db.execute("SELECT status FROM notification_outbox").fetchone()[0], "delivered")

    async def test_sigterm_cancellation_preserves_pending_row(self):
        entered = asyncio.Event()
        with self.store() as store:
            store.record(NAME, "free", alert_first_free=True)
            async def stalled(*_args, **_kwargs):
                entered.set()
                await asyncio.Event().wait()
            with patch("username_monitor.outbox.notify_module.notify", side_effect=stalled):
                task = asyncio.create_task(deliver_due(self.config, store))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        with self.store() as reopened:
            self.assertEqual(reopened.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(reopened.db.execute("SELECT status,attempts FROM notification_outbox").fetchone(),
                             ("pending", 0))

    async def test_one_failure_does_not_block_other_message(self):
        with self.store() as store:
            store.record(NAME, "free", alert_first_free=True)
            store.record("other123", "free", alert_first_free=True)
            self.names.write_text(NAME + "\nother123\n")
            with patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock,
                       side_effect=[False, True]) as notify:
                self.assertEqual(await deliver_due(self.config, store), 2)
            self.assertEqual(notify.await_count, 2)
            self.assertEqual(dict(store.db.execute("SELECT username,status FROM notification_outbox")),
                             {NAME: "pending", "other123": "delivered"})

    async def test_confirmed_free_required_for_creation(self):
        with self.store() as store:
            with patch("username_monitor.__main__.load_names", return_value=([NAME], [])), \
                    patch("username_monitor.notify.notify", new_callable=AsyncMock) as notify:
                await one_cycle(AsyncMock(side_effect=[True, False]), self.config, store, 1)
                notify.assert_not_awaited()
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)


class TestMigrationAndCli(unittest.TestCase):
    def test_existing_database_migrates_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE state(username TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'unknown',
                                       checked_at TEXT,detail TEXT NOT NULL DEFAULT '');
                    CREATE TABLE changes(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL,
                                         old_status TEXT,new_status TEXT NOT NULL,changed_at TEXT NOT NULL);
                    INSERT INTO state VALUES ('first123','occupied','2026-11-01T00:00:00+00:00','kept');
                    INSERT INTO changes(username,old_status,new_status,changed_at)
                        VALUES('first123',NULL,'occupied','2026-11-01T00:00:00+00:00');
                """)
            with Store(path) as store:
                self.assertEqual(store.summary()[0][1:], ("occupied", "2026-11-01T00:00:00+00:00", "kept"))
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)

    def test_notifications_status_is_read_only_and_needs_no_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "monitor.sqlite3"
            with Store(path) as store:
                store.record(NAME, "free", alert_first_free=True)
                store.failed(store.due_notifications(1)[0])
            output = io.StringIO()
            with patch.dict(os.environ, {"DB_PATH": str(path)}, clear=True), \
                    patch("dotenv.load_dotenv"), \
                    patch("username_monitor.__main__.load_config") as config, \
                    patch("username_monitor.__main__.create_telegram_client") as client, \
                    patch("sys.argv", ["username_monitor", "notifications-status"]), redirect_stdout(output):
                self.assertEqual(main(), 0)
            config.assert_not_called()
            client.assert_not_called()
            self.assertIn("pending: 1", output.getvalue())
            self.assertIn("delivery_failed: 1", output.getvalue())
            self.assertNotIn(NAME, output.getvalue())
            self.assertNotIn("token", output.getvalue())

    def test_notifications_status_on_old_db_does_not_migrate_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE state(username TEXT PRIMARY KEY)")
            output = io.StringIO()
            with patch.dict(os.environ, {"DB_PATH": str(path)}, clear=True), \
                    patch("dotenv.load_dotenv"), \
                    patch("sys.argv", ["username_monitor", "notifications-status"]), \
                    redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertIn("таблица ещё не создана", output.getvalue())
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='notification_outbox'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
