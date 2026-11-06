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

Debian/Ubuntu VPS, Python >= 3.10, `python3-venv`, Git, Telegram-аккаунт, API ID и API Hash с https://my.telegram.org. Для сообщений бота — токен от @BotFather и `CHAT_ID` (опционально). Для создания репозитория на Windows — Git и GitHub CLI (`gh`).

## Быстрый запуск на VPS

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
# После публикации репозитория на GitHub:
git clone https://github.com/lxstjxck/TelegramUsernameMonitor.git
cd TelegramUsernameMonitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
mkdir -p data
chmod 700 data
nano .env
nano usernames.txt
.venv/bin/python -m username_monitor login
.venv/bin/python -m username_monitor check
.venv/bin/python -m username_monitor status
```

В `.env` обязательны `TG_API_ID` и `TG_API_HASH`. В `usernames.txt` — **один кандидат на строку**, начиная с пяти символов; `@` необязателен. Примеры по умолчанию закомментированы, заполни файл сам. Для входа команда `login` попросит номер, код Telegram и при необходимости пароль 2FA. Не передавай их никому. Файл `data/telegram.session` — ключ к аккаунту; не выкладывай его в Git и не пересылай. Для работы через SSH первый вход удобно выполнить вручную.

### MTProxy

Для подключения Telethon через MTProxy заполните в `.env` все три параметра:

```dotenv
TG_PROXY_HOST=proxy.example
TG_PROXY_PORT=443
TG_PROXY_SECRET=0123456789abcdef0123456789abcdef
```

`TG_PROXY_PORT` должен быть целым числом от 1 до 65535. Секрет — 32 шестнадцатеричных символа; также поддерживается префикс `dd` перед ними. Если все три поля пусты, используется прямое подключение. Частично заполненная или некорректная конфигурация вызывает ошибку до подключения. Одинаковые параметры MTProxy используются для `login`, `check` и `run`; команда `status` не подключается к Telegram. Храните `.env` с секретом вне Git.

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

`BOT_PROXY_URL` — только HTTPS-адрес Worker без пути `/bot/sendMessage`, параметров запроса и учётных данных. Монитор добавляет маршрут `/bot/sendMessage` сам, отправляет POST JSON с `chat_id` и `text` и заголовок `Authorization: Bearer <BOT_PROXY_SECRET>`. В этом режиме `BOT_TOKEN` не требуется и не передаётся Worker. Если обе настройки Worker пусты, сохраняется прямая отправка через Telegram Bot API. Заполнить нужно обе настройки Worker; неполная конфигурация останавливает запуск с ошибкой.

Время ожидания HTTP-запроса ограничено 15 секундами. Успехом считается только HTTP 2xx с JSON `{"ok":true}`; ошибка доставки записывается без токенов, секретов, заголовков и текста уведомления. Автоматических повторов отправки нет: после сбоя уведомление может быть пропущено, поскольку статус имени уже записан. Маршрут `/apiws` и настройки MTProxy этой опцией не затрагиваются.

### Запуск 24/7 через systemd

Устанавливай проект в каталог, который доступен Linux-пользователю, под которым будет работать сервис. `login` и тестовый `check` должны быть выполнены **тем же пользователем**, что запускает сервис. Если подключаешься по SSH как обычный (не root) пользователь и работал от его имени, можно использовать его же. Если входишь как root, создай отдельного пользователя и передай ему права на папку проекта до настройки `.env` и авторизации Telegram.

```bash
# Пример: проект уже установлен, .env настроен,
# сессия авторизована под текущим НЕ-root SSH-пользователем.
sudo bash deploy/install-systemd.sh "$(id -un)"
sudo systemctl status telegram-username-monitor
sudo journalctl -u telegram-username-monitor -f
```

Скрипт явно проверяет `.env`, виртуальное окружение, сессию и существование пользователя. Он включает сервис на автозапуск. Для остановки:

```bash
sudo systemctl stop telegram-username-monitor
sudo systemctl disable telegram-username-monitor
```

### Команды

```bash
.venv/bin/python -m username_monitor login   # создать пользовательскую сессию
.venv/bin/python -m username_monitor check   # выполнить один цикл
.venv/bin/python -m username_monitor run     # циклы до Ctrl+C
.venv/bin/python -m username_monitor status  # показать последние статусы
.venv/bin/python -m username_monitor diagnose --username testusername12345  # диагностика MTProto RPC
```

Команда `diagnose` использует существующий файл сессии и те же настройки MTProxy, что `check` и `run`. Она ничего не записывает в SQLite и не отправляет уведомления. Запускайте её из каталога проекта на VPS с настроенным `.env`:

```bash
.venv/bin/python -m username_monitor diagnose --username testusername12345
```

Отчёт показывает длительность подключения, `is_user_authorized()`, `help.GetConfigRequest`, `contacts.ResolveUsernameRequest`, `account.CheckUsernameRequest` и отключения. Имя, найденный peer, содержимое сессии и секреты в отчёт не включаются. Лимиты ожидания этапов: подключение 20 с, авторизация 15 с, `help.GetConfigRequest` 15 с, разрешение имени 10 с, проверка имени — `CHECK_TIMEOUT_SECONDS` (по умолчанию 30 с), отключение 5 с. Resolve выполняется перед Check, чтобы сравнить независимые ответы. Успешный Resolve означает, что имя удалось разрешить; `UsernameNotOccupiedError` означает, что разрешить его не удалось, но сам по себе не гарантирует доступность для назначения. `UsernameInvalidError`, `FloodWaitError`, `TimeoutError` и прочие ошибки показываются отдельно, без исходного текста исключения. Если авторизация и `help.GetConfigRequest` отвечают, а один из запросов имени зависает, проблема проявляется на уровне этого RPC после подключения. При `FloodWaitError` команда показывает требуемую паузу и не отправляет следующие запросы.

### Надёжность проверок

`CHECK_TIMEOUT_SECONDS=30` ограничивает ожидание **каждого** запроса `account.checkUsername`, включая вторую проверку результата `free`. Допустимый диапазон — от 1 до 86400 секунд. Если запрос не завершился вовремя, он отменяется, ошибка записывается как временная, прежний статус имени сохраняется, текущий цикл прекращается до следующей паузы. Уведомление о свободном имени отправляется только после двух успешных ответов `free`.

Для `login`, `check` и `run` настроены ограниченные повторы Telethon: `request_retries=1` (один дополнительный повтор при поддерживаемых RPC-ошибках), `connection_retries=2`, `retry_delay=1` секунда между повторными подключениями. При проверках автоматическое ожидание `FloodWait` отключено: монитор получает ошибку и выдерживает паузу не менее указанного Telegram времени плюс 60 секунд. Эти параметры не меняют интервалы между именами и циклами.

### Настройки нагрузки

Значения по умолчанию: `CHECK_DELAY_SECONDS=30` (между кандидатами), `CONFIRM_DELAY_SECONDS=10` (вторая проверка доступного имени), `MAX_PER_CYCLE=20`, `CYCLE_PAUSE_SECONDS=600` (между циклами). При большом списке кандидаты обходятся по очереди, старые проверки получают приоритет. Это **не** гарантированно безопасные лимиты — Telegram не публикует универсальную безопасную частоту. При `FLOOD_WAIT` монитор приостанавливается как минимум на указанное Telegram время + 60 секунд. Не увеличивай частоту агрессивно.

### Публикация из архива на GitHub

1. Распакуй архив и открой PowerShell в папке проекта.
2. Установи [Git](https://git-scm.com/download/win) и [GitHub CLI](https://cli.github.com/).
3. Выполни `gh auth login`, затем `./publish-to-github.ps1`.
4. Скрипт создаст **приватный** `lxstjxck/TelegramUsernameMonitor` и отправит туда исходники. Проверяй, что `gh` вошёл под `lxstjxck`, а имя свободно. Если Git не знает автора, задай `git config --global user.name ...` и `git config --global user.email ...`.

Для приватного репозитория на VPS потребуется настроить авторизованный GitHub-доступ (SSH-ключ / GitHub CLI) перед `git clone`; альтернативно можно сделать репозиторий публичным, но **без .env или .session**.

## Ограничения и ответственность

- `account.checkUsername` проверяет доступность для пользовательского аккаунта. Для конкретного канала Telegram предоставляет отдельный `channels.checkUsername`. Даже `free` не резервирует имя: оно может быть занято другим человеком между проверкой и назначением.
- Telegram может ограничивать запросы, не отдавать временно освобождённые имена в свободную регистрацию, а также применять правила против сквоттинга. Не используйте множество аккаунтов для обхода лимитов.
- Не собираем личные данные владельцев чужих имён, не отправляем запросы на регистрацию и не торгуем именами автоматически. Покупка/продажа — отдельный ручной процесс.
- Официальная документация: https://core.telegram.org/method/account.checkUsername и https://core.telegram.org/method/channels.checkUsername

## Тесты

```bash
python3 -m unittest discover -s tests -v
```
