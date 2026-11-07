# FakeTLS-only update of the running Moscow service

**Prepared procedure, not authorization to execute it.** The active
`username-monitor.service` runs release `20be8d577cdc` as `username-monitor`
with `LoadCredential` and `/etc/systemd/system/username-monitor.service.d/idle-lifecycle.conf`.
Publish the reviewed commit and obtain explicit production approval first.
Do not run `login`, touch `flowseal.service`, change the Worker or restore the
original session. Keep the current checkout, venv, session, DB and backups.

## 1. Stage new code while the old release runs

The new checkout is separate; do not update the executable checkout in place.
Inspect the current unit privately and stop if its paths or credential differ
from those below. Unit output and `.env` may be sensitive; do not share them.

```bash
set -euo pipefail
SERVICE=username-monitor.service
APP_USER=username-monitor
OLD_RELEASE=/opt/username-monitor-release-20be8d577cdc
REVIEWED_SHA='<full-published-commit-sha>'
NEW_RELEASE="/opt/username-monitor-release-${REVIEWED_SHA:0:12}"
OVERRIDE="/etc/systemd/system/$SERVICE.d/idle-lifecycle.conf"
ROLLBACK_OVERRIDE="/etc/username-monitor/idle-lifecycle-before-faketls-only-${REVIEWED_SHA:0:12}"
sudo systemctl cat "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
test "$(sudo systemctl show -P User "$SERVICE")" = "$APP_USER"
test "$(sudo systemctl show -P WorkingDirectory "$SERVICE")" = "$OLD_RELEASE"
sudo test -f "$OVERRIDE"
sudo test -s /etc/username-monitor/mtproxy-secret.b64
sudo test -f /opt/username-monitor/data/telegram.session
sudo test -f /opt/username-monitor/data/monitor.sqlite3
sudo test -f /opt/username-monitor/usernames.txt
sudo install -d -m 700 -o "$APP_USER" -g "$(id -gn "$APP_USER")" "$NEW_RELEASE"
sudo -u "$APP_USER" git clone https://github.com/lxstjxck/UsernameMonitor.git "$NEW_RELEASE"
sudo -u "$APP_USER" git -C "$NEW_RELEASE" checkout --detach "$REVIEWED_SHA"
test "$(sudo -u "$APP_USER" git -C "$NEW_RELEASE" rev-parse HEAD)" = "$REVIEWED_SHA"
sudo -u "$APP_USER" python3.12 -m venv "$NEW_RELEASE/.venv"
sudo -u "$APP_USER" "$NEW_RELEASE/.venv/bin/python" -m pip install -r "$NEW_RELEASE/requirements.txt"
sudo install -m 600 -o "$APP_USER" -g "$(id -gn "$APP_USER")" "$OLD_RELEASE/.env" "$NEW_RELEASE/.env"
cd "$NEW_RELEASE"
sudo -u "$APP_USER" .venv/bin/python -m unittest discover -s tests -q
sudo -u "$APP_USER" .venv/bin/python -m pip check
sudo -u "$APP_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  .venv/bin/python -m username_monitor status
```

Old `TG_TRANSPORT` and `TG_PROXY_*` in the copied `.env` are ignored.
`status` does not connect to Telegram. Confirm the base unit still has
`LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64`
and the active override uses the credential runner with the public
`--mtproxy-host 45.130.124.28 --mtproxy-port 443` arguments. Do not display
the credential contents.

## 2. Stop only UsernameMonitor, back up SQLite and probe a copy

Stop the service before running the copy-session RPC probe: no parallel
clients may use the production session. The SQLite online backup reads
the source in read-only mode, includes WAL and creates mode-0600 copies.

```bash
sudo systemctl stop "$SERVICE"
test "$(sudo systemctl is-active "$SERVICE")" = inactive
STAMP="$(date +%Y%m%d-%H%M%S)"
SESSION_BACKUP="/opt/username-monitor/data/telegram-before-faketls-only-$STAMP.session"
DB_BACKUP="/opt/username-monitor/data/monitor-before-faketls-only-$STAMP.sqlite3"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/telegram.session "$SESSION_BACKUP"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/monitor.sqlite3 "$DB_BACKUP"
sudo -u "$APP_USER" .venv/bin/python - "$SESSION_BACKUP" "$DB_BACKUP" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
for path in sys.argv[1:]:
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit('SQLite backup integrity failed')
print('SQLITE_BACKUPS_OK')
PY
```

