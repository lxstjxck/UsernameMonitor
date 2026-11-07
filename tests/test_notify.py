import json
import base64
import os
import socket
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from username_monitor.config import load_config
from username_monitor.notify import HTTP_TIMEOUT_SECONDS, notify


WORKER_URL = "https://username-monitor-proxy.svjack69.workers.dev"
WORKER_SECRET = "worker-secret-for-test"
BOT_TOKEN = "bot-token-for-test"
MESSAGE = "private notification text"
BASE_ENV = {"TG_API_ID": "12345", "TG_API_HASH": "test-hash",
            "TG_MTPROXY_HOST": "proxy.example", "TG_MTPROXY_PORT": "443",
            "TG_MTPROXY_SECRET_B64": base64.b64encode(bytes.fromhex("ee" + "a1" * 16) + b"a.com").decode()}


def response_context(status=200, body=b'{"ok":true}'):
    response = MagicMock()
    response.status = status
    response.read.return_value = body
    response.__enter__.return_value = response
    return response


class TestBotProxyConfig(unittest.TestCase):
    def config(self, **values):
        with patch.dict(os.environ, BASE_ENV | values, clear=True), patch("username_monitor.config.load_dotenv"):
            return load_config()

    def test_worker_mode_does_not_require_bot_token(self):
        config = self.config(BOT_PROXY_URL=WORKER_URL + "/", BOT_PROXY_SECRET=WORKER_SECRET, CHAT_ID="123")
        self.assertEqual(config.bot_proxy_url, WORKER_URL)
        self.assertEqual(config.bot_token, "")
        self.assertNotIn(WORKER_SECRET, repr(config))

    def test_chat_id_alias_and_matching_values(self):
        alias = self.config(BOT_PROXY_URL=WORKER_URL, BOT_PROXY_SECRET=WORKER_SECRET,
                            TG_CHAT_ID=" 123 ")
        self.assertEqual(alias.chat_id, "123")
        matching = self.config(BOT_TOKEN=BOT_TOKEN, CHAT_ID="123", TG_CHAT_ID=" 123 ")
        self.assertEqual(matching.chat_id, "123")
        self.assertEqual(self.config(BOT_TOKEN=BOT_TOKEN, TG_CHAT_ID="123").chat_id, "123")

    def test_conflicting_chat_ids_fail_without_echoing_values(self):
        with self.assertRaises(ValueError) as caught:
            self.config(CHAT_ID="private-primary-123", TG_CHAT_ID="private-alias-456")
        message = str(caught.exception)
        self.assertIn("CHAT_ID и TG_CHAT_ID", message)
        self.assertNotIn("private-primary-123", message)
        self.assertNotIn("private-alias-456", message)

    def test_incomplete_or_invalid_worker_config_is_rejected(self):
        for values in (
            {"BOT_PROXY_URL": WORKER_URL},
            {"BOT_PROXY_SECRET": WORKER_SECRET},
            {"BOT_PROXY_URL": WORKER_URL, "BOT_PROXY_SECRET": WORKER_SECRET},
            {"BOT_PROXY_URL": "http://example.com", "BOT_PROXY_SECRET": WORKER_SECRET, "CHAT_ID": "123"},
            {"BOT_PROXY_URL": WORKER_URL + "/bot/sendMessage", "BOT_PROXY_SECRET": WORKER_SECRET, "CHAT_ID": "123"},
            {"BOT_PROXY_URL": WORKER_URL + "?", "BOT_PROXY_SECRET": WORKER_SECRET, "CHAT_ID": "123"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.config(**values)

    def test_direct_config_is_unchanged(self):
        config = self.config(BOT_TOKEN=BOT_TOKEN, CHAT_ID="123")
        self.assertIsNone(config.bot_proxy_url)
        self.assertIsNone(config.bot_proxy_secret)
        with self.assertRaisesRegex(ValueError, "BOT_TOKEN и CHAT_ID"):
            self.config(BOT_TOKEN=BOT_TOKEN)


class TestNotifications(unittest.IsolatedAsyncioTestCase):
    async def test_worker_success_uses_json_bearer_and_no_bot_token(self):
        opener = MagicMock()
        opener.open.return_value = response_context()
        with patch("username_monitor.notify.request.build_opener", return_value=opener):
            delivered = await notify(BOT_TOKEN, "123", MESSAGE,
                                     bot_proxy_url=WORKER_URL, bot_proxy_secret=WORKER_SECRET)
        self.assertTrue(delivered)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, WORKER_URL + "/bot/sendMessage")
        self.assertNotIn(BOT_TOKEN, req.full_url)
        self.assertEqual(req.get_method(), "POST")
        headers = {key.lower(): value for key, value in req.header_items()}
        self.assertEqual(headers["authorization"], "Bearer " + WORKER_SECRET)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["user-agent"], "UsernameMonitor/0.1")
        self.assertEqual(json.loads(req.data), {"chat_id": "123", "text": MESSAGE})
        self.assertNotIn(BOT_TOKEN.encode(), req.data)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], HTTP_TIMEOUT_SECONDS)

    async def test_worker_http_failures_are_not_success(self):
        for status in (401, 403, 502):
            with self.subTest(status=status):
                opener = MagicMock()
                opener.open.side_effect = HTTPError(WORKER_URL, status, "denied", {}, None)
                with patch("username_monitor.notify.request.build_opener", return_value=opener):
                    with self.assertLogs("username_monitor.notify", level="ERROR") as captured:
                        delivered = await notify("", "123", MESSAGE,
                                                 bot_proxy_url=WORKER_URL, bot_proxy_secret=WORKER_SECRET)
                self.assertFalse(delivered)
                self.assertIn(str(status), " ".join(captured.output))
                self.assertNotIn(WORKER_SECRET, " ".join(captured.output))
                self.assertNotIn(MESSAGE, " ".join(captured.output))

    async def test_worker_timeout_is_not_success_and_does_not_log_secrets(self):
        opener = MagicMock()
        opener.open.side_effect = socket.timeout("private data")
        with patch("username_monitor.notify.request.build_opener", return_value=opener):
            with self.assertLogs("username_monitor.notify", level="ERROR") as captured:
                delivered = await notify("", "123", MESSAGE,
                                         bot_proxy_url=WORKER_URL, bot_proxy_secret=WORKER_SECRET)
        self.assertFalse(delivered)
        logs = " ".join(captured.output)
        for sensitive in (WORKER_SECRET, MESSAGE, "private data"):
            self.assertNotIn(sensitive, logs)

    async def test_worker_non_success_response_is_not_success(self):
        for status, body in ((502, b'{"ok":true}'), (200, b'{"ok":false}'), (200, b'not json')):
            with self.subTest(status=status, body=body):
                opener = MagicMock()
                opener.open.return_value = response_context(status, body)
                with patch("username_monitor.notify.request.build_opener", return_value=opener):
                    with self.assertLogs("username_monitor.notify", level="ERROR"):
                        self.assertFalse(await notify("", "123", MESSAGE,
                                                      bot_proxy_url=WORKER_URL, bot_proxy_secret=WORKER_SECRET))

    async def test_direct_mode_preserves_bot_api_request(self):
        with patch("username_monitor.notify.request.urlopen", return_value=response_context()) as urlopen:
            delivered = await notify(BOT_TOKEN, "123", MESSAGE)
        self.assertTrue(delivered)
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")
        self.assertIn(b"chat_id=123", req.data)
        self.assertIn(b"text=private+notification+text", req.data)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], HTTP_TIMEOUT_SECONDS)
        self.assertNotIn("User-agent", dict(req.header_items()))

    async def test_unconfigured_mode_does_not_log_message(self):
        with self.assertLogs("username_monitor.notify", level="WARNING") as captured:
            self.assertFalse(await notify("", "", MESSAGE))
        self.assertNotIn(MESSAGE, " ".join(captured.output))


if __name__ == "__main__":
    unittest.main()
