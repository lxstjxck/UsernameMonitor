# Telegram Username Monitor

Лёгкий Python-мониторинг свободных обычных Telegram-юзернеймов. SQLite + MTProto + уведомления в Telegram. **v0.1.0 — только проверка, без автоматической регистрации, массового захвата и операций с Fragment.**

## Возможности

- Проверяет собственный список `usernames.txt` через официальный `account.checkUsername` (пользовательская MTProto-сессия).
- Различает `free`, `occupied`, `fragment`, `invalid`, `unavailable`; технические ошибки не превращаются в «свободно».
- Повторно проверяет `free` перед уведомлением. Не обещает, что Telegram разрешит назначить имя аккаунту или каналу.
- Запоминает результаты и историю изменений в SQLite; уведомляет при переходе в `free` (и при первой проверке, если включено).
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

Время ожидания HTTP-запроса ограничено 15 секундами. Успехом считается только HTTP 2xx с JSON `{"ok":true}`; ошибка доставки записывается без токенов, секретов, заголовков и текста уведомления. Автоматических повторов отправки нет: после сбоя уведомление может быть пропущено, поскольку статус имени уже записан. Маршрут `/apiws` и настройки MTProxy этой опцией не затрагиваются.

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

Отчёт показывает длительность подключения, `is_user_authorized()`, `help.GetConfigRequest`, `contacts.ResolveUsernameRequest`, `account.CheckUsernameRequest` и отключения. Имя, найденный peer, содержимое сессии и секреты в отчёт не включаются. Лимиты ожидания этапов: подключение 20 с, авторизация 15 с, `help.GetConfigRequest` 15 с, разрешение имени 10 с, проверка имени — `CHECK_TIMEOUT_SECONDS` (по умолчанию 30 с), отключение 5 с. Resolve выполняется перед Check, чтобы сравнить независимые ответы. Успешный Resolve означает, что имя удалось разрешить; `UsernameNotOccupiedError` означает, что разрешить его не удалось, но сам по себе не гарантирует доступность для назначения. `UsernameInvalidError`, `FloodWaitError`, `TimeoutError` и прочие ошибки показываются отдельно, без исходного текста исключения. Если авторизация и `help.GetConfigRequest` отвечают, а один из запросов имени зависает, проблема проявляется на уровне этого RPC после подключения. При `FloodWaitError` команда показывает требуемую паузу и не отправляет следующие запросы.

### Надёжность проверок

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
