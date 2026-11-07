"""Shared TelegramClient settings remain the same for login and monitoring."""
import asyncio
import base64
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tgnet.connection.faketls import TgNetConnectionTls

from username_monitor.__main__ import create_telegram_client, login, monitor
from username_monitor.config import load_config


SECRET_B64 = base64.b64encode(bytes.fromhex("ee" + "a1" * 16) + b"a.com").decode()
BASE_ENV = {"TG_API_ID": "12345", "TG_API_HASH": "test-hash",
            "TG_MTPROXY_HOST": "45.130.124.28", "TG_MTPROXY_PORT": "443",
            "TG_MTPROXY_SECRET_B64": SECRET_B64}
RETRY_OPTIONS = {"request_retries": 1, "connection_retries": 2, "retry_delay": 1}


class TestTelegramClientConfig(unittest.TestCase):
    def config(self, **values):
        with patch.dict(os.environ, BASE_ENV | values, clear=True), patch("username_monitor.config.load_dotenv"):
            return load_config()

    def test_faketls_connection_and_bounded_retries(self):
        config = self.config()
        self.assertEqual(config.check_timeout_seconds, 30)
        with patch("telethon.TelegramClient") as client_class:
            create_telegram_client(config, flood_sleep_threshold=0)
            args = client_class.call_args
        self.assertEqual(args.args, (str(config.session_path), 12345, "test-hash"))
        self.assertEqual({key: args.kwargs[key] for key in RETRY_OPTIONS}, RETRY_OPTIONS)
        self.assertEqual(args.kwargs["flood_sleep_threshold"], 0)
        self.assertTrue(issubclass(args.kwargs["connection"], TgNetConnectionTls))
        self.assertNotIn("proxy", args.kwargs)

    def test_login_and_monitor_use_same_endpoint(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as root:
            config = SimpleNamespace(**(vars(config) | {"session_path": Path(root) / "session"}))
            client = Mock(start=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=42)),
                          disconnect=AsyncMock(), connect=AsyncMock(),
                          is_user_authorized=AsyncMock(return_value=False))
            with patch("telethon.TelegramClient", return_value=client) as client_class:
                with patch("builtins.print"):
                    asyncio.run(login(config))
                with self.assertRaises(RuntimeError):
                    asyncio.run(monitor(config, once=True, limit=1))
            self.assertEqual(client_class.call_count, 2)
            loggers = {"telethon.network.connection.connection": logging.getLogger("test")}
            for call in client_class.call_args_list:
                options = call.kwargs
                self.assertNotIn("proxy", options)
                self.assertEqual({key: options[key] for key in RETRY_OPTIONS}, RETRY_OPTIONS)
                connection = options["connection"]("telegram.dc", 443, 2, loggers=loggers)
                self.assertEqual((connection._ip, connection._port, connection._dc_id),
                                 (config.mtproxy_host, config.mtproxy_port, 2))
            self.assertEqual(client_class.call_args_list[1].kwargs["flood_sleep_threshold"], 0)

    def test_check_timeout_is_configurable_and_validated(self):
        self.assertEqual(self.config(CHECK_TIMEOUT_SECONDS="2.5").check_timeout_seconds, 2.5)
        for value in ("0", "-1", "86401"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "CHECK_TIMEOUT_SECONDS"):
                self.config(CHECK_TIMEOUT_SECONDS=value)


if __name__ == "__main__":
    unittest.main()
