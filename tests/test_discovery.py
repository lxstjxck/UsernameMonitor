import asyncio
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import base64
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon import errors

from username_monitor.__main__ import main, one_cycle
from username_monitor.config import load_config
from username_monitor.discovery import CandidatePool, generate_candidates, read_pool, score_name
from username_monitor.names import USERNAME_PATTERN
from username_monitor.outbox import deliver_due
from username_monitor.store import Store
from username_monitor.telemetry import Telemetry


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 7, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def response():
    value = MagicMock()
    value.status = 200
    value.read.return_value = b'{"ok":true}'
    value.__enter__.return_value = value
    return value


class DiscoveryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "monitor.sqlite3"
        self.names = Path(self.temp.name) / "usernames.txt"
        self.names.write_text("manual123\n")
        self.clock = Clock()
        self.config = SimpleNamespace(
            usernames_path=self.names, check_timeout_seconds=1,
            confirm_delay_seconds=10, delay_seconds=30, cycle_pause_seconds=600,
            alert_first_free=False, bot_token="", chat_id="123",
            bot_proxy_url="https://worker.example", bot_proxy_secret="private-token",
            auto_discovery_enabled=True, auto_discovery_per_cycle=1,
            auto_discovery_daily_limit=12, auto_discovery_pool_size=1)

    def store(self):
        return Store(self.path, clock=self.clock.now)

    def seeded(self, store):
        pool = CandidatePool(store)
        self.assertEqual(pool.generate(1, seed=42, pool_size=1), 1)
        return pool, pool.pick([], 12)

    async def test_generator_ratio_seed_validity_scoring_and_dedup(self):
        first = generate_candidates(300, seed=42)
        self.assertEqual(first, generate_candidates(300, seed=42))
        self.assertEqual(len(first), len({c.username for c in first}))
        self.assertEqual(Counter(c.category for c in first), {"startup": 210, "short": 90})
        self.assertTrue(all(USERNAME_PATTERN.fullmatch(c.username) and 0 <= c.score <= 100
                            and c.reason for c in first))
        self.assertTrue(all(5 <= len(c.username) <= 10 and c.username.isalpha() for c in first))
        self.assertIsNone(score_name("bad___", "short"))
        self.assertIsNone(score_name("aaaaa", "short"))
        self.assertIsNone(score_name("xxxxxx", "startup"))
        with self.assertRaises(ValueError):
            generate_candidates(1001, seed=1)

    async def test_config_defaults_and_hard_limits(self):
        secret = base64.b64encode(bytes.fromhex("ee" + "a1" * 16) + b"a.com").decode()
        base = {"TG_API_ID": "12345", "TG_API_HASH": "synthetic",
                "TG_MTPROXY_HOST": "proxy.example", "TG_MTPROXY_PORT": "443",
                "TG_MTPROXY_SECRET_B64": secret}
        with patch.dict(os.environ, base, clear=True), patch("username_monitor.config.load_dotenv"):
            cfg = load_config()
        self.assertFalse(cfg.auto_discovery_enabled)
        self.assertEqual((cfg.auto_discovery_per_cycle, cfg.auto_discovery_daily_limit,
                          cfg.auto_discovery_pool_size), (1, 12, 300))
        for key, value in (("AUTO_DISCOVERY_PER_CYCLE", "2"),
                           ("AUTO_DISCOVERY_DAILY_LIMIT", "13"),
                           ("AUTO_DISCOVERY_POOL_SIZE", "1001")):
            with self.subTest(key=key), patch.dict(os.environ, base | {key: value}, clear=True), \
                    patch("username_monitor.config.load_dotenv"), self.assertRaisesRegex(ValueError, key):
                load_config()

    async def test_pool_restart_dedup_quota_recovery_and_retention(self):
        with self.store() as store:
            pool = CandidatePool(store)
            self.assertEqual(pool.generate(300, seed=42, pool_size=300), 300)
            self.assertEqual(pool.generate(300, seed=42, pool_size=300), 0)
            candidate = pool.pick([], 12)
            self.assertTrue(pool.begin(candidate, 12))
            self.assertEqual(pool.remaining(12), 11)
            self.assertEqual(store.db.execute("SELECT attempts FROM discovery_candidates WHERE username=?",
                                              (candidate,)).fetchone()[0], 1)
        with self.store() as reopened:
            pool = CandidatePool(reopened)
            pool.recover()
            self.assertEqual(pool.remaining(12), 11)
            self.assertEqual(reopened.db.execute("SELECT status FROM discovery_candidates WHERE username=?",
                                                 (candidate,)).fetchone()[0], "retry")
            self.assertTrue(pool.begin(candidate, 12))
            pool.temporary_error(candidate, "timeout", 600)
            self.assertEqual(pool.remaining(12), 10)
            self.clock.advance(24 * 3600 + 1)
            pool.recover()
            self.assertEqual(pool.remaining(12), 12)
            # The long-term pool remains unique across restarts and refills.
            self.assertEqual(reopened.db.execute("SELECT COUNT(DISTINCT username) FROM discovery_candidates"
                                                 ).fetchone()[0], 300)

    async def test_rolling_twelve_attempt_limit_survives_restart(self):
        self.names.write_text("")
        with self.store() as store:
            pool = CandidatePool(store)
            pool.generate(12, seed=12, pool_size=12)
            for _ in range(12):
                name = pool.pick([], 12)
                self.assertIsNotNone(name)
                self.assertTrue(pool.begin(name, 12))
                pool.temporary_error(name, "network_error", 600)
            self.assertEqual(pool.remaining(12), 0)
            self.assertIsNone(pool.pick([], 12))
        with self.store() as store:
            pool = CandidatePool(store)
            self.assertEqual(pool.remaining(12), 0)
            self.clock.advance(24 * 3600 + 1)
            self.assertEqual(pool.remaining(12), 12)

    async def test_auto_free_and_candidate_update_are_atomic(self):
        with self.store() as store:
            pool, auto = self.seeded(store)
            pool.begin(auto, 12)
            store.db.execute("""CREATE TRIGGER fail_outbox BEFORE INSERT ON notification_outbox
                BEGIN SELECT RAISE(ABORT, 'private fault'); END""")
            store.db.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                store.record(auto, "free", alert_first_free=True,
                             notification_source="auto", auto_candidate=True)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT status FROM discovery_candidates WHERE username=?",
                                              (auto,)).fetchone()[0], "checking")
            pool.recover()
            self.assertEqual(store.db.execute("SELECT status FROM discovery_candidates WHERE username=?",
                                              (auto,)).fetchone()[0], "retry")
            self.assertEqual(pool.remaining(12), 11)

    async def test_cancelled_rpc_releases_candidate_on_restart(self):
        self.names.write_text("")
        with self.store() as store:
            pool, auto = self.seeded(store)
            entered = asyncio.Event()

            async def stalled(_client, _name):
                entered.set()
                await asyncio.Event().wait()

            with patch("username_monitor.telegram_api.check_username", side_effect=stalled):
                task = asyncio.create_task(one_cycle(object(), self.config, store, 1))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(pool.remaining(12), 11)
            self.assertEqual(store.db.execute("SELECT status FROM discovery_candidates WHERE username=?",
                                              (auto,)).fetchone()[0], "checking")
        with self.store() as reopened:
            pool = CandidatePool(reopened)
            pool.recover()
            self.assertEqual(reopened.db.execute("SELECT status FROM discovery_candidates WHERE username=?",
                                                 (auto,)).fetchone()[0], "retry")

    async def test_migration_keeps_all_existing_data(self):
        with self.store() as store, Telemetry(self.path, clock=self.clock.now) as metrics:
            store.record("manual123", "free", alert_first_free=True)
            metrics.rpc(self.clock.now().isoformat(), 20, "primary", "free")
            before = {table: store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in
                      ("state", "changes", "notification_outbox", "rpc_metrics", "delivery_metrics", "cycle_metrics")}
            CandidatePool(store).generate(10, seed=3, pool_size=10)
            after = {table: store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
            self.assertEqual(before, after)
            self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(store.db.execute("SELECT source FROM notification_outbox").fetchone()[0], "manual")

    async def test_manual_priority_one_auto_and_full_capacity(self):
        with self.store() as store:
            pool, auto = self.seeded(store)
            called = []

            async def check(_client, name):
                called.append(name)
                return "occupied", ""

            with patch("username_monitor.telegram_api.check_username", side_effect=check), \
                    patch("username_monitor.__main__.asyncio.sleep", new_callable=AsyncMock) as sleep:
                await one_cycle(object(), self.config, store, 2)
            self.assertEqual(called, ["manual123", auto])
            sleep.assert_awaited_once_with(30)
            self.assertEqual(pool.remaining(12), 11)
            self.assertEqual(store.db.execute("SELECT status FROM discovery_candidates WHERE username=?",
                                              (auto,)).fetchone()[0], "checked")
            self.assertEqual(self.config.delay_seconds, 30)
        with self.store() as store:
            second = []

            async def check(_client, name):
                second.append(name)
                return "occupied", ""

            with patch("username_monitor.telegram_api.check_username", side_effect=check):
                await one_cycle(object(), self.config, store, 1)
            self.assertEqual(second, ["manual123"])
            self.assertEqual(CandidatePool(store).remaining(12), 11)

    async def test_disabled_never_uses_auto_rpc_and_cancels_only_auto_alert(self):
        with self.store() as store:
            pool, auto = self.seeded(store)
            self.names.write_text("manual123\n")
            pool.begin(auto, 12)
            store.record(auto, "free", alert_first_free=True,
                         notification_source="auto", auto_candidate=True)
            store.record("manual123", "free", alert_first_free=True)
            self.config.auto_discovery_enabled = False
            with patch("username_monitor.telegram_api.check_username", new_callable=AsyncMock,
                       return_value=("free", "")) as rpc, \
                    patch("username_monitor.__main__.asyncio.sleep", new_callable=AsyncMock), \
                    patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock,
                          return_value=False):
                await one_cycle(object(), self.config, store, 2)
            self.assertEqual(rpc.await_count, 2)
            rows = dict(store.db.execute("SELECT source,status FROM notification_outbox"))
            self.assertEqual(rows["auto"], "cancelled")
            self.assertEqual(rows["manual"], "pending")

    async def test_confirmed_free_outbox_message_retry_and_discard_policy(self):
        self.names.write_text("")
        with self.store() as store, Telemetry(self.path, clock=self.clock.now) as metrics:
            pool, auto = self.seeded(store)
            responses = [("free", ""), ("free", "")]
            with patch("username_monitor.telegram_api.check_username", new_callable=AsyncMock,
                       side_effect=responses) as rpc, \
                    patch("username_monitor.__main__.asyncio.sleep", new_callable=AsyncMock) as sleep, \
                    patch("username_monitor.notify.request.build_opener") as builder:
                builder.return_value.open.side_effect = OSError("private address")
                with self.assertLogs("username_monitor", level="WARNING") as logs:
                    await one_cycle(object(), self.config, store, 1, metrics)
                self.assertEqual(rpc.await_count, 2)
                sleep.assert_awaited_once_with(10)
                self.assertNotIn("private address", " ".join(logs.output))
            self.assertEqual(store.db.execute("SELECT phase,COUNT(*) FROM rpc_metrics GROUP BY phase"
                                              ).fetchall(), [("confirmation", 1), ("primary", 1)])
            event = store.db.execute("SELECT source,status,attempts FROM notification_outbox").fetchone()
            self.assertEqual(event, ("auto", "pending", 1))
            self.assertEqual(store.db.execute("SELECT status FROM discovery_candidates WHERE username=?",
                                              (auto,)).fetchone()[0], "confirmed_free")
            self.clock.advance(30)
            with patch("username_monitor.notify.request.build_opener") as builder:
                builder.return_value.open.return_value = response()
                await deliver_due(self.config, store, metrics)
                message = json.loads(builder.return_value.open.call_args.args[0].data)["text"]
                self.assertIn(auto, message)
                self.assertIn("startup", message)
                self.assertIn("рейтинг", message)
                self.assertIn("Два успешных Telegram RPC", message)
            self.assertEqual(store.db.execute("SELECT status FROM notification_outbox").fetchone()[0], "delivered")
            self.assertEqual(store.db.execute("SELECT success,COUNT(*) FROM delivery_metrics GROUP BY success"
                                              ).fetchall(), [(0, 1), (1, 1)])
            self.assertEqual(pool.remaining(12), 11)

    async def test_once_free_no_event_timeout_flood_and_limited_retry(self):
        self.names.write_text("")
        with self.store() as store:
            pool, auto = self.seeded(store)
            with patch("username_monitor.telegram_api.check_username", new_callable=AsyncMock,
                       side_effect=[("free", ""), asyncio.TimeoutError()]), \
                    patch("username_monitor.__main__.asyncio.sleep", new_callable=AsyncMock):
                self.assertEqual(await one_cycle(object(), self.config, store, 1), 600)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT status FROM discovery_candidates WHERE username=?",
                                              (auto,)).fetchone()[0], "retry")
            self.clock.advance(600)
            with patch("username_monitor.telegram_api.check_username", new_callable=AsyncMock,
                       side_effect=errors.FloodWaitError(request=None, capture=700)):
                self.assertEqual(await one_cycle(object(), self.config, store, 1), 760)
            self.assertEqual(pool.remaining(12), 10)
            self.clock.advance(760)
            with patch("username_monitor.telegram_api.check_username", new_callable=AsyncMock,
                       side_effect=OSError("private")), self.assertLogs("username_monitor", level="WARNING"):
                await one_cycle(object(), self.config, store, 1)
            row = store.db.execute("SELECT status,attempts,excluded_reason FROM discovery_candidates WHERE username=?",
                                   (auto,)).fetchone()
            self.assertEqual(row, ("discarded", 3, "retry_limit"))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)

    async def test_discard_cancels_auto_but_preserves_manual_pending(self):
        with self.store() as store:
            pool, auto = self.seeded(store)
            pool.begin(auto, 12)
            store.record(auto, "free", alert_first_free=True,
                         notification_source="auto", auto_candidate=True)
            store.record("manual123", "free", alert_first_free=True)
            with store.db:
                store.db.execute("UPDATE discovery_candidates SET status='discarded' WHERE username=?", (auto,))
            with patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock,
                       return_value=False) as notify:
                await deliver_due(self.config, store)
            self.assertEqual(notify.await_count, 1)
            self.assertEqual(dict(store.db.execute("SELECT source,status FROM notification_outbox")),
                             {"auto": "cancelled", "manual": "pending"})

    async def test_deleting_candidate_cancels_pending_auto_event(self):
        self.names.write_text("")
        with self.store() as store:
            pool, auto = self.seeded(store)
            pool.begin(auto, 12)
            store.record(auto, "free", alert_first_free=True,
                         notification_source="auto", auto_candidate=True)
            with store.db:
                store.db.execute("DELETE FROM discovery_candidates WHERE username=?", (auto,))
            with patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock) as notify:
                await deliver_due(self.config, store)
            notify.assert_not_awaited()
            self.assertEqual(store.db.execute("SELECT status FROM notification_outbox").fetchone()[0],
                             "cancelled")

    async def test_new_definitive_status_cancels_stale_auto_free(self):
        with self.store() as store:
            pool, auto = self.seeded(store)
            pool.begin(auto, 12)
            store.record(auto, "free", alert_first_free=True,
                         notification_source="auto", auto_candidate=True)
            store.record(auto, "occupied")
            self.assertEqual(store.db.execute("SELECT status FROM notification_outbox").fetchone()[0],
                             "cancelled")
            self.assertEqual(store.db.execute("SELECT status,result FROM discovery_candidates WHERE username=?",
                                              (auto,)).fetchone(), ("checked", "occupied"))

    async def test_readonly_cli_and_generate_only_pool(self):
        old = self.path
        with sqlite3.connect(old) as db:
            db.execute("CREATE TABLE state (username TEXT PRIMARY KEY, status TEXT)")
            db.execute("INSERT INTO state VALUES ('manual123','occupied')")
        output = io.StringIO()
        with patch.dict(os.environ, {"DB_PATH": str(old)}, clear=True), patch("dotenv.load_dotenv"), \
                patch("username_monitor.__main__.load_config", side_effect=AssertionError("secret")), \
                patch("username_monitor.__main__.create_telegram_client", side_effect=AssertionError("network")), \
                patch("sys.argv", ["username_monitor", "candidates", "list", "--top", "20"]), \
                redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn("no data", output.getvalue())
        self.assertIn("daily_remaining: 12", read_pool(old, action="status"))
        with sqlite3.connect(old) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='discovery_candidates'").fetchone()[0], 0)
        with patch.dict(os.environ, {"DB_PATH": str(old)}, clear=True), patch("dotenv.load_dotenv"), \
                patch("sys.argv", ["username_monitor", "candidates", "generate", "--count", "10", "--seed", "42"]), \
                patch("username_monitor.__main__.load_config", side_effect=AssertionError("secret")), \
                patch("username_monitor.__main__.create_telegram_client", side_effect=AssertionError("network")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        with sqlite3.connect(old) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("notification_outbox", tables)
            self.assertEqual(db.execute("SELECT status FROM state WHERE username='manual123'").fetchone()[0], "occupied")
        output = io.StringIO()
        with patch.dict(os.environ, {"DB_PATH": str(old)}, clear=True), patch("dotenv.load_dotenv"), \
                patch("sys.argv", ["username_monitor", "candidates", "status"]), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn("queued: 10", output.getvalue())
        self.assertIn("daily_remaining: 12", output.getvalue())
        self.assertIn("startup", read_pool(old, action="list", category="startup", top=20))


if __name__ == "__main__":
    unittest.main()
