import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError

from telethon import errors

from username_monitor.__main__ import _check_measured, main, one_cycle
from username_monitor.outbox import deliver_due, wait_with_outbox
from username_monitor.store import Store
from username_monitor.telemetry import Telemetry, _percentile, read_report


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 7, tzinfo=timezone.utc)
        self.monotonic = 0.0

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)
        self.monotonic += seconds


def response():
    value = MagicMock()
    value.status = 200
    value.read.return_value = b'{"ok":true}'
    value.__enter__.return_value = value
    return value


class TelemetryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "monitor.sqlite3"
        self.names = Path(self.temp.name) / "usernames.txt"
        self.names.write_text("first123\nsecond123\n")
        self.clock = Clock()
        self.config = SimpleNamespace(
            usernames_path=self.names, check_timeout_seconds=30,
            confirm_delay_seconds=10, delay_seconds=30, cycle_pause_seconds=600,
            alert_first_free=True, bot_token="", chat_id="123",
            bot_proxy_url="https://worker.example", bot_proxy_secret="private-token")

    def report(self, hours=24):
        return read_report(self.path, hours, clock=self.clock.now)

    async def test_success_and_confirmation_include_rpc_but_exclude_delays(self):
        async def check(_client, _name):
            self.clock.advance(.120)
            return "free", ""

        async def sleep(seconds):
            self.clock.advance(seconds)

        with Store(self.path, clock=self.clock.now) as store, Telemetry(self.path, clock=self.clock.now) as metrics, \
                patch("username_monitor.telegram_api.check_username", side_effect=check), \
                patch("username_monitor.__main__.asyncio.sleep", side_effect=sleep), \
                patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock):
            # Restrict to one name; confirmation sleep must not enter either RPC duration.
            self.names.write_text("first123\n")
            self.assertEqual(await one_cycle(object(), self.config, store, 1, metrics,
                                             monotonic=lambda: self.clock.monotonic), 600)
            self.assertEqual(store.summary()[0][1], "free")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
        report = self.report()
        self.assertEqual((report["rpc"]["primary"], report["rpc"]["confirmation"]), (1, 1))
        self.assertEqual((report["rpc"]["successful"], report["cycles_completed"]), (2, 1))
        self.assertEqual(report["rpc"]["p50_ms"], 120)
        self.assertEqual(report["rpc"]["results"]["free"], 2)

    async def test_flood_wait_timeout_rpc_network_and_other(self):
        outcomes = [errors.FloodWaitError(request=None, capture=100), asyncio.TimeoutError(),
                    errors.RPCError(request=None, message="private"), OSError("private"),
                    RuntimeError("private")]
        with Store(self.path), Telemetry(self.path, clock=self.clock.now) as metrics, \
                patch("username_monitor.telegram_api.check_username", new_callable=AsyncMock,
                      side_effect=outcomes):
            for error in outcomes:
                with self.assertRaises(type(error)):
                    await _check_measured(object(), "first123", 2, "primary", metrics,
                                          monotonic=iter([0, .1]).__next__)
        r = self.report()["rpc"]
        self.assertEqual(r["failed"], 5)
        self.assertEqual((r["flood_wait_count"], r["max_flood_wait_seconds"]), (1, 100))
        self.assertEqual((r["timeout_count"], r["network_error_count"]), (1, 1))
        self.assertEqual((r["results"]["rpc_error"], r["results"]["other_error"]), (1, 1))

    async def test_failed_telemetry_cannot_invent_free_or_block_next_name(self):
        broken = SimpleNamespace(rpc=MagicMock(side_effect=OSError("private token")),
                                 cycle=MagicMock(side_effect=OSError("private token")))
        with Store(self.path) as store, \
                patch("username_monitor.telegram_api.check_username", new_callable=AsyncMock,
                      side_effect=[("occupied", ""), ("unavailable", "")]), \
                patch("username_monitor.__main__.asyncio.sleep", new_callable=AsyncMock), \
                self.assertLogs("username_monitor", level="WARNING") as logs:
            self.assertEqual(await one_cycle(object(), self.config, store, 2, broken), 600)
            self.assertEqual({r[0]: r[1] for r in store.summary()},
                             {"first123": "occupied", "second123": "unavailable"})
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
        self.assertNotIn("private token", " ".join(logs.output))

    async def test_sqlite_metrics_write_failure_preserves_confirmed_free_and_outbox(self):
        self.names.write_text("first123\n")
        with Store(self.path) as store, Telemetry(self.path) as metrics:
            metrics.db.execute("""CREATE TRIGGER fail_rpc BEFORE INSERT ON rpc_metrics
                BEGIN SELECT RAISE(ABORT, 'private synthetic token'); END""")
            metrics.db.commit()
            with patch("username_monitor.telegram_api.check_username", new_callable=AsyncMock,
                       side_effect=[("free", ""), ("free", "")]), \
                    patch("username_monitor.__main__.asyncio.sleep", new_callable=AsyncMock), \
                    patch("username_monitor.outbox.notify_module.notify", new_callable=AsyncMock,
                          return_value=False), \
                    self.assertLogs("username_monitor", level="WARNING") as logs:
                await one_cycle(object(), self.config, store, 1, metrics)
            self.assertEqual(store.summary()[0][1], "free")
            self.assertEqual(store.db.execute(
                "SELECT status FROM notification_outbox").fetchone()[0], "pending")
            self.assertNotIn("private synthetic token", " ".join(logs.output))

    async def test_http_success_failure_retry_and_durable_state(self):
        with Store(self.path, clock=self.clock.now) as store, Telemetry(self.path, clock=self.clock.now) as metrics:
            store.record("first123", "free", alert_first_free=True)
            self.names.write_text("first123\n")
            opener = MagicMock()
            opener.open.side_effect = HTTPError("https://worker.example", 502, "private body", {}, None)
            with patch("username_monitor.notify.request.build_opener", return_value=opener), \
                    self.assertLogs("username_monitor", level="WARNING") as logs:
                self.assertEqual(await deliver_due(self.config, store, metrics), 1)
            self.assertNotIn("private body", " ".join(logs.output))
            self.assertNotIn("private-token", " ".join(logs.output))
            self.assertEqual(store.db.execute("SELECT status,attempts FROM notification_outbox").fetchone(),
                             ("pending", 1))
            self.clock.advance(30)
            opener.open.side_effect = None
            opener.open.return_value = response()
            with patch("username_monitor.notify.request.build_opener", return_value=opener):
                self.assertEqual(await deliver_due(self.config, store, metrics), 1)
            self.assertEqual(store.db.execute("SELECT status,attempts FROM notification_outbox").fetchone(),
                             ("delivered", 2))
        d = self.report()["delivery"]
        self.assertEqual((d["successful"], d["failed"], d["retries"]), (1, 1, 1))
        self.assertEqual(d["average_latency_ms"], 30000)
        self.assertIsNotNone(d["http_p95_ms"])
        self.assertEqual(self.report()["outbox"]["delivered"], 1)
        self.assertEqual(self.report()["rpc"]["checks"], 0)

    async def test_no_http_means_no_metric_and_direct_bot_api(self):
        with Store(self.path, clock=self.clock.now) as store, Telemetry(self.path, clock=self.clock.now) as metrics:
            self.names.write_text("first123\n")
            store.record("first123", "free", alert_first_free=True)
            self.config.bot_proxy_url = None
            self.config.bot_token = ""
            with self.assertLogs("username_monitor", level="WARNING"):
                await deliver_due(self.config, store, metrics)
            self.assertEqual(self.report()["delivery"]["failed"], 0)
            self.clock.advance(30)
            self.config.bot_token = "private-bot-token"
            with patch("username_monitor.notify.request.urlopen", return_value=response()) as urlopen:
                await deliver_due(self.config, store, metrics)
            self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(self.report()["delivery"]["successful"], 1)

    async def test_retry_during_six_hundred_second_pause_never_opens_mtproto(self):
        with Store(self.path, clock=self.clock.now) as store, Telemetry(self.path, clock=self.clock.now) as metrics:
            store.record("first123", "free", alert_first_free=True)
            self.names.write_text("first123\n")
            opener = MagicMock()
            opener.open.side_effect = [HTTPError("private", 429, "private", {}, None), response()]

            async def sleep(seconds):
                self.clock.advance(seconds)

            with patch("username_monitor.notify.request.build_opener", return_value=opener), \
                    patch("username_monitor.__main__.create_telegram_client", side_effect=AssertionError("MTProto")):
                await wait_with_outbox(self.config, store, 600, sleep=sleep,
                                       monotonic=lambda: self.clock.monotonic, telemetry=metrics)
        self.assertEqual(self.clock.monotonic, 600)
        self.assertEqual(self.report()["delivery"]["successful"], 1)

    async def test_percentiles_retention_migration_restart_and_read_only_old_db(self):
        self.assertEqual(_percentile([10, 20, 30, 40], 50), 25)
        self.assertEqual(_percentile([10, 20, 30, 40], 95), 38.5)
        self.assertIsNone(_percentile([], 95))
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE state (username TEXT PRIMARY KEY, status TEXT)")
            db.execute("CREATE TABLE changes (id INTEGER PRIMARY KEY, username TEXT)")
            db.execute("INSERT INTO state VALUES ('first123','occupied')")
            db.execute("INSERT INTO changes(username) VALUES ('first123')")
        before = self.path.stat().st_mtime_ns
        old = self.report()
        self.assertEqual(old["rpc"]["checks"], 0)
        self.assertIsNone(old["rpc"]["p95_ms"])
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        with Telemetry(self.path, clock=self.clock.now) as metrics:
            metrics.rpc(self.clock.now().isoformat(timespec="seconds"), 20, "primary", "free")
            metrics.delivery(self.clock.now().isoformat(timespec="seconds"), 20, True, False, 5000)
        with Telemetry(self.path, clock=self.clock.now) as reopened:
            reopened.rpc(self.clock.now().isoformat(timespec="seconds"), 40, "confirmation", "occupied")
        self.assertEqual(self.report()["rpc"]["p50_ms"], 30)
        self.assertEqual(self.report()["delivery"]["p95_latency_ms"], 5000)
        self.assertEqual(self.report()["rpc"]["checks"], 2)
        self.clock.advance(15 * 86400)
        with Telemetry(self.path, clock=self.clock.now):
            pass
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rpc_metrics").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM state").fetchone()[0], "occupied")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 1)

    async def test_cli_json_without_secrets_client_or_writes(self):
        with Store(self.path) as store:
            store.record("first123", "occupied")
        before = self.path.stat().st_mtime_ns
        output = io.StringIO()
        with patch.dict(os.environ, {"DB_PATH": str(self.path), "TG_MTPROXY_SECRET_B64": "private"}), \
                patch("sys.argv", ["username_monitor", "metrics-status", "--hours", "24", "--json"]), \
                patch("username_monitor.__main__.load_config", side_effect=AssertionError("config")), \
                patch("username_monitor.__main__.create_telegram_client", side_effect=AssertionError("client")), \
                redirect_stdout(output):
            self.assertEqual(main(), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["hours"], 24)
        self.assertIsNone(data["rpc"]["p95_ms"])
        self.assertNotIn("private", output.getvalue())
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        output = io.StringIO()
        with patch.dict(os.environ, {"DB_PATH": str(self.path)}), \
                patch("sys.argv", ["username_monitor", "metrics-status"]), \
                redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn("no data", output.getvalue())


if __name__ == "__main__":
    unittest.main()
