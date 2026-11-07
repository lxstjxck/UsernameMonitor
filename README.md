# Telegram Username Monitor

Лёгкий Python-мониторинг свободных обычных Telegram-юзернеймов. SQLite + MTProto + уведомления в Telegram. **v0.1.0 — только проверка, без автоматической регистрации, массового захвата и операций с Fragment.**

## Возможности

- Проверяет собственный список `usernames.txt` через официальный `account.checkUsername` (пользовательская MTProto-сессия).
- Различает `free`, `occupied`, `fragment`, `invalid`, `unavailable`; технические ошибки не превращаются в «свободно».
- Повторно проверяет `free` перед уведомлением. Не обещает, что Telegram разрешит назначить имя аккаунту или каналу.
- Запоминает результаты и историю изменений в SQLite; уведомляет при переходе в `free` (и при первой проверке, если включено).
- Сохраняет подтверждённые `free` вместе с событием уведомления в одной транзакции SQLite; повторяет неудачную доставку во время паузы между циклами.
- По явному включению локально генерирует и ранжирует кандидатов AutoDiscovery; ручной список всегда проверяется первым.
- Соблюдает паузы между запросами, ограничивает размер цикла, прекращает проверки на `FLOOD_WAIT` и выдерживает паузу, которую требует Telegram.
- Работает без браузера, Docker и веб-панели. Для постоянной работы есть unit `systemd`.

## Требования

Debian/Ubuntu VPS, Python 3.12, `python3-venv`, Git, авторизованная Telegram-сессия, API ID и API Hash с https://my.telegram.org, доступный FakeTLS MTProxy и защищённое окружение для EE/Base64-секрета. Для сообщений бота — токен от @BotFather и `CHAT_ID` (опционально). Для создания репозитория на Windows — Git и GitHub CLI (`gh`).

## Запуск на VPS

Для действующего `username-monitor.service` используйте [процедуру FakeTLS-only обновления](deploy/faketls-only-production.md). Она готовит отдельный checkout и venv, проверяет копию сессии, обновляет override и предусматривает откат. Не запускайте `login` или параллельный `check` с production-сессией. Файл `data/telegram.session` — ключ к аккаунту; храните его вне Git.

В `.env` обязательны `TG_API_ID` и `TG_API_HASH`. В `usernames.txt` — одно имя на строку, от пяти символов; `@` необязателен. `status` читает SQLite и не требует Telegram-конфигурации.

### MTProxy

Telegram RPCs always use FakeTLS. Set `TG_MTPROXY_HOST`, `TG_MTPROXY_PORT`
and `TG_MTPROXY_SECRET_B64` in the process environment. On systemd, use the private `LoadCredential=` file
and [credential runner](deploy/run-with-mtproxy-credential.py): it injects
the secret into the application environment without placing its value in
unit settings or command arguments. The last variable is read before the
project `.env` is loaded; never write it to the project or Git.
The FakeTLS TCP endpoint may differ from the Telegram DC address. The
adapter keeps Telethon's DC ID and connects TCP to the MTProxy without SOCKS5.
Old `TG_TRANSPORT` and `TG_PROXY_*` values in an existing `.env` do not
select a transport. Missing FakeTLS settings fail before any network request.
The `verify-transport` command uses an SQLite backup of the
existing session, never calls `login`, and does not change the original.
For a full preflight, pass `--username testusername12345 --reconnect`:
the one username result is stored in temporary SQLite without notifications,
then a fresh connection requests Telegram config again. Add `--idle-probe`
to measure a 25-second idle interval and a separate series of RPCs five
seconds apart on a copied session, with production stopped. The current
[production procedure](deploy/faketls-only-production.md) includes rollback.
Never put `TG_MTPROXY_SECRET_B64` into `.env`, command arguments or Git.

### Уведомления

