# FakeTLS rollout on the Moscow VPS

The production switch below requires separate approval after preflight.
Do not run `login`, `deploy/install-systemd.sh`, or another monitor process.
Keep the old checkout and virtual environment for rollback. Never edit
`/opt/username-monitor/data/telegram.session`.

## 1. Discover the actual service

Do not assume the name from the repository template. On the VPS:

```bash
systemctl list-units --all --type=service 'username-monitor.service' 'telegram-username-monitor.service' --no-pager
systemctl list-unit-files 'username-monitor.service' 'telegram-username-monitor.service' --no-pager
read -r -p "Active unit name from the output: " SERVICE
case "$SERVICE" in
  username-monitor.service|telegram-username-monitor.service) ;;
  *) echo "Unknown unit; stop rollout" >&2; exit 1 ;;
esac
systemctl is-active --quiet "$SERVICE"
RUN_USER="$(systemctl show -P User "$SERVICE")"
test -n "$RUN_USER" && test "$RUN_USER" != root
systemctl show -P WorkingDirectory "$SERVICE"
systemctl show -P FragmentPath "$SERVICE"
```

If neither or both candidates are active, investigate before proceeding.
Inspect the unit privately for its executable, working directory and existing
drop-ins. Do not share raw unit contents: they may contain credentials.

## 2. Prepare an isolated candidate

Only after the reviewed commit is published, insert its full SHA. This does
not change files used by the running service.

```bash
REVIEWED_SHA='<reviewed-commit-sha>'
CANDIDATE=/opt/username-monitor-candidate
sudo install -d -m 700 -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" "$CANDIDATE"
sudo -u "$RUN_USER" git clone https://github.com/lxstjxck/UsernameMonitor.git "$CANDIDATE"
test "$(sudo -u "$RUN_USER" git -C "$CANDIDATE" rev-parse HEAD)" = "$REVIEWED_SHA"
sudo -u "$RUN_USER" python3 -m venv "$CANDIDATE/.venv"
sudo -u "$RUN_USER" "$CANDIDATE/.venv/bin/python" -m pip install -r "$CANDIDATE/requirements.txt"
sudo -u "$RUN_USER" "$CANDIDATE/.venv/bin/python" -m pip check
test -f /opt/username-monitor/data/telegram.session
test -f /opt/username-monitor/data/monitor.sqlite3
test -f /opt/username-monitor/usernames.txt
sudo install -m 600 -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" /opt/username-monitor/.env "$CANDIDATE/.env"
cd "$CANDIDATE"
sudo -u "$RUN_USER" .venv/bin/python -m unittest discover -s tests -v
```

If the current unit uses other configuration paths or the database test fails,
adapt the absolute paths in the switch step before continuing. Keep all
credentials private. Create a root-owned credential file containing **only**
the Base64 secret. Check `systemctl --version`: `LoadCredential=` requires
systemd 247 or newer. Stop the rollout if unavailable. Never put the value
in a command, Git, chat, or the project's `.env`.

```bash
sudo install -d -m 700 /etc/username-monitor
sudo install -m 600 /dev/null /etc/username-monitor/mtproxy-secret.b64
sudoedit /etc/username-monitor/mtproxy-secret.b64
# In the editor: the Base64 value alone, no variable name or quotes.
sudo chmod 600 /etc/username-monitor/mtproxy-secret.b64
```

The decoded secret must be 22 bytes, beginning with EE. systemd supplies
it as a private credential; the runner puts it into `TG_MTPROXY_SECRET_B64`
in the application process. The app reads that variable before `.env`.
Preserve the old transport settings until production smoke succeeds.

## 3. Back up and probe the authorized session

The SQLite online backup includes WAL, reads the source in read-only mode,
and creates a private destination exclusively. Leave the old service running.

```bash
BACKUP="/opt/username-monitor/data/telegram-before-faketls-$(date +%Y%m%d-%H%M%S).session"
cd "$CANDIDATE"
sudo -u "$RUN_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path("/opt/username-monitor/data/telegram.session"), Path(sys.argv[1]))' "$BACKUP"
sudo -u "$RUN_USER" .venv/bin/python -c 'import sys, sqlite3; from pathlib import Path; db=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+"?mode=ro",uri=True); assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"; db.close()' "$BACKUP"
```

