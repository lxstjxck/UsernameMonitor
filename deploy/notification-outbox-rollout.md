# Transactional outbox rollout: username-monitor.service

**Prepared, not executed.** Publish the reviewed commit and obtain explicit
production approval before stopping or updating the service. The planned
rollback target is `d0372839ea33bdb32601a5d89cb9034a63d5e8b3`.
Keep both releases, all backup files, the original Telegram session and
`flowseal.service`. Never run `login` or a second client on the production
session. Do not send a synthetic free-username notification to the real chat.

## 1. Stage a separate release while production runs

Privately inspect the actual unit and override. The commands below require
the existing `username-monitor.service`, `LoadCredential`, user
`username-monitor` and the `idle-lifecycle.conf` override. Stop and review
if the active release differs from the expected rollback commit. Never show
the credential or `.env` in logs or chat.

```bash
set -euo pipefail
SERVICE=username-monitor.service
APP_USER=username-monitor
OLD_SHA=d0372839ea33bdb32601a5d89cb9034a63d5e8b3
NEW_SHA='<full-reviewed-sha-after-publication>'
OLD_RELEASE="$(sudo systemctl show -P WorkingDirectory "$SERVICE")"
NEW_RELEASE="/opt/username-monitor-release-${NEW_SHA:0:12}"
OVERRIDE="/etc/systemd/system/$SERVICE.d/idle-lifecycle.conf"
ROLLBACK_OVERRIDE="/etc/username-monitor/idle-lifecycle-before-outbox-${NEW_SHA:0:12}"
sudo systemctl cat "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
test "$(sudo systemctl show -P User "$SERVICE")" = "$APP_USER"
test "$(sudo -u "$APP_USER" git -C "$OLD_RELEASE" rev-parse HEAD)" = "$OLD_SHA"
sudo test -f "$OVERRIDE"
sudo test -s /etc/username-monitor/mtproxy-secret.b64
sudo test -f /opt/username-monitor/data/telegram.session
sudo test -f /opt/username-monitor/data/monitor.sqlite3
sudo test -f /opt/username-monitor/usernames.txt
sudo install -d -m 700 -o "$APP_USER" -g "$(id -gn "$APP_USER")" "$NEW_RELEASE"
sudo -u "$APP_USER" git clone https://github.com/lxstjxck/UsernameMonitor.git "$NEW_RELEASE"
sudo -u "$APP_USER" git -C "$NEW_RELEASE" checkout --detach "$NEW_SHA"
test "$(sudo -u "$APP_USER" git -C "$NEW_RELEASE" rev-parse HEAD)" = "$NEW_SHA"
sudo -u "$APP_USER" python3.12 -m venv "$NEW_RELEASE/.venv"
sudo -u "$APP_USER" "$NEW_RELEASE/.venv/bin/python" -m pip install -r "$NEW_RELEASE/requirements.txt"
sudo install -m 600 -o "$APP_USER" -g "$(id -gn "$APP_USER")" "$OLD_RELEASE/.env" "$NEW_RELEASE/.env"
cd "$NEW_RELEASE"
sudo -u "$APP_USER" .venv/bin/python -m unittest discover -s tests -q
sudo -u "$APP_USER" .venv/bin/python -m pip check
```

The unit tests use mock HTTP, never send live notifications and exercise
retries, a simulated restart, SQLite rollback and cancellation. Check the
installed unit's public MTProxy host/port and credential runner privately;
the new release must keep them unchanged.

## 2. Stop only the monitor, back up and verify a disposable copy

Stopping the monitor prevents simultaneous Telegram clients on the same
session. `backup_session` uses SQLite online backup from read-only sources,
including WAL, and creates private mode-0600 destinations. Do not modify
either recovery backup. The migration test runs on a **second** database copy.

