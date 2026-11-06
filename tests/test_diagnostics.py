import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, functions

from username_monitor.__main__ import main
from username_monitor.diagnostics import diagnose


USERNAME = "testusername12345"


class FakeClient:
    def __init__(self):
        self.calls = []
        self.connect = AsyncMock()
        self.is_user_authorized = AsyncMock(return_value=True)
        self.disconnect = AsyncMock()
        self.config_result = object()
        self.username_result = True

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, functions.help.GetConfigRequest):
            if isinstance(self.config_result, Exception):
                raise self.config_result
            if asyncio.iscoroutine(self.config_result):
                return await self.config_result
            return self.config_result
        if isinstance(self.username_result, Exception):
            raise self.username_result
        if asyncio.iscoroutine(self.username_result):
            return await self.username_result
        return self.username_result


class TestDiagnose(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        session_path = Path(self.temp.name) / "telegram"
        Path(f"{session_path}.session").touch()
        self.config = SimpleNamespace(session_path=session_path, check_timeout_seconds=0.01)
        self.client = FakeClient()
        self.factory = Mock(return_value=self.client)

    async def run_diagnose(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = await diagnose(self.config, USERNAME, self.factory)
        return code, output.getvalue()

    async def test_success_uses_existing_session_and_ordered_rpc_probes(self):
        code, output = await self.run_diagnose()
        self.assertEqual(code, 0)
        self.factory.assert_called_once_with(self.config, flood_sleep_threshold=0)
        self.client.connect.assert_awaited_once()
        self.client.is_user_authorized.assert_awaited_once()
        self.assertIsInstance(self.client.calls[0], functions.help.GetConfigRequest)
        self.assertIsInstance(self.client.calls[1], functions.account.CheckUsernameRequest)
        self.assertEqual(self.client.calls[1].username, USERNAME)
        self.client.disconnect.assert_awaited_once()
        self.assertLess(output.index("connect:"), output.index("is_user_authorized:"))
        self.assertLess(output.index("is_user_authorized:"), output.index("help.GetConfigRequest:"))
        self.assertLess(output.index("help.GetConfigRequest:"), output.index("account.CheckUsernameRequest:"))
        self.assertIn(" с;", output)
        self.assertNotIn(USERNAME, output)
        self.assertNotIn(str(self.config.session_path), output)

    async def test_missing_session_does_not_create_client(self):
        Path(f"{self.config.session_path}.session").unlink()
        code, output = await self.run_diagnose()
        self.assertEqual(code, 1)
        self.factory.assert_not_called()
        self.assertIn("FileNotFoundError", output)
        self.assertNotIn(str(self.config.session_path), output)

    async def test_connect_failure_stops_rpc_probes_and_redacts_exception(self):
        self.client.connect.side_effect = RuntimeError("secret and phone")
        code, output = await self.run_diagnose()
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("secret and phone", output)
        self.assertEqual(self.client.calls, [])
        self.client.disconnect.assert_awaited_once()

    async def test_connect_timeout_cancels_connect_and_stops(self):
        cancelled = asyncio.Event()

        async def stalled_connect():
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        self.client.connect.side_effect = stalled_connect
        with patch("username_monitor.diagnostics.CONNECT_TIMEOUT_SECONDS", 0.01):
            code, output = await self.run_diagnose()
        self.assertEqual(code, 1)
        self.assertTrue(cancelled.is_set())
        self.assertIn("connect:", output)
        self.assertIn("TimeoutError", output)
        self.assertEqual(self.client.calls, [])

    async def test_auth_failure_does_not_hide_later_probe_results(self):
        self.client.is_user_authorized.return_value = False
        code, output = await self.run_diagnose()
        self.assertEqual(code, 1)
        self.assertIn("не авторизована", output)
        self.assertEqual(len(self.client.calls), 2)

    async def test_auth_timeout_does_not_block_later_probes(self):
        cancelled = asyncio.Event()

        async def stalled_auth():
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        self.client.is_user_authorized.side_effect = stalled_auth
        with patch("username_monitor.diagnostics.AUTH_TIMEOUT_SECONDS", 0.01):
            code, output = await self.run_diagnose()
        self.assertEqual(code, 1)
        self.assertTrue(cancelled.is_set())
        self.assertIn("is_user_authorized:", output)
        self.assertIn("TimeoutError", output)
        self.assertEqual(len(self.client.calls), 2)

    async def test_each_rpc_has_own_timeout_and_cancellation(self):
        cancelled = asyncio.Event()

        async def stalled():
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        self.client.config_result = stalled()
        with patch("username_monitor.diagnostics.CONFIG_TIMEOUT_SECONDS", 0.01):
            code, output = await self.run_diagnose()
        self.assertEqual(code, 1)
        self.assertTrue(cancelled.is_set())
        self.assertIn("help.GetConfigRequest: ", output)
        self.assertIn("TimeoutError", output)
        self.assertIn("account.CheckUsernameRequest:", output)

    async def test_username_timeout_is_separate_from_config_rpc(self):
        cancelled = asyncio.Event()

        async def stalled():
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        self.client.username_result = stalled()
        code, output = await self.run_diagnose()
        self.assertEqual(code, 1)
        self.assertTrue(cancelled.is_set())
        self.assertIn("help.GetConfigRequest:", output)
        self.assertIn("account.CheckUsernameRequest:", output)
        self.assertIn("TimeoutError", output)

    async def test_known_username_rpc_error_identifies_working_transport(self):
        self.client.username_result = errors.UsernameInvalidError(request=None)
        code, output = await self.run_diagnose()
        self.assertEqual(code, 0)
        self.assertIn("UsernameInvalidError", output)
        self.assertIn("Telegram отклонил username", output)
        self.assertNotIn(USERNAME, output)

    async def test_flood_wait_reports_safe_pause(self):
        self.client.username_result = errors.FloodWaitError(request=None, capture=45)
        code, output = await self.run_diagnose()
        self.assertEqual(code, 1)
        self.assertIn("FloodWaitError", output)
        self.assertIn("45 с", output)
        self.assertNotIn(USERNAME, output)

    async def test_disconnect_failure_is_reported(self):
        self.client.disconnect.side_effect = OSError("private session data")
        code, output = await self.run_diagnose()
        self.assertEqual(code, 1)
        self.assertIn("disconnect:", output)
        self.assertIn("OSError", output)
        self.assertNotIn("private session data", output)


class TestDiagnoseCli(unittest.TestCase):
    def test_cli_dispatches_normalized_username(self):
        config = object()
        with patch("sys.argv", ["username_monitor", "diagnose", "--username", "@TestUsername12345"]), \
                patch("username_monitor.__main__.load_config", return_value=config), \
                patch("username_monitor.diagnostics.diagnose", new_callable=AsyncMock, return_value=0) as probe:
            self.assertEqual(main(), 0)
        self.assertEqual(probe.await_args.args[:2], (config, USERNAME))

    def test_cli_requires_valid_username_without_echoing_input(self):
        with patch("sys.argv", ["username_monitor", "diagnose", "--username", "secret-invalid-name"]), \
                patch("username_monitor.__main__.load_config", return_value=object()), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit):
                main()
        self.assertNotIn("secret-invalid-name", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