The probe makes another disposable SQLite copy of the backup. It runs
authorized `get_me`, `help.GetConfigRequest`, one
`account.CheckUsernameRequest`, records the result in isolated SQLite, then
forces a fresh FakeTLS connection and requests Telegram config again. It
never calls `start()`, writes the production monitor DB or sends alerts.
Choose a valid test name outside `usernames.txt`; its value is not printed.

```bash
TEST_USERNAME=testusername12345
sudo systemd-run --wait --pipe --collect \
  -p "User=$RUN_USER" -p "WorkingDirectory=$CANDIDATE" \
  -p LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64 \
  "$CANDIDATE/.venv/bin/python" "$CANDIDATE/deploy/run-with-mtproxy-credential.py" \
  --mtproxy-host 45.130.124.28 --mtproxy-port 443 --session-path "$BACKUP" verify-transport \
  --username "$TEST_USERNAME" --reconnect
```

Require exit code 0 and: `get_me: PASS`, `help.GetConfig: PASS`,
`account.checkUsername: PASS (...)`, `isolated SQLite: PASS`,
`reconnect: PASS`. Any `FLOOD_WAIT`, timeout or authorization failure blocks
deployment. Unit tests inject FloodWait, network failure and timeout; do not
deliberately trigger Telegram rate limits. The raw AuthKey/Ping 60/60 test
does not replace this authorized Telethon probe.

For notification delivery, send one neutral, clearly labeled test message
through the already configured Bot API or Cloudflare `/bot/sendMessage`.
This does not claim that any username is free and contains no secret in the
command line:

```bash
sudo systemd-run --wait --pipe --collect \
  -p "User=$RUN_USER" -p "WorkingDirectory=$CANDIDATE" \
  "$CANDIDATE/.venv/bin/python" -c 'import asyncio,sys; from username_monitor.config import load_config; from username_monitor.notify import notify; c=load_config(); ok=asyncio.run(notify(c.bot_token,c.chat_id,"UsernameMonitor: тест доставки уведомлений, без проверки username",bot_proxy_url=c.bot_proxy_url,bot_proxy_secret=c.bot_proxy_secret)); sys.exit(0 if ok else 1)'
```

Require exit code 0 and receipt of the neutral message. Present these
preflight results and the rollback plan before the production switch.

## 4. Switch production, only after explicit approval

Do not move the candidate virtual environment; Python venv paths are not
generally relocatable. Stop the **actual** unit first. A drop-in changes its
executable and working directory while leaving the old checkout and venv
intact. Absolute paths preserve the existing session, monitor SQLite and
watched names. Check for conflicting drop-ins before writing this one.

```bash
sudo systemctl stop "$SERVICE"
sudo install -d -m 755 "/etc/systemd/system/$SERVICE.d"
sudo tee "/etc/systemd/system/$SERVICE.d/faketls-rollout.conf" >/dev/null <<EOF
[Service]
WorkingDirectory=$CANDIDATE
LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64
ExecStart=
ExecStart=$CANDIDATE/.venv/bin/python $CANDIDATE/deploy/run-with-mtproxy-credential.py --mtproxy-host 45.130.124.28 --mtproxy-port 443 --session-path /opt/username-monitor/data/telegram --db-path /opt/username-monitor/data/monitor.sqlite3 --usernames-path /opt/username-monitor/usernames.txt run
EOF
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
```

Smoke: verify the service stays active through a monitoring cycle; check
authorization, absence of connection errors and advancement of SQLite
history. Do not run `check` alongside the service or send a fake free alert.

```bash
cd "$CANDIDATE"
sudo -u "$RUN_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  .venv/bin/python -m username_monitor status
sudo systemctl is-active "$SERVICE"
```

Review the journal privately for connection failures and secret leakage.
Do not paste unredacted logs or unit configuration into issues or chat.

## 5. Rollback

If the candidate fails, restore the untouched old executable and venv by
removing only the rollout drop-in. Do not automatically select a known broken
transport; do not restore the original session from backup without a separate
recovery decision.

```bash
sudo systemctl stop "$SERVICE"
sudo rm "/etc/systemd/system/$SERVICE.d/faketls-rollout.conf"
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
```

Keep the persistent backup private. Legacy MTProxy code can be removed in
another commit only after stable production smoke and separate confirmation.
Cloudflare notifications and `flowseal.service` are outside that cleanup.
