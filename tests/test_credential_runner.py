import io
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch


RUNNER = Path(__file__).resolve().parents[1] / "deploy/run-with-mtproxy-credential.py"
main = runpy.run_path(str(RUNNER))["main"]


class TestCredentialRunner(unittest.TestCase):
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
                self.assertEqual(os.environ["TG_TRANSPORT"], "faketls")
                self.assertEqual(os.environ["SESSION_PATH"], "/tmp/backup")
                self.assertEqual(execv.call_args.args[1][-2:], ["verify-transport", "--reconnect"])
                self.assertNotIn("private-synthetic-value", repr(execv.call_args))

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
