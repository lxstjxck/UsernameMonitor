# AutoDiscovery v1: подготовка и включение username-monitor.service

**Подготовлено, не выполнялось.** База: `bb4bba6f3525e39a87f58e32fa5399f76acb3428`,
release `/opt/username-monitor-release-bb4bba6f3525`. Перед production
нужны опубликованный SHA новой ветки и отдельное разрешение на deploy/restart.
Не меняйте исходную Telegram-сессию, Worker, `flowseal.service`, старые
релизы и резервные копии. Не запускайте параллельный TelegramClient и не
создавайте тестовое сообщение о свободном username в рабочем чате.

## 1. Подготовить отдельный checkout и venv

Проверяйте unit и override локально, не копируя секреты или содержимое `.env`
в переписку/журнал. Не меняйте файлы исполняемого старого checkout во время
его работы. Новые зависимости не требуются.

```bash
set -euo pipefail
SERVICE=username-monitor.service
APP_USER=username-monitor
OLD_SHA=bb4bba6f3525e39a87f58e32fa5399f76acb3428
NEW_SHA='<полный-SHA-опубликованного-коммита>'
OLD_RELEASE=/opt/username-monitor-release-bb4bba6f3525
NEW_RELEASE="/opt/username-monitor-release-${NEW_SHA:0:12}"
OVERRIDE="/etc/systemd/system/$SERVICE.d/idle-lifecycle.conf"
ROLLBACK_OVERRIDE="/etc/username-monitor/idle-lifecycle-before-discovery-${NEW_SHA:0:12}"
AUTO_OVERRIDE="/etc/systemd/system/$SERVICE.d/auto-discovery.conf"
sudo systemctl is-active --quiet "$SERVICE"
test "$(sudo systemctl show -P WorkingDirectory "$SERVICE")" = "$OLD_RELEASE"
test "$(sudo systemctl show -P User "$SERVICE")" = "$APP_USER"
test "$(sudo -u "$APP_USER" git -C "$OLD_RELEASE" rev-parse HEAD)" = "$OLD_SHA"
sudo test -f "$OVERRIDE"
sudo test -f /opt/username-monitor/data/telegram.session
sudo test -f /opt/username-monitor/data/monitor.sqlite3
sudo test -f /opt/username-monitor/usernames.txt
sudo test ! -e "$AUTO_OVERRIDE"
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

Убедитесь, что `LoadCredential` по-прежнему задаёт FakeTLS-секрет; значение
не должно оказаться в `.env`, аргументах процесса, unit, Git или журналах.
Пока переменная `AUTO_DISCOVERY_ENABLED` отсутствует, функция выключена.

## 2. Остановить только монитор, сделать online backup и проверить миграцию копии

`backup_session` использует SQLite online backup, учитывая WAL. Миграцию
выполняйте только на отдельной **копии** backup, сравнивая исходные таблицы.

```bash
sudo systemctl stop "$SERVICE"
test "$(sudo systemctl is-active "$SERVICE")" = inactive
STAMP="$(date +%Y%m%d-%H%M%S)"
SESSION_BACKUP="/opt/username-monitor/data/telegram-before-discovery-$STAMP.session"
DB_BACKUP="/opt/username-monitor/data/monitor-before-discovery-$STAMP.sqlite3"
MIGRATION_COPY="/opt/username-monitor/data/monitor-discovery-test-$STAMP.sqlite3"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/telegram.session "$SESSION_BACKUP"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/monitor.sqlite3 "$DB_BACKUP"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' "$DB_BACKUP" "$MIGRATION_COPY"
sudo -u "$APP_USER" .venv/bin/python - "$DB_BACKUP" "$MIGRATION_COPY" <<'PY'
import sqlite3, sys
from pathlib import Path
from username_monitor.store import Store
from username_monitor.discovery import CandidatePool
original, candidate = map(Path, sys.argv[1:])
tables = ('state','changes','notification_outbox','rpc_metrics','delivery_metrics','cycle_metrics')
def inspect(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                for name in tables if name in existing}
before = inspect(original)
with Store(candidate) as store:
    CandidatePool(store)
assert inspect(candidate) == before
print('DISCOVERY_MIGRATION_COPY_OK')
PY
sudo -u "$APP_USER" env DB_PATH="$MIGRATION_COPY" \
  .venv/bin/python -m username_monitor candidates status
sudo -u "$APP_USER" env DB_PATH="$MIGRATION_COPY" \
  .venv/bin/python -m username_monitor candidates top
sudo -u "$APP_USER" env DB_PATH="$MIGRATION_COPY" \
  .venv/bin/python -m username_monitor metrics-status --hours 24 --json
```

Если на копии уже есть Candidate Pool версии v1, запустите явную переоценку
только на `MIGRATION_COPY`, затем сравните все уже проверенные строки с
неизменённым backup. Команда не должна менять историю, Outbox или метрики:

```bash
sudo -u "$APP_USER" env DB_PATH="$MIGRATION_COPY" \
  .venv/bin/python -m username_monitor candidates rescore