```bash
sudo systemctl stop "$SERVICE"
test "$(sudo systemctl is-active "$SERVICE")" = inactive
STAMP="$(date +%Y%m%d-%H%M%S)"
SESSION_BACKUP="/opt/username-monitor/data/telegram-before-outbox-$STAMP.session"
DB_BACKUP="/opt/username-monitor/data/monitor-before-outbox-$STAMP.sqlite3"
MIGRATION_COPY="/opt/username-monitor/data/monitor-migration-test-$STAMP.sqlite3"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/telegram.session "$SESSION_BACKUP"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/monitor.sqlite3 "$DB_BACKUP"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' "$DB_BACKUP" "$MIGRATION_COPY"
sudo -u "$APP_USER" .venv/bin/python - "$DB_BACKUP" "$MIGRATION_COPY" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from username_monitor.store import Store
backup, candidate = map(Path, sys.argv[1:])
def counts(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit('SQLite integrity failed')
        return tuple(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                     for table in ('state', 'changes'))
before = counts(backup)
with Store(candidate) as store:
    if store.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('Migration integrity failed')
    assert store.db.execute("SELECT 1 FROM sqlite_master WHERE name='notification_outbox'").fetchone()
if counts(candidate) != before:
    raise SystemExit('State or history count changed during migration')
print('OUTBOX_MIGRATION_COPY_OK')
PY
```

The authorized probe copies the session backup again, performs `get_me`
and `help.GetConfigRequest`, then disconnects. No Bot API message or SQLite
production write is performed. Run it only while the monitor is stopped:

```bash
sudo systemd-run --wait --pipe --collect \
  -p "User=$APP_USER" -p "WorkingDirectory=$NEW_RELEASE" \
  -p LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64 \
  "$NEW_RELEASE/.venv/bin/python" "$NEW_RELEASE/deploy/run-with-mtproxy-credential.py" \
  --mtproxy-host 45.130.124.28 --mtproxy-port 443 \
  --session-path "$SESSION_BACKUP" verify-transport
```

Require exit 0, `get_me: PASS`, `help.GetConfig: PASS` and the migration
PASS line. If a gate fails, start the unchanged old unit and investigate;
do not change its override. On the migration copy, the following read-only
command must show zero or existing counts without requiring a secret:

```bash
sudo -u "$APP_USER" env DB_PATH="$MIGRATION_COPY" \
  .venv/bin/python -m username_monitor notifications-status
```

## 3. Switch the existing override and smoke

Back up the active override outside the `.conf` directory. Do not change
the base unit, Worker, `flowseal.service`, credential or persistent data.
With the service stopped, replace only its existing override:

```bash
sudo install -m 600 -o root -g root "$OVERRIDE" "$ROLLBACK_OVERRIDE"
sudo tee "$OVERRIDE" >/dev/null <<EOF
[Service]
WorkingDirectory=$NEW_RELEASE
ExecStart=
ExecStart=$NEW_RELEASE/.venv/bin/python $NEW_RELEASE/deploy/run-with-mtproxy-credential.py --mtproxy-host 45.130.124.28 --mtproxy-port 443 --session-path /opt/username-monitor/data/telegram --db-path /opt/username-monitor/data/monitor.sqlite3 --usernames-path /opt/username-monitor/usernames.txt run
EOF
sudo systemd-analyze verify "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
sudo -u "$APP_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  "$NEW_RELEASE/.venv/bin/python" -m username_monitor notifications-status
```

Privately inspect `sudo systemctl status "$SERVICE" --no-pager` and
`sudo journalctl -u "$SERVICE" --since '15 minutes ago' --no-pager`.
Verify one full monitoring cycle, unchanged result/history storage,
authorization, no continuous idle reconnects, and the queue status.
Observe a complete 600-second pause and the next cycle. If a real pending
event exists, verify its scheduled retry advances; do not manufacture a
free-username result or send a synthetic free alert. Do not run a second
`check` process beside the unit.

## 4. Rollback to d0372839

Stop only the monitor, restore the saved override and start the prior
checkout/venv. Do **not** restore the DB backup automatically: the old
version ignores the new `notification_outbox` table, so pending events stay
in SQLite but are not processed until this release is re-enabled. Preserve
all release directories, session, database and backups. A later re-enable
may redeliver an event accepted before a lost HTTP response (at least once).

```bash
sudo systemctl stop "$SERVICE"
sudo install -m 644 -o root -g root "$ROLLBACK_OVERRIDE" "$OVERRIDE"
sudo systemd-analyze verify "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
test "$(sudo systemctl show -P WorkingDirectory "$SERVICE")" = "$OLD_RELEASE"
```
