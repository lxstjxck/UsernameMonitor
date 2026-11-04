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

### Уведомления

1. Создай бота через @BotFather, напиши ему `/start`.
2. Получи `CHAT_ID`, например через `https://api.telegram.org/bot<TOKEN>/getUpdates` (токен не публикуй, ссылку с токеном никому не отправляй).
3. Заполни `BOT_TOKEN=` и `CHAT_ID=` в `.env`. Без них уведомления пишутся в журнал, монитор всё равно работает.

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
```

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