sudo -u "$APP_USER" .venv/bin/python - "$DB_BACKUP" "$MIGRATION_COPY" <<'PY'
import sqlite3, sys
from pathlib import Path
from contextlib import closing
old, candidate = map(Path, sys.argv[1:])
def inspected(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checked = db.execute("""SELECT username,status,attempts,last_checked_at,score,reason
            FROM discovery_candidates WHERE status!='queued' OR attempts>0 OR last_checked_at IS NOT NULL
            ORDER BY username""").fetchall() if 'discovery_candidates' in tables else []
        counts = {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                  for name in ('state','changes','notification_outbox',
                               'rpc_metrics','delivery_metrics','cycle_metrics') if name in tables}
        return checked, counts
assert inspected(old) == inspected(candidate)
print('DISCOVERY_RESCORE_COPY_OK')
PY
sudo -u "$APP_USER" env DB_PATH="$MIGRATION_COPY" \
  .venv/bin/python -m username_monitor candidates top
```

Команда рескоринга явная: запуск нового release **не** меняет оценки
существующих строк автоматически. Не запускайте её на рабочей базе при
работающей службе.

Для авторизованной проверки только **копии** сессии используйте прежний
credential runner, пока unit остановлен; требуются PASS `get_me` и
`help.GetConfig`. Не выполняйте `check` с исходной сессией рядом с unit:

```bash
sudo systemd-run --wait --pipe --collect \
  -p "User=$APP_USER" -p "WorkingDirectory=$NEW_RELEASE" \
  -p LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64 \
  "$NEW_RELEASE/.venv/bin/python" "$NEW_RELEASE/deploy/run-with-mtproxy-credential.py" \
  --mtproxy-host 45.130.124.28 --mtproxy-port 443 \
  --session-path "$SESSION_BACKUP" verify-transport
```

Если проверка не прошла, запустите старую службу с прежним override и
остановите обновление. Не восстанавливайте рабочую БД из backup автоматически.

## 3. Включать только после проверки новой версии с флагом false

Скопируйте действующий override за пределы `.d`, затем замените в нём только
путь release; сохраняйте аргументы, `LoadCredential` и все прочие настройки.
Проверка первого полного цикла и Outbox проходит **без** автоматических RPC.

```bash
sudo install -m 600 -o root -g root "$OVERRIDE" "$ROLLBACK_OVERRIDE"
sudo grep -F -q "$OLD_RELEASE" "$OVERRIDE"
sudo sed -i "s|$OLD_RELEASE|$NEW_RELEASE|g" "$OVERRIDE"
sudo systemd-analyze verify "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
sudo -u "$APP_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  "$NEW_RELEASE/.venv/bin/python" -m username_monitor candidates status
```

После обычного ручного цикла и 600-секундной паузы проверьте state/Outbox,
метрики, отсутствие лишних RPC и reconnect. Отдельное включение выполняйте
только с разрешения на production. Для первого включения сохраните лимиты:

```bash
sudo tee "$AUTO_OVERRIDE" >/dev/null <<'EOF'
[Service]
Environment=AUTO_DISCOVERY_ENABLED=true AUTO_DISCOVERY_PER_CYCLE=1 AUTO_DISCOVERY_DAILY_LIMIT=12 AUTO_DISCOVERY_POOL_SIZE=300
EOF
sudo systemd-analyze verify "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
sudo -u "$APP_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  "$NEW_RELEASE/.venv/bin/python" -m username_monitor candidates status
sudo -u "$APP_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  "$NEW_RELEASE/.venv/bin/python" -m username_monitor metrics-status --hours 24 --json
```

Наблюдайте полный цикл: сначала ручные имена; не более одного автоимени в
свободном слоте; один логический первичный RPC, второй только при `free`;
следующие циклы сохраняют исходный интервал. Не отправляйте искусственный
`free` в настоящий чат. Задержка HTTP Outbox и `FloodWait` должны сохранять
прежние правила. `candidates status` показывает остаток скользящих 24 часов.

## 4. Откат

Сначала остановите только монитор. Уберите drop-in включения за пределы
`.d`, восстановите сохранённый override и запустите прежний release/venv.
В старом релизе автоимена не проверяются; его Outbox отменит ещё ожидающие
автоуведомления как отсутствующие в `usernames.txt`. Доставленные сообщения
не отзываются. **Не восстанавливайте БД из backup**: кандидатные таблицы,
Outbox, история и телеметрия остаются на месте; старый код игнорирует новые
колонки и таблицы. При последующем повторном включении отменённые события
не воскреснут автоматически.

```bash
sudo systemctl stop "$SERVICE"
sudo install -d -m 700 /etc/username-monitor/disabled-overrides
if sudo test -f "$AUTO_OVERRIDE"; then
  sudo mv "$AUTO_OVERRIDE" /etc/username-monitor/disabled-overrides/auto-discovery-$STAMP.conf
fi
sudo install -m 644 -o root -g root "$ROLLBACK_OVERRIDE" "$OVERRIDE"
sudo systemd-analyze verify "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
test "$(sudo systemctl show -P WorkingDirectory "$SERVICE")" = "$OLD_RELEASE"
```
