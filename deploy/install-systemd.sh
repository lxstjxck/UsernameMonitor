#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run: sudo bash deploy/install-systemd.sh \"$(id -un)\"" >&2
  exit 1
fi
RUN_USER="${1:-}"
if [[ -z "$RUN_USER" || "$RUN_USER" == root ]]; then
  echo 'Specify a dedicated, non-root Linux user: sudo bash deploy/install-systemd.sh monitor' >&2
  exit 1
fi
if ! id "$RUN_USER" >/dev/null 2>&1; then
  echo "User does not exist: $RUN_USER" >&2
  exit 1
fi
RUN_GROUP="$(id -gn "$RUN_USER")"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" || ! -f "$PROJECT_DIR/.env" ]]; then
  echo 'Missing .venv/bin/python or .env. Follow README setup first.' >&2
  exit 1
fi
if [[ ! -f "$PROJECT_DIR/data/telegram.session" ]]; then
  echo 'Login first as the service user: python -m username_monitor login' >&2
  exit 1
fi
python3 - "$PROJECT_DIR" "$RUN_USER" "$RUN_GROUP" <<'PY'
from pathlib import Path
import sys
folder, user, group = sys.argv[1:]
service = (Path(folder) / 'deploy/telegram-username-monitor.service.template').read_text()
for key, val in {'__PROJECT_DIR__': folder, '__RUN_USER__': user, '__RUN_GROUP__': group}.items():
    if any(ch in val for ch in '\n\r%'):
        raise SystemExit('Unsafe value for systemd unit')
    service = service.replace(key, val)
Path('/etc/systemd/system/telegram-username-monitor.service').write_text(service)
PY
systemctl daemon-reload
systemctl enable --now telegram-username-monitor.service
systemctl --no-pager status telegram-username-monitor.service || true
