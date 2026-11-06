import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate

from username_monitor.__main__ import create_telegram_client, login, monitor
from username_monitor.config import load_config


SECRET = "a1" * 16
BASE_ENV = {"TG_API_ID": "12345", "TG_API_HASH": "test-hash"}


class TestProxyConfig(unittest.TestCase):
    def config(self, **proxy_values):
        with patch.dict(os.environ, BASE_ENV | proxy_values, clear=True), patch("username_monitor.config.load_dotenv"):
            return load_config()

    def test_no_proxy_preserves_direct_connection(self):
        config = self.config()
        with patch("telethon.TelegramClient") as client_class:
            create_telegram_client(config)
            client_class.assert_called_once_with(str(config.session_path), 12345, "test-hash")

    def test_mtproxy_connection(self):
        config = self.config(TG_PROXY_HOST="proxy.example", TG_PROXY_PORT="443", TG_PROXY_SECRET="DD" + SECRET.upper())
        with patch("telethon.TelegramClient") as client_class:
            create_telegram_client(config, flood_sleep_threshold=0)
            client_class.assert_called_once_with(
                str(config.session_path), 12345, "test-hash",
                connection=ConnectionTcpMTProxyRandomizedIntermediate,
                proxy=("proxy.example", 443, "dd" + SECRET),
                flood_sleep_threshold=0,
            )
        self.assertNotIn(SECRET, repr(config))

    def test_login_and_monitor_use_same_connection(self):
        config = self.config(TG_PROXY_HOST="proxy.example", TG_PROXY_PORT="443", TG_PROXY_SECRET=SECRET)
        with tempfile.TemporaryDirectory() as root:
            config = SimpleNamespace(**(vars(config) | {"session_path": Path(root) / "session"}))
            client = Mock()
            client.start = AsyncMock()
            client.get_me = AsyncMock(return_value=SimpleNamespace(id=42))
            client.disconnect = AsyncMock()
            client.connect = AsyncMock()
            client.is_user_authorized = AsyncMock(return_value=False)
            with patch("telethon.TelegramClient", return_value=client) as client_class:
                with patch("builtins.print"):
                    asyncio.run(login(config))
                with self.assertRaises(RuntimeError):
                    asyncio.run(monitor(config, once=True, limit=1))
                self.assertEqual(client_class.call_count, 2)
                login_options = client_class.call_args_list[0].kwargs
                monitor_options = client_class.call_args_list[1].kwargs
                self.assertEqual(login_options["connection"], monitor_options["connection"])
                self.assertEqual(login_options["proxy"], monitor_options["proxy"])
                self.assertEqual(monitor_options["flood_sleep_threshold"], 0)

    def test_incomplete_proxy_configuration_is_rejected(self):
        for values in (
            {"TG_PROXY_HOST": "proxy.example"},
            {"TG_PROXY_HOST": "proxy.example", "TG_PROXY_PORT": "443"},
        ):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "TG_PROXY_HOST"):
                self.config(**values)

    def test_invalid_ports_and_secrets_are_rejected_without_echoing_secret(self):
        for port in ("0", "65536", "abc", "1.5"):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, "TG_PROXY_PORT"):
                self.config(TG_PROXY_HOST="proxy.example", TG_PROXY_PORT=port, TG_PROXY_SECRET=SECRET)
        for secret in ("invalid-secret", "a" * 31, "ee" + SECRET):
            with self.subTest(secret=secret):
                with self.assertRaises(ValueError) as result:
                    self.config(TG_PROXY_HOST="proxy.example", TG_PROXY_PORT="443", TG_PROXY_SECRET=secret)
                self.assertNotIn(secret, str(result.exception))


if __name__ == "__main__":
    unittest.main()
