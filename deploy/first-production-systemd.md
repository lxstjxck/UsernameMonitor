# First production start: username-monitor.service

This is the first installation of the UsernameMonitor unit on the Moscow VPS.
It is **not** the rollout procedure for an already running monitor. Do not run
these commands until the reviewed source commit is published and production
start is explicitly approved. Leave `flowseal.service` alone. Never run
`login`, alter `/opt/username-monitor/data/telegram.session`, or paste the
FakeTLS secret into a command, Git or a log.

The confirmed host has Python 3.12.3 and systemd 255. The service runs as
`username-monitor`. Source and venv live in a **new** release directory;
existing session, monitor SQLite and `usernames.txt` remain in
`/opt/username-monitor`. Do not transplant the manual User-Agent patch from
`/opt/username-monitor-candidate`: the new release must contain it in Git.

## 1. Preflight without starting production

```bash
set -euo pipefail
SERVICE=username-monitor.service
APP_USER=username-monitor
REVIEWED_SHA='<full-sha-after-approval-and-push>'
RELEASE="/opt/username-monitor-release-${REVIEWED_SHA:0:12}"
test "$(python3.12 --version)" = 'Python 3.12.3'
test "$(systemctl --version | awk 'NR==1 {print $2}')" = 255
id "$APP_USER"
if systemctl cat "$SERVICE" >/dev/null 2>&1; then
  echo "UsernameMonitor unit already exists; stop" >&2
  exit 1
fi
test -f /opt/username-monitor/data/telegram.session
test -f /opt/username-monitor/data/monitor.sqlite3
test -f /opt/username-monitor/usernames.txt
test -f /opt/username-monitor/.env
sudo test -s /etc/username-monitor/mtproxy-secret.b64
test "$(sudo stat -c %a /etc/username-monitor/mtproxy-secret.b64)" = 600
```

If the systemd version is not 255 or the environment differs from the
confirmed layout, stop and review the unit before continuing. Do not print
the secret file or the contents of `.env`.

```bash
sudo install -d -m 700 -o "$APP_USER" -g "$(id -gn "$APP_USER")" "$RELEASE"
sudo -u "$APP_USER" git clone https://github.com/lxstjxck/UsernameMonitor.git "$RELEASE"
sudo -u "$APP_USER" git -C "$RELEASE" checkout --detach "$REVIEWED_SHA"
test "$(sudo -u "$APP_USER" git -C "$RELEASE" rev-parse HEAD)" = "$REVIEWED_SHA"
sudo -u "$APP_USER" python3.12 -m venv "$RELEASE/.venv"
sudo -u "$APP_USER" "$RELEASE/.venv/bin/python" -m pip install -r "$RELEASE/requirements.txt"
sudo install -m 600 -o "$APP_USER" -g "$(id -gn "$APP_USER")" /opt/username-monitor/.env "$RELEASE/.env"
cd "$RELEASE"
sudo -u "$APP_USER" .venv/bin/python -m pip check
sudo -u "$APP_USER" .venv/bin/python -m unittest discover -s tests -q
```

Confirm the `.env` uses `CHAT_ID` or `TG_CHAT_ID`; if both are set, the
application requires them to match. Do not print their values.

## 2. Fresh SQLite backups and authorized RPC on a copy

The SQLite online backup reads the existing session and monitor DB in
read-only mode, includes WAL and creates destination files exclusively with
mode 0600. It does not modify the originals.

```bash
STAMP="$(date +%Y%m%d-%H%M%S)"
SESSION_BACKUP="/opt/username-monitor/data/telegram-before-first-service-$STAMP.session"
DB_BACKUP="/opt/username-monitor/data/monitor-before-first-service-$STAMP.sqlite3"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/telegram.session "$SESSION_BACKUP"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/monitor.sqlite3 "$DB_BACKUP"
sudo -u "$APP_USER" .venv/bin/python - "$SESSION_BACKUP" "$DB_BACKUP" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
for name in sys.argv[1:]:
    with closing(sqlite3.connect(Path(name).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("SQLite backup integrity failed")
print("SQLITE_BACKUPS_OK")
PY
```

