import asyncio
import base64
from dataclasses import replace
import hashlib
import io
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, functions
from tgnet.connection.faketls import TgNetConnectionTls

from username_monitor.__main__ import create_telegram_client, main
from username_monitor.config import load_config
from username_monitor.transport_probe import backup_session, verify_transport


RAW_SECRET = bytes.fromhex("ee" + "a1" * 16) + b"a.com"
SECRET_B64 = base64.b64encode(RAW_SECRET).decode("ascii")
BASE = {"TG_API_ID": "12345", "TG_API_HASH": "test-hash"}


def config(**values):
    with patch.dict(os.environ, BASE | values, clear=True), patch("username_monitor.config.load_dotenv"):
        return load_config()


class TestFakeTlsConfig(unittest.TestCase):
    def test_valid_and_rollback_with_one_setting(self):
        values = dict(TG_TRANSPORT="faketls", TG_MTPROXY_HOST="45.130.124.28",
                      TG_MTPROXY_PORT="443", TG_MTPROXY_SECRET_B64=SECRET_B64)
        cfg = config(**values)
        self.assertEqual((cfg.mtproxy_host, cfg.mtproxy_port, cfg.mtproxy_secret),
                         ("45.130.124.28", 443, RAW_SECRET))
        self.assertNotIn(SECRET_B64, repr(cfg))
        self.assertNotIn("a.com", repr(cfg))
        direct = config(**(values | {"TG_TRANSPORT": "direct"}))
        with patch("telethon.TelegramClient") as client:
            create_telegram_client(direct)
            self.assertNotIn("connection", client.call_args.kwargs)
            self.assertNotIn("proxy", client.call_args.kwargs)
        legacy = config(**(values | {"TG_TRANSPORT": "legacy",
                                    "TG_PROXY_HOST": "old.example", "TG_PROXY_PORT": "443",
                                    "TG_PROXY_SECRET": "a1" * 16}))
        with patch("telethon.TelegramClient") as client:
            create_telegram_client(legacy)
            self.assertEqual(client.call_args.kwargs["proxy"],
                             ("old.example", 443, "a1" * 16))

    def test_invalid_values_do_not_echo_secret(self):
        for secret in (SECRET_B64[:-1], "!", base64.b64encode(b"\xdd" + RAW_SECRET[1:]).decode(),
                       base64.b64encode(RAW_SECRET + b"x").decode(),
                       base64.b64encode(RAW_SECRET[:17] + b"\x00.com").decode()):
            with self.subTest(length=len(secret)), self.assertRaises(ValueError) as caught:
                config(TG_TRANSPORT="faketls", TG_MTPROXY_HOST="proxy", TG_MTPROXY_PORT="443",
                       TG_MTPROXY_SECRET_B64=secret)
            self.assertNotIn(secret, str(caught.exception))
        for values in ({"TG_MTPROXY_HOST": "proxy"},
                       {"TG_MTPROXY_HOST": "proxy", "TG_MTPROXY_PORT": "0"},
                       {"TG_MTPROXY_HOST": "proxy", "TG_MTPROXY_PORT": "abc"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                config(TG_TRANSPORT="faketls", TG_MTPROXY_SECRET_B64=SECRET_B64, **values)

    def test_missing_dependency_only_matters_when_selected(self):
        cfg = config(TG_TRANSPORT="direct")
        with patch("telethon.TelegramClient") as client:
            create_telegram_client(cfg)
            self.assertEqual(client.call_count, 1)

    def test_secret_in_project_dotenv_is_not_accepted(self):
        def project_dotenv(*args, **kwargs):
            os.environ["TG_MTPROXY_SECRET_B64"] = SECRET_B64
        values = BASE | {"TG_TRANSPORT": "faketls", "TG_MTPROXY_HOST": "proxy",
                         "TG_MTPROXY_PORT": "443"}
        with patch.dict(os.environ, values, clear=True), \
                patch("username_monitor.config.load_dotenv", side_effect=project_dotenv):
            with self.assertRaisesRegex(ValueError, "TG_MTPROXY_SECRET_B64"):
                load_config()

    def test_verify_cli_validates_name_without_echoing_it(self):
        with patch("sys.argv", ["username_monitor", "verify-transport",
                                "--username", "private-invalid-name", "--reconnect"]), \
                patch("username_monitor.__main__.load_config", return_value=object()), \
                redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                main()
        self.assertNotIn("private-invalid-name", stderr.getvalue())

    def test_verify_cli_passes_valid_name_and_reconnect(self):
        with patch("sys.argv", ["username_monitor", "verify-transport",
                                "--username", "@TestUsername12345", "--reconnect"]), \
                patch("username_monitor.__main__.load_config", return_value=object()), \
                patch("username_monitor.transport_probe.verify_transport",
                      new_callable=AsyncMock, return_value=0) as probe:
            self.assertEqual(main(), 0)
        self.assertEqual(probe.await_args.kwargs,
                         {"username": "testusername12345", "reconnect": True})


class TestAdapter(unittest.TestCase):
    def setUp(self):
        self.cfg = config(TG_TRANSPORT="faketls", TG_MTPROXY_HOST="45.130.124.28",
                          TG_MTPROXY_PORT="443", TG_MTPROXY_SECRET_B64=SECRET_B64)

    def test_dc_id_stays_telegram_dc_and_tcp_targets_mtproxy(self):
        with patch("telethon.TelegramClient") as client:
            create_telegram_client(self.cfg, flood_sleep_threshold=0)
            args = client.call_args
            self.assertNotIn("proxy", args.kwargs)
            self.assertEqual(args.kwargs["connection_retries"], 2)
            self.assertEqual(args.kwargs["flood_sleep_threshold"], 0)
            connection = args.kwargs["connection"]
        self.assertTrue(issubclass(connection, TgNetConnectionTls))
        loggers = {"telethon.network.connection.connection": logging.getLogger("test")}
        instance = connection("149.154.167.50", 443, 2, loggers=loggers)
        self.assertEqual((instance._ip, instance._port, instance._dc_id),
                         ("45.130.124.28", 443, 2))
        self.assertEqual(instance.telegram_dc_ip, "149.154.167.50")
        self.assertEqual(instance._tgnet_secret, RAW_SECRET)
        self.assertNotIn("a.com", str(instance))
        with self.assertRaises(ValueError):
            connection("149.154.167.50", 443, 2, loggers=loggers, proxy=("socks",))

    def test_reconnect_creates_fresh_connection_for_current_dc(self):
        with patch("telethon.TelegramClient") as client:
            create_telegram_client(self.cfg)
            connection = client.call_args.kwargs["connection"]
        loggers = {"telethon.network.connection.connection": logging.getLogger("test")}
        first = connection("dc1.example", 443, 1, loggers=loggers)
        second = connection("dc2.example", 443, 2, loggers=loggers)
        self.assertIsNot(first, second)
        self.assertEqual((first._dc_id, second._dc_id), (1, 2))
        self.assertEqual((first._ip, second._ip), ("45.130.124.28",) * 2)

    def test_connection_failure_and_next_reconnect_target_proxy(self):
        with patch("telethon.TelegramClient") as client:
            create_telegram_client(self.cfg)
            connection = client.call_args.kwargs["connection"]
        loggers = {"telethon.network.connection.connection": logging.getLogger("test")}
        async def failed_tcp(**kwargs):
            self.assertEqual((kwargs["host"], kwargs["port"]), ("45.130.124.28", 443))
            self.assertIsNone(kwargs["ssl"])
            raise OSError("proxy unreachable")
        async def attempt():
            with patch("asyncio.open_connection", side_effect=failed_tcp) as tcp:
                for dc in (1, 2):
                    with self.assertRaises(OSError):
                        await connection("telegram.dc", 443, dc, loggers=loggers).connect(timeout=0.1)
                self.assertEqual(tcp.await_count, 2)
        asyncio.run(attempt())


class TestSessionProbe(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original = Path(self.tmp.name) / "telegram.session"
        with sqlite3.connect(self.original) as db:
            db.execute("CREATE TABLE sentinel (value TEXT)")
            db.execute("INSERT INTO sentinel VALUES ('authorized-session-placeholder')")
        self.cfg = replace(config(TG_TRANSPORT="faketls",
                                  TG_MTPROXY_HOST="45.130.124.28",
                                  TG_MTPROXY_PORT="443",
                                  TG_MTPROXY_SECRET_B64=SECRET_B64),
                           session_path=self.original)

    async def test_probe_uses_backup_and_never_starts_login(self):
        original_hash = hashlib.sha256(self.original.read_bytes()).digest()
        client = Mock()
        client.connect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_me = AsyncMock(return_value=object())
        client.disconnect = AsyncMock()
        async def rpc(request):
            self.assertIsInstance(request, functions.help.GetConfigRequest)
            return object()
        client.side_effect = rpc
        def create(copied_config, **kwargs):
            self.assertNotEqual(copied_config.session_path, self.original)
            self.assertTrue(copied_config.session_path.is_file())
            with sqlite3.connect(copied_config.session_path) as db:
                self.assertEqual(db.execute("SELECT value FROM sentinel").fetchone()[0],
                                 "authorized-session-placeholder")
            return client
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(await verify_transport(self.cfg, create), 0)
        self.assertIn("get_me: PASS", output.getvalue())
        self.assertIn("help.GetConfig: PASS", output.getvalue())
        self.assertNotIn(SECRET_B64, output.getvalue())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).digest(), original_hash)
        client.disconnect.assert_awaited_once()
        self.assertEqual(list(Path(self.tmp.name).glob("*")), [self.original])

    async def test_username_sqlite_and_forced_reconnect_use_only_copy(self):
        original_hash = hashlib.sha256(self.original.read_bytes()).digest()
        clients = []
        def make_client():
            client = Mock(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                          get_me=AsyncMock(return_value=object()), disconnect=AsyncMock())
            async def rpc(request):
                if isinstance(request, functions.account.CheckUsernameRequest):
                    return False  # occupied/unavailable; never a free alert
                self.assertIsInstance(request, functions.help.GetConfigRequest)
                return object()
            client.side_effect = rpc
            clients.append(client)
            return client
        def create(copied_config, **kwargs):
            self.assertNotEqual(copied_config.session_path, self.original)
            self.assertEqual(kwargs["flood_sleep_threshold"], 0)
            return make_client()
        output = io.StringIO()
        with redirect_stdout(output), patch("username_monitor.notify.notify") as notify:
            code = await verify_transport(self.cfg, create, username="testusername12345", reconnect=True)
        self.assertEqual(code, 0)
        self.assertIn("account.checkUsername: PASS (unavailable)", output.getvalue())
        self.assertIn("isolated SQLite: PASS", output.getvalue())
        self.assertIn("reconnect: PASS", output.getvalue())
        self.assertEqual(len(clients), 2)
        for client in clients:
            client.connect.assert_awaited_once()
            client.disconnect.assert_awaited_once()
        notify.assert_not_called()
        self.assertFalse(self.cfg.db_path.exists())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).digest(), original_hash)

    async def test_flood_wait_stops_probe_without_notification(self):
        client = Mock(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                      get_me=AsyncMock(return_value=object()), disconnect=AsyncMock())
        async def rpc(request):
            if isinstance(request, functions.account.CheckUsernameRequest):
                raise errors.FloodWaitError(request=None, capture=45)
            return object()
        client.side_effect = rpc
        output = io.StringIO()
        with redirect_stdout(output), patch("username_monitor.notify.notify") as notify:
            code = await verify_transport(self.cfg, lambda *a, **k: client,
                                          username="testusername12345", reconnect=True)
        self.assertEqual(code, 1)
        self.assertIn("FLOOD_WAIT (45 с)", output.getvalue())
        client.disconnect.assert_awaited_once()
        notify.assert_not_called()

    async def test_username_timeout_cancels_and_redacts(self):
        cancelled = asyncio.Event()
        client = Mock(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                      get_me=AsyncMock(return_value=object()), disconnect=AsyncMock())
        async def rpc(request):
            if isinstance(request, functions.account.CheckUsernameRequest):
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()
            return object()
        client.side_effect = rpc
        output = io.StringIO()
        with redirect_stdout(output), patch("username_monitor.notify.notify") as notify:
            code = await verify_transport(replace(self.cfg, check_timeout_seconds=0.01),
                                          lambda *a, **k: client,
                                          username="testusername12345", reconnect=True)
        self.assertEqual(code, 1)
        self.assertTrue(cancelled.is_set())
        self.assertIn("FAIL (TimeoutError)", output.getvalue())
        client.disconnect.assert_awaited_once()
        notify.assert_not_called()

    async def test_unauthorized_and_connection_failure_are_safe(self):
        for authorized, error in ((False, None), (True, OSError(SECRET_B64))):
            client = Mock(connect=AsyncMock(side_effect=error),
                          is_user_authorized=AsyncMock(return_value=authorized),
                          get_me=AsyncMock(), disconnect=AsyncMock())
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(await verify_transport(self.cfg, lambda *a, **k: client), 1)
            client.get_me.assert_not_awaited()
            client.disconnect.assert_awaited_once()
            self.assertNotIn(SECRET_B64, out.getvalue())

    async def test_backup_is_sqlite_snapshot(self):
        dst = Path(self.tmp.name) / "copy.session"
        backup_session(self.original, dst)
        with sqlite3.connect(dst) as db:
            self.assertEqual(db.execute("SELECT value FROM sentinel").fetchone()[0],
                             "authorized-session-placeholder")
        self.assertEqual(dst.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            backup_session(self.original, dst)


if __name__ == "__main__":
    unittest.main()