1. Создай бота через @BotFather, напиши ему `/start`.
2. Получи `CHAT_ID`, например через `https://api.telegram.org/bot<TOKEN>/getUpdates` (токен не публикуй, ссылку с токеном никому не отправляй).
3. Для прямой отправки заполни `BOT_TOKEN=` и `CHAT_ID=` в `.env`. Если отправка не настроена, монитор продолжит работу и запишет в журнал только факт пропущенного уведомления, без его текста.

Для отправки через защищённый Cloudflare Worker укажи `CHAT_ID`, `BOT_PROXY_URL` и `BOT_PROXY_SECRET`:

```dotenv
CHAT_ID=123456789
BOT_PROXY_URL=https://username-monitor-proxy.svjack69.workers.dev
BOT_PROXY_SECRET=your-worker-secret
```

`TG_CHAT_ID` также принимается как альтернативное имя настройки. Если заданы
обе переменные, их значения должны совпадать; при конфликте запуск
останавливается без вывода идентификаторов. Запросы к Worker отправляются с
`User-Agent: UsernameMonitor/0.1`.

`BOT_PROXY_URL` — только HTTPS-адрес Worker без пути `/bot/sendMessage`, параметров запроса и учётных данных. Монитор добавляет маршрут `/bot/sendMessage` сам, отправляет POST JSON с `chat_id` и `text` и заголовок `Authorization: Bearer <BOT_PROXY_SECRET>`. В этом режиме `BOT_TOKEN` не требуется и не передаётся Worker. Если обе настройки Worker пусты, сохраняется прямая отправка через Telegram Bot API. Заполнить нужно обе настройки Worker; неполная конфигурация останавливает запуск с ошибкой.

Время ожидания HTTP-запроса ограничено 15 секундами. Успехом считается только HTTP 2xx с JSON `{"ok":true}`; ошибка доставки записывается без токенов, секретов, заголовков и текста уведомления. Неуспешные попытки повторяются из SQLite Outbox по расписанию. Маршрут `/apiws` и настройки MTProxy этой опцией не затрагиваются.

### Запуск 24/7 через systemd

Текущая служба на московском VPS — `username-monitor.service`, пользователь `username-monitor`, с `LoadCredential` и override `idle-lifecycle.conf`. Установщик `deploy/install-systemd.sh` выведен из эксплуатации и не создаёт службы. Следуйте [проверяемой процедуре обновления](deploy/faketls-only-production.md); исторические инструкции оставлены только для контекста.

### Команды

`check`, `run`, `diagnose` и `verify-transport` требуют FakeTLS-конфигурации.
На VPS используйте credential runner и `LoadCredential` из
[инструкции](deploy/faketls-only-production.md). `status` работает без
секрета: `.venv/bin/python -m username_monitor status`. `verify-transport`
сам создаёт копию сессии; `diagnose` работает с путём `SESSION_PATH`,
поэтому для него задайте **отдельную копию** и остановите production-сервис.
Не запускайте `check` или `diagnose` параллельно с ним.

`notifications-status` показывает количество ожидающих, доставленных и
отменённых событий, время следующей попытки и безопасные категории ошибок.
Она читает SQLite без Telegram и FakeTLS-секрета:
`DB_PATH=/opt/username-monitor/data/monitor.sqlite3 .venv/bin/python -m username_monitor notifications-status`.

`metrics-status --hours 24` показывает число первичных и подтверждающих
проверок, результаты, длительность RPC (включая подключение и отключение),
FloodWait, завершённые циклы, попытки HTTP-доставки и задержку от создания
события до доставки. `--json` выдаёт тот же отчёт с числовыми полями и `null`
для вычисленных значений без данных (в текстовом отчёте — `no data`). Чтение
SQLite выполняется только в режиме read-only: не нужны FakeTLS-секрет,
TelegramClient или сеть. Пример:

```bash
DB_PATH=/opt/username-monitor/data/monitor.sqlite3 .venv/bin/python -m username_monitor metrics-status --hours 24 --json
```

