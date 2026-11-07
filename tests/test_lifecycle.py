import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors

from username_monitor.__main__ import OnDemandTelegramClient, monitor, one_cycle
from username_monitor.store import Store
from username_monitor.transport_probe import verify_transport


class SessionClient:
    def __init__(self, responses=None):
        self.responses = iter(responses or [])
        self.active = False
        self.connects = 0
        self.disconnects = 0
        self.requests = 0

    async def connect(self):
        if self.active:
            raise AssertionError("parallel connection to one session")
        self.active = True
        self.connects += 1

    async def disconnect(self):
        self.active = False
        self.disconnects += 1

    async def is_user_authorized(self):
        return True

    async def __call__(self, request):
        if not self.active:
            raise AssertionError("RPC without connection")
        self.requests += 1
        response = next(self.responses, True)
        if isinstance(response, Exception):
            raise response
        return response


@dataclass
class ProbeConfig:
    session_path: Path
    check_timeout_seconds: float


class TestOnDemandLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = SimpleNamespace(
            session_path=Path(self.temp.name) / "telegram",
            db_path=Path(self.temp.name) / "monitor.sqlite3",
            usernames_path=Path(self.temp.name) / "usernames.txt",
            check_timeout_seconds=0.02, confirm_delay_seconds=10,
            delay_seconds=30, cycle_pause_seconds=600, bot_token="",
            chat_id="", bot_proxy_url=None, bot_proxy_secret=None,
            alert_first_free=False,
        )

    async def test_rpc_25_seconds_idle_then_rpc_with_no_idle_connection(self):
        client = SessionClient([False, False])
        on_demand = OnDemandTelegramClient(client)
        await on_demand(object())
        self.assertFalse(client.active)
        # Represents 25 seconds of no requests, including a proxy that
        # would close an open connection after 10 seconds.
        async def idle_25(seconds):
            self.assertEqual(seconds, 25)
            self.assertFalse(client.active)

        with patch("username_monitor.__main__.asyncio.sleep", side_effect=idle_25):
            await asyncio.sleep(25)
        await on_demand(object())
        self.assertEqual((client.connects, client.disconnects, client.requests), (2, 2, 2))
        self.assertFalse(client.active)

    async def test_free_confirmation_and_other_names_disconnect_during_delays(self):
        names = ["first123", "second123", "third123"]
        client = SessionClient([True, True, False, False])
        sleeps = []

        async def fast_sleep(seconds):
            self.assertFalse(client.active, "idle connection during pause")
            sleeps.append(seconds)

        with Store(self.config.db_path) as store, \
                patch("username_monitor.__main__.load_names", return_value=(names, [])), \
                patch("username_monitor.__main__.asyncio.sleep", side_effect=fast_sleep), \
                patch("username_monitor.notify.notify", new_callable=AsyncMock) as notify:
            pause = await one_cycle(OnDemandTelegramClient(client), self.config, store, 3)
            self.assertEqual({row[0]: row[1] for row in store.summary()},
                             {"first123": "free", "second123": "unavailable", "third123": "unavailable"})
            notify.assert_not_awaited()  # alert_first_free=False
        self.assertEqual(pause, 600)
        self.assertEqual(sleeps, [10, 30, 30])
        self.assertEqual((client.connects, client.disconnects), (4, 4))

    async def test_connect_failure_is_recorded_and_next_name_can_reconnect(self):
        client = SessionClient([False])
        original_connect = client.connect
        attempts = 0

        async def flaky_connect():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("private endpoint")
            await original_connect()

        client.connect = flaky_connect
        with Store(self.config.db_path) as store, \
                patch("username_monitor.__main__.load_names", return_value=(["first123", "second123"], [])), \
                patch("username_monitor.__main__.asyncio.sleep", new_callable=AsyncMock), \
                self.assertLogs("username_monitor", level="WARNING") as logs:
            await one_cycle(OnDemandTelegramClient(client), self.config, store, 2)
            rows = {row[0]: row for row in store.summary()}
            self.assertEqual(rows["first123"][3], "OSError")
            self.assertEqual(rows["second123"][1], "unavailable")
        self.assertNotIn("private endpoint", " ".join(logs.output))
        self.assertEqual(attempts, 2)
        self.assertFalse(client.active)

    async def test_rpc_timeout_cancels_and_disconnects(self):
        client = SessionClient()
        cancelled = asyncio.Event()

        async def stall(_client, _request):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        with patch.object(SessionClient, "__call__", stall), \
                Store(self.config.db_path) as store, \
                patch("username_monitor.__main__.load_names", return_value=(["first123"], [])):
            pause = await one_cycle(OnDemandTelegramClient(client), self.config, store, 1)
            self.assertEqual(pause, 600)
            self.assertEqual(store.summary()[0][3], "TimeoutError")
        self.assertTrue(cancelled.is_set())
        self.assertFalse(client.active)

    async def test_network_failure_during_rpc_waits_before_next_name(self):
        client = SessionClient([OSError("private endpoint"), False])
        delays = []

        async def pause(seconds):
            self.assertFalse(client.active)
            delays.append(seconds)

        with Store(self.config.db_path) as store, \
                patch("username_monitor.__main__.load_names", return_value=(["first123", "second123"], [])), \
                patch("username_monitor.__main__.asyncio.sleep", side_effect=pause):
            result = await one_cycle(OnDemandTelegramClient(client), self.config, store, 2)
            rows = {row[0]: row for row in store.summary()}
        self.assertEqual((result, delays), (600, [30]))
        self.assertEqual(rows["first123"][3], "OSError")
        self.assertEqual(rows["second123"][1], "unavailable")
        self.assertEqual((client.connects, client.disconnects), (2, 2))

    async def test_flood_wait_disconnects_and_preserves_cooldown(self):
        client = SessionClient([errors.FloodWaitError(request=None, capture=700)])
        with Store(self.config.db_path) as store, \
                patch("username_monitor.__main__.load_names", return_value=(["first123"], [])):
            pause = await one_cycle(OnDemandTelegramClient(client), self.config, store, 1)
            self.assertEqual(store.summary()[0][3], "FloodWait")
        self.assertEqual(pause, 760)
        self.assertFalse(client.active)

    async def test_systemd_stop_cancels_rpc_and_disconnects(self):
        client = SessionClient()
        entered = asyncio.Event()

        async def stall(_client, _request):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(SessionClient, "__call__", stall):
            task = asyncio.create_task(OnDemandTelegramClient(client)(object()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(client.active)
        self.assertEqual(client.disconnects, 1)

    async def test_overlapping_calls_cannot_open_parallel_connections(self):
        client = SessionClient([False, False])
        entered = asyncio.Event()
        release = asyncio.Event()
        original_call = client.__class__.__call__

        async def slow_call(instance, request):
            entered.set()
            await release.wait()
            return await original_call(instance, request)

        with patch.object(SessionClient, "__call__", slow_call):
            on_demand = OnDemandTelegramClient(client)
            first = asyncio.create_task(on_demand(object()))
            await entered.wait()
            second = asyncio.create_task(on_demand(object()))
            await asyncio.sleep(0)
            self.assertEqual(client.connects, 1)
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual((client.connects, client.disconnects), (2, 2))
        self.assertFalse(client.active)

    async def test_cycle_pause_has_no_live_connection(self):
        client = SessionClient()
        pauses = []

        async def stop_after_pause(seconds):
            pauses.append(seconds)
            self.assertFalse(client.active)
            raise asyncio.CancelledError()

        with patch("username_monitor.__main__.create_telegram_client", return_value=client), \
                patch("username_monitor.__main__.one_cycle", new_callable=AsyncMock, return_value=600) as cycle, \
                patch("username_monitor.__main__.asyncio.sleep", side_effect=stop_after_pause):
            with self.assertRaises(asyncio.CancelledError):
                await monitor(self.config, once=False, limit=3)
        self.assertEqual(pauses, [600])
        self.assertIsInstance(cycle.call_args.args[0], OnDemandTelegramClient)
        self.assertFalse(client.active)


class TestIdleProbe(unittest.IsolatedAsyncioTestCase):
    async def test_copy_session_idle_and_periodic_patterns(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "telegram.session"
            import sqlite3
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE data (x INTEGER)")
            config = ProbeConfig(session_path=source, check_timeout_seconds=1)
            clients = []

            def create(cfg, **_):
                self.assertNotEqual(cfg.session_path, source)
                client = SessionClient()
                client.get_me = AsyncMock(return_value=object())
                clients.append(client)
                return client

            async def fast_sleep(seconds):
                self.assertIn(seconds, (5, 25))
                self.assertTrue(clients[-1].active)

            output = io.StringIO()
            with redirect_stdout(output), patch("username_monitor.transport_probe.asyncio.sleep", side_effect=fast_sleep):
                result = await verify_transport(config, create, idle_probe=True)
            self.assertEqual(result, 0, output.getvalue())
            self.assertIn("idle 25s + RPC: PASS", output.getvalue())
            self.assertIn("periodic RPC every 5s: PASS", output.getvalue())
            self.assertEqual([c.requests for c in clients], [2, 5])
            self.assertTrue(all(not c.active for c in clients))

    async def test_idle_rpc_failure_still_checks_periodic_traffic(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "telegram.session"
            import sqlite3
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE data (x INTEGER)")
            clients = []

            def create(cfg, **_):
                client = SessionClient([True, OSError("private secret")] if not clients else [True] * 5)
                client.get_me = AsyncMock(return_value=object())
                clients.append(client)
                return client

            output = io.StringIO()
            with redirect_stdout(output), patch("username_monitor.transport_probe.asyncio.sleep", new_callable=AsyncMock):
                result = await verify_transport(ProbeConfig(source, 1), create, idle_probe=True)
            self.assertEqual(result, 1)
            self.assertIn("idle 25s + RPC: FAIL (OSError)", output.getvalue())
            self.assertIn("periodic RPC every 5s: PASS", output.getvalue())
            self.assertNotIn("private secret", output.getvalue())
            self.assertTrue(all(not c.active for c in clients))


if __name__ == "__main__":
    unittest.main()
