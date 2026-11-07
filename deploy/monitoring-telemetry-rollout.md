# Monitoring telemetry: обновление username-monitor.service

**Подготовлено, не выполнялось.** Начальная версия `2899ecb45f9b5bf4501f58269419098cc94d720a`
в `/opt/username-monitor-release-2899ecb45f9b`. Перед началом нужен опубликованный
и проверенный SHA этой ветки и отдельное подтверждение переключения production.
Оставьте прежние release, venv и backup. Не запускайте `login`, второй клиент
на исходной сессии или тестовые сообщения о свободном username в настоящий чат.

## 1. Отдельный checkout и проверки при работающей старой службе

Проверьте установленные unit, `LoadCredential` и override без вывода их
секретных значений. Если путь/ревизия отличаются, остановите процедуру и
уточните фактическое состояние. Новые зависимости не добавлены.

```bash
set -euo pipefail
SERVICE=username-monitor.service
APP_USER=username-monitor
OLD_SHA=2899ecb45f9b5bf4501f58269419098cc94d720a
NEW_SHA='<полный-SHA-опубликованного-коммита>'
OLD_RELEASE=/opt/username-monitor-release-2899ecb45f9b
NEW_RELEASE="/opt/username-monitor-release-${NEW_SHA:0:12}"
OVERRIDE="/etc/systemd/system/$SERVICE.d/idle-lifecycle.conf"
ROLLBACK_OVERRIDE="/etc/username-monitor/idle-lifecycle-before-telemetry-${NEW_SHA:0:12}"
sudo systemctl is-active --quiet "$SERVICE"
test "$(sudo systemctl show -P WorkingDirectory "$SERVICE")" = "$OLD_RELEASE"
test "$(sudo systemctl show -P User "$SERVICE")" = "$APP_USER"
test "$(sudo -u "$APP_USER" git -C "$OLD_RELEASE" rev-parse HEAD)" = "$OLD_SHA"
sudo test -f "$OVERRIDE"
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

Unit и `.env` могут содержать чувствительные настройки. Проверяйте их
локально, не присылая вывод; не помещайте FakeTLS credential в `.env`, CLI,
журнал или Git. `TG_MTPROXY_SECRET_B64` остаётся только в `LoadCredential`.
Никакие файлы рабочего checkout при работе службы не меняются.

## 2. Остановить только монитор, сделать online backup, проверить миграцию копии

Все команды ниже исполняются из нового checkout. `backup_session` выполняет
SQLite online backup, учитывая WAL. Исходные файлы данных остаются нетронутыми;
тест миграции запускается на отдельной копии резервной копии.

```bash
sudo systemctl stop "$SERVICE"
test "$(sudo systemctl is-active "$SERVICE")" = inactive
STAMP="$(date +%Y%m%d-%H%M%S)"
SESSION_BACKUP="/opt/username-monitor/data/telegram-before-telemetry-$STAMP.session"
DB_BACKUP="/opt/username-monitor/data/monitor-before-telemetry-$STAMP.sqlite3"
MIGRATION_COPY="/opt/username-monitor/data/monitor-telemetry-test-$STAMP.sqlite3"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/telegram.session "$SESSION_BACKUP"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' /opt/username-monitor/data/monitor.sqlite3 "$DB_BACKUP"
sudo -u "$APP_USER" .venv/bin/python -c 'import sys; from pathlib import Path; from username_monitor.transport_probe import backup_session; backup_session(Path(sys.argv[1]), Path(sys.argv[2]))' "$DB_BACKUP" "$MIGRATION_COPY"
sudo -u "$APP_USER" .venv/bin/python - "$DB_BACKUP" "$MIGRATION_COPY" <<'PY'
import sqlite3, sys
from pathlib import Path
from username_monitor.telemetry import Telemetry
old, copy = map(Path, sys.argv[1:])
def inspect(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                for name in ('state', 'changes', 'notification_outbox')}
before = inspect(old)
with Telemetry(copy) as metrics:
    assert metrics.db is not None