Choose one valid test name outside the monitored `usernames.txt`. The
probe creates another temporary copy of the session backup, runs
`get_me`, `help.GetConfigRequest`, one `account.CheckUsernameRequest`,
isolated SQLite recording and a fresh FakeTLS connection. It sends no alert.

```bash
TEST_USERNAME=testusername12345
sudo systemd-run --wait --pipe --collect \
  -p "User=$APP_USER" -p "WorkingDirectory=$RELEASE" \
  -p LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64 \
  "$RELEASE/.venv/bin/python" "$RELEASE/deploy/run-with-mtproxy-credential.py" \
  --mtproxy-host 45.130.124.28 --mtproxy-port 443 \
  --session-path "$SESSION_BACKUP" verify-transport \
  --username "$TEST_USERNAME" --reconnect
```

Require exit 0 and `get_me: PASS`, `help.GetConfig: PASS`,
`account.checkUsername: PASS (...)`, `isolated SQLite: PASS` and
`reconnect: PASS`. A FloodWait, timeout, missing credential or unauthorized
session blocks the first start. Do not retry in a loop and do not run
`login`. The automated tests cover injected error and FloodWait paths.

Send only one neutral notification to confirm the published User-Agent and
the existing Cloudflare `/bot/sendMessage` route. This cannot trigger a
free-username message:

```bash
sudo systemd-run --wait --pipe --collect \
  -p "User=$APP_USER" -p "WorkingDirectory=$RELEASE" \
  -p Environment=TG_TRANSPORT=direct \
  "$RELEASE/.venv/bin/python" -c 'import asyncio,sys; from username_monitor.config import load_config; from username_monitor.notify import notify; c=load_config(); ok=asyncio.run(notify(c.bot_token,c.chat_id,"UsernameMonitor: тест production-доставки, без проверки username",bot_proxy_url=c.bot_proxy_url,bot_proxy_secret=c.bot_proxy_secret)); sys.exit(0 if ok else 1)'
```

Require exit 0 **and receipt** of the neutral message. No Worker settings,
tokens or message format are changed by this rollout.

## 3. Create and start the unit, only after approval and PASS

The following writes one new unit, validates it, then enables and starts it.
`ExecStart` contains only paths and the public proxy address. The root-owned
credential file holds the secret, and systemd exposes it only to this unit.
The runner injects `TG_MTPROXY_SECRET_B64` into the application process.

```bash
sudo tee "/etc/systemd/system/$SERVICE" >/dev/null <<EOF
[Unit]
Description=Telegram Username Monitor (FakeTLS)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$(id -gn "$APP_USER")
WorkingDirectory=$RELEASE
LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64
ExecStart=$RELEASE/.venv/bin/python $RELEASE/deploy/run-with-mtproxy-credential.py --mtproxy-host 45.130.124.28 --mtproxy-port 443 --session-path /opt/username-monitor/data/telegram --db-path /opt/username-monitor/data/monitor.sqlite3 --usernames-path /opt/username-monitor/usernames.txt run
Restart=on-failure
RestartSec=30
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
sudo systemd-analyze verify "/etc/systemd/system/$SERVICE"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
```

## 4. Production smoke and rollback

Privately inspect `systemctl status "$SERVICE"` and
`journalctl -u "$SERVICE" --since "10 minutes ago"` for authorized startup,
connection failures and accidental credential output. Never paste raw
journal content without redaction. Wait for a scheduled monitoring cycle,
then compare `status` against the pre-start database history; confirm that
the existing SQLite database advances and that no false free alert was sent.
Do not launch a second `check` process alongside the unit.

```bash
cd "$RELEASE"
sudo -u "$APP_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  .venv/bin/python -m username_monitor status
sudo systemctl is-active "$SERVICE"
```

If startup or smoke fails, stop and disable **only** UsernameMonitor. With
no previous UsernameMonitor unit there is no unit to restart. Keep the old
checkout and venv, both SQLite backups and the original session intact.
Do not touch `flowseal.service`, restore a session automatically or switch
to an unverified legacy/direct route.

```bash
sudo systemctl disable --now "$SERVICE"
sudo systemctl is-active "$SERVICE" || true
```
