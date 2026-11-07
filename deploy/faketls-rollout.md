# FakeTLS rollout for /opt/username-monitor

These commands are for a later, explicitly approved deployment. Do not run
`login`, `check`, `run`, or the existing `diagnose` during the preflight:
`verify-transport` is the read-only two-RPC probe on a disposable SQLite copy.
The existing /opt/username-monitor/data/telegram.session is never moved,
deleted, or opened writable by the probe.

## Prepare an isolated candidate

After the reviewed change has been pushed, on the VPS:

```bash
RUN_USER="$(systemctl show -P User telegram-username-monitor.service)"
test -n "$RUN_USER"
sudo install -d -m 700 -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" /opt/username-monitor-candidate
sudo -u "$RUN_USER" git clone https://github.com/lxstjxck/UsernameMonitor.git /opt/username-monitor-candidate
cd /opt/username-monitor-candidate
sudo -u "$RUN_USER" python3 -m venv .venv
sudo -u "$RUN_USER" .venv/bin/python -m pip install -r requirements.txt
sudo install -m 600 -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" /opt/username-monitor/.env .env
sudo -u "$RUN_USER" .venv/bin/python -m unittest discover -s tests -v
```

The service account needs read access to the existing project and session.
Do not change ownership of the production session.

Create the private systemd environment file on the VPS. Edit locally on the
VPS; do not paste the secret into shell arguments, chat, logs, or Git.
`TG_MTPROXY_SECRET_B64` must be the purchased EE/Base64 value. The program
requires exactly 22 decoded bytes (EE + 16-byte key + five ASCII SNI bytes).

```bash
sudo install -d -m 700 /etc/username-monitor
sudo install -m 600 /dev/null /etc/username-monitor/mtproxy.env
sudoedit /etc/username-monitor/mtproxy.env
# Set these four lines in the editor, without quoting or exporting them:
# TG_TRANSPORT=faketls
# TG_MTPROXY_HOST=45.130.124.28
# TG_MTPROXY_PORT=443
# TG_MTPROXY_SECRET_B64=<private value>
sudo chmod 600 /etc/username-monitor/mtproxy.env
```

Keep the secret out of the project's `.env`. The service and the isolated
probe read it from the protected process environment.

## Back up the live session and verify through a disposable copy

The SQLite online backup is safe while the old service is running. The backup
destination must not already exist. Set a private destination name, then run:

```bash
cd /opt/username-monitor-candidate
BACKUP="/opt/username-monitor/data/telegram-before-faketls-$(date +%Y%m%d-%H%M%S).session"
sudo -u "$RUN_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path("/opt/username-monitor/data/telegram.session"), Path(sys.argv[1]))' "$BACKUP"
sudo -u "$RUN_USER" .venv/bin/python -c 'import sys, sqlite3; db=sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True); assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"; db.close()' "$BACKUP"
```

The service must remain active and unchanged at this point. Run the candidate
probe via systemd so the service user receives the protected environment.
Override SESSION_PATH to the persistent backup. The probe creates another
private SQLite copy, runs authenticated `get_me` and `help.GetConfig` on it,
then deletes that temporary copy. It does not call `start()` or attempt login.

```bash
sudo systemd-run --wait --pipe --collect \
  -p "User=$RUN_USER" \
  -p "WorkingDirectory=/opt/username-monitor-candidate" \
  -p "EnvironmentFile=/etc/username-monitor/mtproxy.env" \
  -p "Environment=SESSION_PATH=$BACKUP" \
  /opt/username-monitor-candidate/.venv/bin/python -m username_monitor verify-transport
```

Expected output: `get_me: PASS`, `help.GetConfig: PASS`, exit code 0.
Any failure blocks the service switch. Do not use `login` to repair the
backup. The separate raw AuthKey/PingRequest test does not replace this
authorized Telethon RPC probe.

## Switch production only after both gates pass

Prepare a separate virtual environment under the production project, then
stop only `telegram-username-monitor.service`. Preserve the old environment.
Use the reviewed commit SHA in place of `<reviewed-sha>`.

```bash
cd /opt/username-monitor
git fetch origin main
git rev-parse HEAD > /tmp/username-monitor-previous-sha
git checkout --detach <reviewed-sha>
sudo -u "$RUN_USER" python3 -m venv .venv-faketls
sudo -u "$RUN_USER" .venv-faketls/bin/python -m pip install -r requirements.txt
sudo systemctl stop telegram-username-monitor.service
mv .venv .venv-before-faketls
mv .venv-faketls .venv
sudo install -d /etc/systemd/system/telegram-username-monitor.service.d
printf '[Service]\nEnvironmentFile=/etc/username-monitor/mtproxy.env\n' | sudo tee /etc/systemd/system/telegram-username-monitor.service.d/faketls.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl start telegram-username-monitor.service
sudo systemctl --no-pager status telegram-username-monitor.service
sudo journalctl -u telegram-username-monitor.service -n 50 --no-pager
```

Smoke: confirm the service stays active through a monitoring cycle, the
existing session remains authorized, and no connection errors appear. Check
`status` to inspect stored results. Do not run an extra `check` concurrently
with the service and do not test notifications by sending a message.

## One-setting transport rollback

Change only `TG_TRANSPORT=faketls` to `TG_TRANSPORT=legacy` in the protected
environment file, then restart the monitor service. `legacy` selects the
previous `TG_PROXY_*` MTProxy when configured in the project's `.env`,
otherwise direct TCP. The FakeTLS host and secret can remain in the protected
environment and are ignored in legacy mode. Verify service status and logs.

```bash
sudoedit /etc/username-monitor/mtproxy.env
sudo systemctl restart telegram-username-monitor.service
sudo systemctl --no-pager status telegram-username-monitor.service
```

Do not touch `flowseal.service`, Cloudflare Worker routes, or notification
settings. If a full code rollback is necessary, stop the monitor, restore the
recorded commit and `.venv-before-faketls`, then restart it; leave
`data/telegram.session` and `.env` untouched.
