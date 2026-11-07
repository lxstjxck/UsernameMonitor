import io
import base64
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from username_monitor.config import load_config


RUNNER = Path(__file__).resolve().parents[1] / "deploy/run-with-mtproxy-credential.py"
main = runpy.run_path(str(RUNNER))["main"]


class TestCredentialRunner(unittest.TestCase):
    def test_retired_installer_never_creates_a_unit(self):
        installer = RUNNER.parent / "install-systemd.sh"
        result = subprocess.run(["bash", str(installer), "username-monitor"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("faketls-only-production.md", result.stderr)

    def test_secret_moves_from_credential_to_environment_not_argv(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "mtproxy_secret_b64").write_text("private-synthetic-value\n")
            env = {"CREDENTIALS_DIRECTORY": root, "TG_TRANSPORT": "legacy",
                   "SESSION_PATH": "/tmp/old"}
            with patch.dict(os.environ, env, clear=True), \
                    patch("sys.argv", [str(RUNNER), "--mtproxy-host", "45.130.124.28",
                                       "--mtproxy-port", "443", "--session-path", "/tmp/backup",
                                       "verify-transport", "--reconnect"]), \
                    patch("os.execv") as execv:
                main()
                self.assertEqual(os.environ["TG_MTPROXY_SECRET_B64"], "private-synthetic-value")
                self.assertEqual(os.environ["TG_TRANSPORT"], "legacy")
                self.assertEqual(os.environ["TG_MTPROXY_HOST"], "45.130.124.28")
                self.assertEqual(os.environ["TG_MTPROXY_PORT"], "443")
                self.assertEqual(os.environ["SESSION_PATH"], "/tmp/backup")
                self.assertEqual(execv.call_args.args[1][-2:], ["verify-transport", "--reconnect"])
                self.assertNotIn("private-synthetic-value", repr(execv.call_args))

    def test_existing_execstart_with_old_env_still_loads_faketls(self):
        secret = base64.b64encode(bytes.fromhex("ee" + "a1" * 16) + b"a.com").decode()
        with tempfile.TemporaryDirectory() as root:
            Path(root, "mtproxy_secret_b64").write_text(secret + "\n")
            env = {"CREDENTIALS_DIRECTORY": root, "TG_API_ID": "12345",
                   "TG_API_HASH": "test-hash", "TG_TRANSPORT": "legacy",
                   "TG_PROXY_HOST": "obsolete.example", "TG_PROXY_PORT": "443",
                   "TG_PROXY_SECRET": "a1" * 16}
            with patch.dict(os.environ, env, clear=True), \
                    patch("sys.argv", [str(RUNNER), "--mtproxy-host", "45.130.124.28",
                                       "--mtproxy-port", "443", "--session-path", "/tmp/backup",
                                       "run"]), patch("os.execv") as execv, \
                    patch("username_monitor.config.load_dotenv"):
                main()
                config = load_config()
                self.assertEqual((config.mtproxy_host, config.mtproxy_port), ("45.130.124.28", 443))
                self.assertEqual(config.mtproxy_secret[0], 0xEE)
                self.assertEqual(config.session_path, Path("/tmp/backup"))
                self.assertNotIn(secret, repr(config))
                self.assertNotIn(secret, repr(execv.call_args))

    def test_missing_credential_fails_without_exposing_path(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("sys.argv", [str(RUNNER), "--mtproxy-host", "proxy",
                                   "--mtproxy-port", "443", "run"]), \
                redirect_stderr(io.StringIO()) as output, \
                patch("os.execv") as execv:
            self.assertEqual(main(), 1)
        self.assertIn("credential unavailable", output.getvalue())
        execv.assert_not_called()

    def test_username_option_is_not_abbreviated_as_usernames_path(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "mtproxy_secret_b64").write_text("private-synthetic-value\n")
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": root}, clear=True), \
                    patch("sys.argv", [str(RUNNER), "--mtproxy-host", "45.130.124.28",
                                       "--mtproxy-port", "443", "--session-path", "/tmp/backup",
                                       "verify-transport", "--username", "testusername12345",
                                       "--reconnect"]), \
                    patch("os.execv") as execv:
                main()
                self.assertNotIn("USERNAMES_PATH", os.environ)
                self.assertEqual(
                    execv.call_args.args[1][-5:],
                    ["username_monitor", "verify-transport", "--username",
                     "testusername12345", "--reconnect"],
                )


if __name__ == "__main__":
    unittest.main()
