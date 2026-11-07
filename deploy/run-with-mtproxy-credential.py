"""Inject a systemd credential and explicit runtime paths without CLI secrets."""

import argparse
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--mtproxy-host", required=True)
    parser.add_argument("--mtproxy-port", required=True)
    parser.add_argument("--session-path")
    parser.add_argument("--db-path")
    parser.add_argument("--usernames-path")
    args, command = parser.parse_known_args()
    if not command:
        print("UsernameMonitor command missing", file=sys.stderr)
        return 1
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        print("MTProxy credential unavailable", file=sys.stderr)
        return 1
    try:
        secret = (Path(directory) / "mtproxy_secret_b64").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        print("MTProxy credential unavailable", file=sys.stderr)
        return 1
    if not secret:
        print("MTProxy credential unavailable", file=sys.stderr)
        return 1
    os.environ["TG_MTPROXY_SECRET_B64"] = secret
    os.environ["TG_MTPROXY_HOST"] = args.mtproxy_host
    os.environ["TG_MTPROXY_PORT"] = args.mtproxy_port
    for key, value in (("SESSION_PATH", args.session_path), ("DB_PATH", args.db_path),
                       ("USERNAMES_PATH", args.usernames_path)):
        if value is not None:
            os.environ[key] = value
    os.execv(sys.executable, [sys.executable, "-m", "username_monitor", *command])
    return 1  # os.execv does not return on success.


if __name__ == "__main__":
    sys.exit(main())