Pick one valid test name. The probe makes an additional temporary copy of
the backup, calls authorized `get_me`, `help.GetConfigRequest`,
`account.CheckUsernameRequest`, writes only a temporary SQLite database and
checks reconnect. It never sends a free-username alert.

```bash
TEST_USERNAME=testusername12345
sudo systemd-run --wait --pipe --collect \
  -p "User=$APP_USER" -p "WorkingDirectory=$NEW_RELEASE" \
  -p LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64 \
  "$NEW_RELEASE/.venv/bin/python" "$NEW_RELEASE/deploy/run-with-mtproxy-credential.py" \
  --mtproxy-host 45.130.124.28 --mtproxy-port 443 \
  --session-path "$SESSION_BACKUP" verify-transport --username "$TEST_USERNAME" --reconnect
```

Require exit 0 and the `get_me`, `help.GetConfig`, `account.checkUsername`,
`isolated SQLite` and `reconnect` PASS lines. If any step fails, **do not
switch**: restart the untouched old unit with `sudo systemctl start "$SERVICE"`
and investigate. Never run a second `check` against the production session.

For a neutral notification test, use only the independent Bot API path. This
loads the existing `.env` and sends no Telegram MTProto RPC. Inspect
`CHAT_ID` and `TG_CHAT_ID` privately for conflicts before running it; it
does not put the token or Worker secret into the command line.

```bash
sudo systemd-run --wait --pipe --collect \
  -p "User=$APP_USER" -p "WorkingDirectory=$NEW_RELEASE" \
  "$NEW_RELEASE/.venv/bin/python" -c 'import asyncio,os,sys; from dotenv import load_dotenv; from username_monitor.notify import notify; load_dotenv(); chat=os.getenv("CHAT_ID") or os.getenv("TG_CHAT_ID", ""); ok=asyncio.run(notify(os.getenv("BOT_TOKEN", ""),chat,"UsernameMonitor: проверка доставки, без проверки username",bot_proxy_url=os.getenv("BOT_PROXY_URL") or None,bot_proxy_secret=os.getenv("BOT_PROXY_SECRET") or None)); sys.exit(0 if ok else 1)'
```

Require exit 0 and receipt of this neutral message. If it fails, restart
the old unit and investigate without switching. No Worker changes are needed.

## 3. Switch the existing override after preflight PASS

Back up the current override **outside** the `.conf` directory so systemd
will not load the backup. This preserves the base unit, LoadCredential,
restart policy and hardening. Do not create a second competing override.

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
```

Inspect private `sudo systemctl status "$SERVICE" --no-pager` and
`sudo journalctl -u "$SERVICE" --since '15 minutes ago' --no-pager` for
authorization, errors or accidental secret output; redact before sharing.
Use `status` as in step 1 to check the existing SQLite history advances.
Observe a full monitoring cycle, the 600-second pause without reconnect
loops, and the beginning of the next cycle. Check for false free alerts.
No second production client should run during this observation.

## 4. Rollback

If any smoke gate fails, stop only UsernameMonitor, restore the saved override
and restart the old `20be8d5` release with its existing venv. No `.env`,
session, SQLite or credential restore is automatic. Keep all backups and
both release directories.

```bash
sudo systemctl stop "$SERVICE"
sudo install -m 644 -o root -g root "$ROLLBACK_OVERRIDE" "$OVERRIDE"
sudo systemd-analyze verify "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
test "$(sudo systemctl show -P WorkingDirectory "$SERVICE")" = "$OLD_RELEASE"
```

The principal remaining risk is increased connection overhead: a free
username is checked on two separate FakeTLS connections. Telegram rate
limits and proxy availability still apply. Rollback restores the previous
code, not an alternative MTProto transport.