### AutoDiscovery v1 (выключен по умолчанию)

`AUTO_DISCOVERY_ENABLED=false`, `AUTO_DISCOVERY_PER_CYCLE=1`,
`AUTO_DISCOVERY_DAILY_LIMIT=12` и `AUTO_DISCOVERY_POOL_SIZE=300` — настройки
по умолчанию. При включении генератор локально создаёт около 70% имён
категории startup (обычно 6–10 букв) и 30% short (обычно 5–6 букв).
Рейтинг 0–100 — эвристика длины, произносимости, запоминаемости и сочетаний
букв. Он **не** проверяет товарные знаки, рыночную стоимость или доступность.
Внешние AI API не используются.

```bash
DB_PATH=data/monitor.sqlite3 python -m username_monitor candidates generate --count 300 --seed 42
DB_PATH=data/monitor.sqlite3 python -m username_monitor candidates list --top 20
DB_PATH=data/monitor.sqlite3 python -m username_monitor candidates list --category short --state queued
DB_PATH=data/monitor.sqlite3 python -m username_monitor candidates status
```

`generate` пишет только Candidate Pool в SQLite, не вызывает Telegram и не
меняет `usernames.txt`. `list` и `status` читают SQLite read-only, без
FakeTLS, сети и TelegramClient. Seed позволяет воспроизвести последовательность;
имена, уже записанные или проверенные, повторно в очередь не добавляются.
На рабочем VPS запускайте `generate` только при остановленном monitor unit:
одновременная запись в одну SQLite может кратковременно задержать его цикл.
Активная очередь ограничена `AUTO_DISCOVERY_POOL_SIZE`. Ручные имена сначала
занимают слоты `MAX_PER_CYCLE`, и только один свободный слот может достаться
автокандидату. Скользящий лимит учитывает фактически начатые авто-проверки,
включая неудачные; локальная генерация квоту не расходует. Внутренние
повторы Telethon считаются частью одного логического RPC. Существующие
интервалы и подтверждение `free` вторым успешным RPC сохраняются.

Подтверждённый `free` попадает в существующий SQLite Outbox вместе со
статусом атомарно. Автоуведомление содержит категорию, рейтинг и результат
двух проверок; доставка использует прежний Worker/Bot API и backoff.
При выключении AutoDiscovery или исключении кандидата ожидающее
автоуведомление отменяется, даже если это имя позже добавлено в ручной список.
Ручные уведомления отменяются только по правилам ручного списка и
окончательного статуса. Уже доставленные сообщения не отзываются.
После трёх временных ошибок кандидат исключается; прерванная проверка
восстанавливается после restart и учитывается в лимите. Процедура проверки
миграции на копии базы, безопасного включения и отката находится в
[инструкции AutoDiscovery](deploy/auto-discovery-rollout.md).

Подробные записи RPC, завершённых циклов и фактически начатых HTTP-запросов
хранятся в отдельных SQLite-таблицах 14 дней; удаление старых строк происходит
при старте и затем не чаще раза в день. Считается один логический вызов
`check_username`, включая внутренние повторы Telethon, если они были. Длительность
RPC не включает ожидание между именами и паузу подтверждения `free`. Показатели
доставки не учитывают вызов без настроенного HTTP-маршрута. Метрики
диагностические и не влияют на состояние мониторинга или Outbox. Порядок
обновления действующей службы описан в
[инструкции для телеметрии](deploy/monitoring-telemetry-rollout.md).

