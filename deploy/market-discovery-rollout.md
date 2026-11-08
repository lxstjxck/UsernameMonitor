# Market-Driven Discovery v2: только подготовка и локальное сравнение

Этот документ **не является разрешением на deploy**. Действующий релиз,
`username-monitor.service`, Telegram-сессия, production SQLite и секреты
остаются без изменений. Работайте в новом checkout/venv и на SQLite online
backup (сделанном отдельно в безопасный момент). В рабочую базу не пишите.

1. Проверить `git rev-parse HEAD`, `python -m unittest discover -s tests -q`,
   `python -m pip check` и `git diff --check` в новом checkout.
2. Создать SQLite backup текущей базы встроенным backup API, без прямого
   копирования открытого `.sqlite3` (и не копировать `.session` для аналитики):

   ```bash
   python - <<'PY'
   import sqlite3
   from pathlib import Path
   src = sqlite3.connect(Path('data/monitor.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
   dst = sqlite3.connect('/tmp/market-experiment.sqlite3')
   try:
       src.backup(dst)
       assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
   finally:
       src.close()
       dst.close()
   PY
   ```

3. В тестовой среде с `DB_PATH=/tmp/market-experiment.sqlite3` выполнить
   `market generate --count 300 --seed 42`. Это заменит только таблицу
   `market_test_candidates` в копии. Выполнить `market refresh` (открытый
   источник; не требуется Telegram/TON), `market status`, затем
   `market compare --top 20` и `market explain --username-market amber`.
   Для оценки базовой очереди в копии выполнить `market score`, затем
   `market compare --pool base --top 20` на одинаковом наборе; отчёт указывает источник,
   время снимка, уверенность и причины. `market exclusions` выводит причины
   исключения отдельных продаж и аукционов. При нынешнем TOP-срезе
   `commercial_score` должен оставаться пустым, а обе очереди совпадать.
   Включать флаг сейчас не следует.
4. Запустить `market promote --count 20 --dry-run` на **копии**: без `--apply` это
   read-only план с именами, категориями и причинами. Сверить его с ручным
   списком, `state`, `changes`, Outbox и объёмом очереди. Только на копии
   выполнить `MARKET_DISCOVERY_ENABLED=true DB_PATH=/tmp/market-experiment.sqlite3
   .venv/bin/python -m username_monitor market promote --count 20 --apply`,
   повторить dry-run и убедиться,
   что **уже проверенные** статусы/попытки/события не изменились, а размер
   активного пула не превысил 300. При полном пуле план перечисляет заменяемые
   непроверенные `queued`: они остаются в истории как `discarded` с отдельной
   причиной. Применение к production требует отдельного решения.
5. Сравнить `SELECT username,score,status,attempts FROM discovery_candidates`,
   `SELECT COUNT(*) FROM state`, `changes`, `notification_outbox`,
   `discovery_attempts` до и после эксперимента. `PRAGMA integrity_check`
   должен вернуть `ok`.
6. Если появятся полные проверяемые записи сделок и снимки, накопленные
   **до** контрольной даты, проверить walk-forward с `market compare
   --as-of 2026-11-01T00:00:00Z`: сравнение не использует поздние снимки.
   Нынешний TOP-срез рынка смещён в пользу дорогих продаж, поэтому
   медианы категорий и точность/ROI не вычисляются. Цена четырёхбуквенных
   collectibles и незавершённые ставки не переносятся на обычные имена.

## Возможное включение после отдельного решения

`MARKET_DISCOVERY_ENABLED=true` меняет **лишь порядок выбора одного
автокандидата** из текущей очереди при свежем `observed` рейтинге с
`confidence=medium`; иначе используется прежний `name_score`. При выключенном
`AUTO_DISCOVERY_ENABLED` ничего не выбирается. Дневной лимит 12, задержки,
FakeTLS, Telegram RPC и Outbox остаются прежними. Минимум 7200 секунд между
началом автоматических попыток хранится в SQLite и не задерживает ручные
проверки каждые 600 секунд. Полезность рыночного
эвристического приоритета не подтверждена продажами и требует наблюдения.

Откат флага: `MARKET_DISCOVERY_ENABLED=false`; таблицы рынка оставляют в
SQLite для истории. Откат к прежнему релизу не требует удаления рыночных
таблиц, но его нужно проверять на копии рабочей базы и сохранять backup.
Никакие ключи кошелька, Telegram-сессия и токены источнику не передаются.

Data: [TG Gifts (tggifts.com)](https://tggifts.com), CC BY 4.0.