assert inspect(copy) == before
with sqlite3.connect(copy.resolve().as_uri() + '?mode=ro', uri=True) as db:
    assert all(db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone()
               for table in ('rpc_metrics', 'delivery_metrics', 'cycle_metrics'))
print('TELEMETRY_MIGRATION_COPY_OK')
PY
sudo -u "$APP_USER" env DB_PATH="$MIGRATION_COPY" \
  .venv/bin/python -m username_monitor metrics-status --hours 24 --json
```

Дополнительная авторизованная проверка использует **копию сессии**, пока
служба остановлена; runner получает секрет через transient `LoadCredential`
и выводит только безопасный результат `get_me` / `help.GetConfig`:

```bash
sudo systemd-run --wait --pipe --collect \
  -p "User=$APP_USER" -p "WorkingDirectory=$NEW_RELEASE" \
  -p LoadCredential=mtproxy_secret_b64:/etc/username-monitor/mtproxy-secret.b64 \
  "$NEW_RELEASE/.venv/bin/python" "$NEW_RELEASE/deploy/run-with-mtproxy-credential.py" \
  --mtproxy-host 45.130.124.28 --mtproxy-port 443 \
  --session-path "$SESSION_BACKUP" verify-transport
```

Требуются `TELEMETRY_MIGRATION_COPY_OK`, корректный JSON и PASS обоих
авторизованных RPC. При ошибке запускайте старую службу с прежним override;
не мигрируйте рабочую БД вручную.

## 3. Переключение после отдельного подтверждения

Проверьте локально существующий `ExecStart` и точный синтаксис override;
заменяется только путь checkout/venv, сохраняются текущие аргументы,
прочие настройки override и `LoadCredential`. Перед заменой убедитесь, что
override действительно ссылается на старый release:

```bash
sudo install -m 600 -o root -g root "$OVERRIDE" "$ROLLBACK_OVERRIDE"
sudo grep -F -q "$OLD_RELEASE" "$OVERRIDE"
sudo sed -i "s|$OLD_RELEASE|$NEW_RELEASE|g" "$OVERRIDE"
test "$(sudo systemctl show -P User "$SERVICE")" = "$APP_USER"
sudo systemd-analyze verify "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
sudo -u "$APP_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  "$NEW_RELEASE/.venv/bin/python" -m username_monitor metrics-status --hours 24 --json
sudo -u "$APP_USER" env DB_PATH=/opt/username-monitor/data/monitor.sqlite3 \
  "$NEW_RELEASE/.venv/bin/python" -m username_monitor notifications-status
```

Проверьте `systemctl status` и `journalctl -u username-monitor.service`
локально: первый полный цикл, сохранение state/changes, работу Outbox и
появление первичных/подтверждающих RPC в отчёте. Дождитесь обычных 600 секунд
и следующего цикла; убедитесь, что во время паузы нет постоянного MTProto
reconnect. Наблюдайте только реальные события доставки; не создавайте
ложный `free` и не выполняйте `check` параллельно с unit.

## 4. Откат

Только после подтверждения: остановите монитор, восстановите сохранённый
override и запустите прежний checkout/venv. Не восстанавливайте backup БД
автоматически: старая версия игнорирует новые таблицы статистики, а все
события Outbox и история должны остаться в рабочей БД. Сохраните обе версии,
копии сессии и базы. `flowseal.service` и Worker не трогайте.

```bash
sudo systemctl stop "$SERVICE"
sudo install -m 644 -o root -g root "$ROLLBACK_OVERRIDE" "$OVERRIDE"
sudo systemd-analyze verify "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
test "$(sudo systemctl show -P WorkingDirectory "$SERVICE")" = "$OLD_RELEASE"
```

Телеметрия считает логические вызовы, а не внутренние повторы Telethon.
Возможна доставка at least once и повторное сообщение после потерянного
HTTP-ответа. Ошибки необязательной статистики видны только как безопасная
категория в журнале; часть измерений при таком сбое может отсутствовать.