Отчёт показывает длительность подключения, `is_user_authorized()`, `help.GetConfigRequest`, `contacts.ResolveUsernameRequest`, `account.CheckUsernameRequest` и отключения. Имя, найденный peer, содержимое сессии и секреты в отчёт не включаются. Лимиты ожидания этапов: подключение 20 с, авторизация 15 с, `help.GetConfigRequest` 15 с, разрешение имени 10 с, проверка имени — `CHECK_TIMEOUT_SECONDS` (по умолчанию 30 с), отключение 5 с. Resolve выполняется перед Check, чтобы сравнить независимые ответы. Успешный Resolve означает, что имя удалось разрешить; `UsernameNotOccupiedError` означает, что разрешить его не удалось, но сам по себе не гарантирует доступность для назначения. `UsernameInvalidError`, `FloodWaitError`, `TimeoutError` и прочие ошибки показываются отдельно, без исходного текста исключения. Если авторизация и `help.GetConfigRequest` отвечают, а один из запросов имени зависает, проблема проявляется на уровне этого RPC после подключения. При `FloodWaitError` команда показывает требуемую паузу и не отправляет следующие запросы.

### Надёжность проверок

При переходе в подтверждённый `free` событие уведомления записывается в
SQLite атомарно со статусом. Первая отправка выполняется сразу; неудачные
попытки повторяются через 30, 60, 120, 240 секунд и далее с ограничением
3600 секунд. Во время паузы между Telegram RPC очередь продолжает работать
через Worker или прямой Bot API без MTProto-подключения. Удаление имени из
`usernames.txt` или новый окончательный статус отменяют ожидающее событие;
временная ошибка RPC сохраняет предыдущее состояние. При потере HTTP-ответа
после принятой Telegram отправки возможен дубль: это доставка **at least
once**, не exactly once. Исторические неудачные уведомления до появления
очереди автоматически не восстанавливаются. Для обновления действующей
службы используйте [процедуру outbox](deploy/notification-outbox-rollout.md).

`CHECK_TIMEOUT_SECONDS=30` ограничивает ожидание **каждого** запроса `account.checkUsername`, включая вторую проверку результата `free`. Допустимый диапазон — от 1 до 86400 секунд. Если запрос не завершился вовремя, он отменяется, ошибка записывается как временная, прежний статус имени сохраняется, текущий цикл прекращается до следующей паузы. Уведомление о свободном имени отправляется только после двух успешных ответов `free`.

Для `login`, `check` и `run` настроены ограниченные повторы Telethon: `request_retries=1` (один дополнительный повтор при поддерживаемых RPC-ошибках), `connection_retries=2`, `retry_delay=1` секунда между повторными подключениями. При проверках автоматическое ожидание `FloodWait` отключено: монитор получает ошибку и выдерживает паузу не менее указанного Telegram времени плюс 60 секунд. Эти параметры не меняют интервалы между именами и циклами.

### Настройки нагрузки

Значения по умолчанию: `CHECK_DELAY_SECONDS=30` (между кандидатами), `CONFIRM_DELAY_SECONDS=10` (вторая проверка доступного имени), `MAX_PER_CYCLE=20`, `CYCLE_PAUSE_SECONDS=600` (между циклами). При большом списке кандидаты обходятся по очереди, старые проверки получают приоритет. Это **не** гарантированно безопасные лимиты — Telegram не публикует универсальную безопасную частоту. При `FLOOD_WAIT` монитор приостанавливается как минимум на указанное Telegram время + 60 секунд. Не увеличивай частоту агрессивно.

## Ограничения и ответственность

- `account.checkUsername` проверяет доступность для пользовательского аккаунта. Для конкретного канала Telegram предоставляет отдельный `channels.checkUsername`. Даже `free` не резервирует имя: оно может быть занято другим человеком между проверкой и назначением.
- Telegram может ограничивать запросы, не отдавать временно освобождённые имена в свободную регистрацию, а также применять правила против сквоттинга. Не используйте множество аккаунтов для обхода лимитов.
- Не собираем личные данные владельцев чужих имён, не отправляем запросы на регистрацию и не торгуем именами автоматически. Покупка/продажа — отдельный ручной процесс.
- Официальная документация: https://core.telegram.org/method/account.checkUsername и https://core.telegram.org/method/channels.checkUsername

## Тесты

```bash
python3 -m unittest discover -s tests -v
```
