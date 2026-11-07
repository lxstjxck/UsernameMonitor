import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors

from username_monitor.__main__ import one_cycle
from username_monitor.store import Store
from username_monitor.telegram_api import check_username


NAME = "testusername12345"


class TestTelegramResults(unittest.IsolatedAsyncioTestCase):
    async def test_username_invalid_is_classified(self):
        client = AsyncMock(side_effect=errors.UsernameInvalidError(request=None))
        self.assertEqual(await check_username(client, NAME), ("invalid", "USERNAME_INVALID"))

    async def test_other_known_username_errors_keep_their_status(self):
        for error_class, expected in (
            (errors.UsernameOccupiedError, "occupied"),
            (errors.UsernamePurchaseAvailableError, "fragment"),
        ):
            with self.subTest(error_class=error_class):
                client = AsyncMock(side_effect=error_class(request=None))
                self.assertEqual((await check_username(client, NAME))[0], expected)

    async def test_flood_wait_propagates(self):
        client = AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=45))
        with self.assertRaises(errors.FloodWaitError):
            await check_username(client, NAME)

    async def test_unknown_rpc_error_propagates(self):
        client = AsyncMock(side_effect=errors.RPCError(request=None, message="UNEXPECTED", code=400))
        with self.assertRaises(errors.RPCError):
            await check_username(client, NAME)


class TestCheckCycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = Store(Path(self.temp_dir.name) / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.config = SimpleNamespace(
            usernames_path=Path(self.temp_dir.name) / "names.txt",
            check_timeout_seconds=0.01,
            confirm_delay_seconds=0,
            cycle_pause_seconds=600,
            delay_seconds=0,
            bot_token="",
            chat_id="",
            bot_proxy_url=None,
            bot_proxy_secret=None,
            alert_first_free=True,
        )
        self.config.usernames_path.write_text(NAME + "\n")
        self.names_patch = patch("username_monitor.__main__.load_names", return_value=([NAME], []))
        self.names_patch.start()
        self.addCleanup(self.names_patch.stop)

    async def test_timeout_cancels_request_and_preserves_status(self):
        self.store.record(NAME, "occupied")
        cancelled = asyncio.Event()

        async def stalled_client(_request):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        with patch("username_monitor.notify.notify", new_callable=AsyncMock) as notify:
            pause = await one_cycle(stalled_client, self.config, self.store, 1)
        self.assertEqual(pause, 600)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.store.summary()[0][1], "occupied")
        self.assertEqual(self.store.summary()[0][3], "TimeoutError")
        notify.assert_not_awaited()

    async def test_second_free_check_must_complete_before_notification(self):
        calls = 0

        async def first_free_then_stall(_request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return True
            await asyncio.sleep(10)

        with patch("username_monitor.notify.notify", new_callable=AsyncMock) as notify:
            await one_cycle(first_free_then_stall, self.config, self.store, 1)
        self.assertEqual(calls, 2)
        self.assertEqual(self.store.summary()[0][1], "unknown")
        self.assertEqual(self.store.summary()[0][3], "TimeoutError")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
        notify.assert_not_awaited()

    async def test_two_successful_free_checks_can_notify(self):
        client = AsyncMock(return_value=True)
        with patch("username_monitor.notify.notify", new_callable=AsyncMock) as notify:
            await one_cycle(client, self.config, self.store, 1)
        self.assertEqual(client.await_count, 2)
        self.assertEqual(self.store.summary()[0][1], "free")
        notify.assert_awaited_once()
        with patch("username_monitor.notify.notify", new_callable=AsyncMock) as second_notify:
            await one_cycle(client, self.config, self.store, 1)
            second_notify.assert_not_awaited()
        self.assertEqual(self.store.db.execute("SELECT status,COUNT(*) FROM notification_outbox").fetchone(),
                         ("delivered", 1))

    async def test_failed_delivery_is_not_logged_as_success(self):
        client = AsyncMock(return_value=True)
        with patch("username_monitor.notify.notify", new_callable=AsyncMock, return_value=False) as notify:
            with self.assertLogs("username_monitor", level="WARNING") as captured:
                await one_cycle(client, self.config, self.store, 1)
        self.assertEqual(self.store.summary()[0][1], "free")
        notify.assert_awaited_once()
        self.assertIn("уведомление не доставлено", " ".join(captured.output))

    async def test_flood_wait_stops_cycle_for_telegram_delay(self):
        client = AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=700))
        with patch("username_monitor.notify.notify", new_callable=AsyncMock) as notify:
            pause = await one_cycle(client, self.config, self.store, 1)
        self.assertEqual(pause, 760)
        self.assertEqual(self.store.summary()[0][1], "unknown")
        self.assertEqual(self.store.summary()[0][3], "FloodWait")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
        notify.assert_not_awaited()

    async def test_unknown_rpc_error_is_temporary(self):
        client = AsyncMock(side_effect=errors.RPCError(request=None, message="UNEXPECTED", code=400))
        with patch("username_monitor.notify.notify", new_callable=AsyncMock) as notify:
            await one_cycle(client, self.config, self.store, 1)
        self.assertEqual(self.store.summary()[0][1], "unknown")
        self.assertEqual(self.store.summary()[0][3], "RPCError")
        notify.assert_not_awaited()

    async def test_external_cancellation_propagates(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def stalled_client(_request):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        task = asyncio.create_task(one_cycle(stalled_client, self.config, self.store, 1))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.store.summary()[0][1], "unknown")


if __name__ == "__main__":
    unittest.main()
