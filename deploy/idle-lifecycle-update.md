# Update the running username-monitor.service after idle reconnects

Run only after review, publication of the tested commit, and explicit approval
for VPS changes. The running production release is `3f94425`; keep its checkout
and venv. Do not run `login`, start a second monitor with the production
session, modify `flowseal.service`, or expose the credential. This procedure
changes only `username-monitor.service`. The new code disconnects after each
username RPC; the 10-second free confirmation, delays between names, and the
600-second cycle pause have no open MTProto socket. Telethon's bounded
automatic reconnect still applies while an RPC is active.

## 1. Stage a separate release while the old service runs

Set `REVIEWED_SHA` to the full published commit SHA after approval. No files
in the current executable checkout or venv are modified.

```bash
set -euo pipefail
SERVICE=username-monitor.service
APP_USER=username-monitor
REVIEWED_SHA='<full-reviewed-sha>'
OLD_RELEASE=/opt/username-monitor-release-3f944252f308
NEW_RELEASE="/opt/username-monitor-release-${REVIEWED_SHA:0:12}"
sudo systemctl cat "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
test "$(sudo systemctl show -P User "$SERVICE")" = "$APP_USER"
test -f /opt/username-monitor/data/telegram.session
test -f /opt/username-monitor/data/monitor.sqlite3
test -f /opt/username-monitor/usernames.txt
sudo test -s /etc/username-monitor/mtproxy-secret.b64
sudo install -d -m 700 -o "$APP_USER" -g "$(id -gn "$APP_USER")" "$NEW_RELEASE"
sudo -u "$APP_USER" git clone https://github.com/lxstjxck/UsernameMonitor.git "$NEW_RELEASE"
sudo -u "$APP_USER" git -C "$NEW_RELEASE" checkout --detach "$REVIEWED_SHA"
test "$(sudo -u "$APP_USER" git -C "$NEW_RELEASE" rev-parse HEAD)" = "$REVIEWED_SHA"
sudo -u "$APP_USER" python3.12 -m venv "$NEW_RELEASE/.venv"
sudo -u "$APP_USER" "$NEW_RELEASE/.venv/bin/python" -m pip install -r "$NEW_RELEASE/requirements.txt"
sudo install -m 600 -o "$APP_USER" -g "$(id -gn "$APP_USER")" "$OLD_RELEASE/.env" "$NEW_RELEASE/.env"
cd "$NEW_RELEASE"
sudo -u "$APP_USER" .venv/bin/python -m pip check
sudo -u "$APP_USER" .venv/bin/python -m unittest discover -s tests -q
```

Before continuing, inspect the installed unit privately: confirm the actual
`WorkingDirectory`, `ExecStart`, `LoadCredential`, data paths, and current
checkout. If they differ from the paths above, update the commands before
stopping anything. Do not print `.env` or the credential file.

## 2. Maintenance window: stop only UsernameMonitor and verify a copy

Stopping the service first avoids concurrent Telegram clients using the
production session. These SQLite online backups use read-only sources and
create mode-0600 destinations. No session file is restored or replaced.

```bash
sudo systemctl stop "$SERVICE"
sudo systemctl is-active "$SERVICE" || true
STAMP="$(date +%Y%m%d-%H%M%S)"
SESSION_BACKUP="/opt/username-monitor/data/telegram-before-idle-update-$STAMP.session"
DB_BACKUP="/opt/username-monitor/data/monitor-before-idle-update-$STAMP.sqlite3"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/telegram.session "$SESSION_BACKUP"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/monitor.sqlite3 "$DB_BACKUP"
sudo -u "$APP_USER" .venv/bin/python - "$SESSION_BACKUP" "$DB_BACKUP" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
for name in sys.argv[1:]:
    with closing(sqlite3.connect(Path(name).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit('SQLite backup integrity failed')
print('SQLITE_BACKUPS_OK')
PY
```

The probe makes another temporary copy of the backup and performs authorized
`get_me`, `help.GetConfig`, 25 seconds of idle followed by another config RPC,
and a separate connection with five RPCs 5 seconds apart. It sends no Bot API
message and does not touch the production session. A failed idle RPC is useful
evidence but does not by itself establish why the peer closed; a successful
idle RPC may have been recovered by Telethon. An unexpected authorization,
periodic-RPC, or connection failure blocks the switch.

```bash
sudo systemd-run --wait --pipe --collect \
  -p "User=$APP_USER" -p "WorkingDirectory=$NEW_RELEASE" \
  -p LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64 \
  "$NEW_RELEASE/.venv/bin/python" "$NEW_RELEASE/deploy/run-with-mtproxy-credential.py" \
  --mtproxy-host 45.130.124.28 --mtproxy-port 443 \
  --session-path "$SESSION_BACKUP" verify-transport --idle-probe
```

If the idle probe fails, run the existing `verify-transport` without
`--idle-probe` to establish that authorized RPC and the route still work;
review both stage results before switching. The idle probe returns nonzero on
an idle failure even when the periodic stage passes. Do not loop retries or
run a second `check` against the production session.

## 3. Switch the existing unit and smoke

Preserve the unit's existing credential, restart policy and hardening. Put
only public paths and the public MTProxy endpoint in the override. Never put
the secret in `ExecStart` or in a journal command.

```bash
sudo install -d -m 755 "/etc/systemd/system/$SERVICE.d"
sudo tee "/etc/systemd/system/$SERVICE.d/idle-lifecycle.conf" >/dev/null <<EOF
[Service]
WorkingDirectory=$NEW_RELEASE
ExecStart=
ExecStart=$NEW_RELEASE/.venv/bin/python $NEW_RELEASE/deploy/run-with-mtproxy-credential.py --mtproxy-host 45.130.124.28 --mtproxy-port 443 --session-path /opt/username-monitor/data/telegram --db-path /opt/username-monitor/data/monitor.sqlite3 --usernames-path /opt/username-monitor/usernames.txt run
EOF
sudo systemd-analyze verify "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager
sudo journalctl -u "$SERVICE" --since '10 minutes ago' --no-pager
sudo -u "$APP_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  "$NEW_RELEASE/.venv/bin/python" -m username_monitor status
```

Check that the first full cycle records results in the existing SQLite DB,
that no false free alert is sent, and that the 600-second wait has **no**
`Connection closed while receiving data` / `begin reconnect` loop. A
successful first cycle alone does not prove the wait is stable; observe the
full pause and start of the next cycle. Do not publish unredacted journal
lines; a credential must not appear there.

## Rollback

If any gate fails, stop UsernameMonitor before switching back. Removing this
single drop-in restores the original unit and original release/venv. Do not
restore the session or database automatically; preserve both backup files.

```bash
sudo systemctl stop "$SERVICE"
sudo rm "/etc/systemd/system/$SERVICE.d/idle-lifecycle.conf"
sudo systemctl daemon-reload
sudo systemd-analyze verify "$SERVICE"
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager
```

The previous release may exhibit the known idle reconnect pattern. If
service continuity is less important than avoiding that pattern, leave only
UsernameMonitor stopped and investigate. Keep the new and old checkouts and
venvs until stable operation is confirmed.
