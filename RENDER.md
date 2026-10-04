# Бесплатный Web Service на Render

Бот поддерживает два режима: `python run.py` для компьютера (SQLite, polling) и `python -m rafflebot.web` для Render (PostgreSQL, webhook). Данные Render хранятся во внешней базе.

## Настройка

1. Создайте отдельный бесплатный PostgreSQL-проект в [Neon](https://console.neon.tech). Скопируйте **прямую** строку подключения с `sslmode=require` (Connection pooling выключен). Прямое соединение нужно для блокировки одновременных запусков.
2. В Render: **New → Web Service → podpiskagemini8-ops/keska**.
3. Выберите **Python 3**, ветку `main`, тариф **Free**.
4. **Build Command:** `pip install -r requirements.txt`.
5. **Start Command:** `python -u -m rafflebot.web`.
6. Переменные окружения:

| Имя | Значение |
|---|---|
| `BOT_TOKEN` | Токен бота из BotFather |
| `DATABASE_URL` | Прямая строка подключения к отдельной базе Neon |
| `WEBHOOK_SECRET` | Случайная строка из 32–256 латинских букв, цифр, `_` или `-` |

HTTPS-адрес Render подставляется автоматически через `RENDER_EXTERNAL_URL`. Для другого хостинга задайте `WEBHOOK_URL=https://адрес-сервиса`. **Health Check Path:** `/health`.

7. Перед запуском остановите локального бота и отключите автозапуск:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\stop-bot.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\remove-autostart.ps1
```

8. Нажмите **Deploy Web Service**. В Logs появится `Webhook bot @... ready`. Бот сам подключит webhook, сохранив ожидающие сообщения Telegram. Проверьте `/start`.

Вместо ручного создания можно использовать **New → Blueprint**: `render.yaml` задаёт бесплатный Web Service, команды и секрет. База подключается отдельно; платный Worker или диск не создаются.

## Перенос через приватный файл окружения

Для первого запуска можно остановить локального бота, выполнить `python tools/export_render.py` и добавить созданный `.env.render-import` через **Add from .env → Choose a file** в Render. Приватная копия импортируется в пустую базу атомарно, повторный запуск не дублирует данные. После успешного переноса удалите `IMPORT_DATA_BASE64` и `CHECK_POSTGRES` из настроек Render. Файл содержит пользователей и розыгрыши и исключён из Git.

`CHECK_POSTGRES=1` однократно запускает проверки совместимости в отдельных временных схемах. Они используют подставной Telegram API и не отправляют сообщения настоящим пользователям. Рабочие данные не меняются.

## Перенос прежних пользователей и розыгрышей

Внешняя база должна быть отдельной и пустой. Остановите локального бота, затем:

```powershell
python -m pip install -r requirements.txt
python tools/backup_db.py --output data/final-transfer.sqlite3
```

Приватно добавьте `DATABASE_URL` в локальный `.env`, выполните **до запуска Render**:

```powershell
python tools/migrate_postgres.py --source data/final-transfer.sqlite3
```

Скрипт переносит пользователей, настройки, участников, итоги и очереди в одной транзакции. В непустую базу не пишет. SQLite-копию и `.env` нельзя загружать в GitHub. После переноса удалите `DATABASE_URL` из локального `.env`, если компьютер должен остаться в режиме SQLite. Не запускайте локального бота параллельно с Render.

## Итоги по времени и сон сервиса

Telegram-сообщение будит сервис через webhook. Во время сна задачи не выполняются: без внешнего расписания просроченные итоги обрабатываются после пробуждения. Итоги по числу участников и ручные итоги выполняются при соответствующем событии.

Для автоматических итогов без сообщений настройте внешний HTTP-планировщик:

- Метод **POST**.
- Адрес `https://ВАШ-СЕРВИС.onrender.com/tasks`.
- Заголовок `Authorization: Bearer ЗНАЧЕНИЕ_WEBHOOK_SECRET`.
- Запуск в нужное время или с выбранным интервалом; холодный запуск требует повторных попыток и таймаута не меньше 90 секунд.

Запрос будит сервис и ставит проверку итогов в очередь. Ответ `202` означает принятие, а не завершение рассылки. `/health` только проверяет сервер и не заменяет `/tasks`. Не публикуйте секрет и не передавайте его в URL.

Частые запросы удерживают сервис активным и расходуют часы Render, общие для workspace. Проверяйте лимиты Render и Neon. Веб-режим запрещает локальную SQLite, чтобы база не терялась при обновлении.

Документация: [Render Free](https://render.com/docs/free), [Telegram Webhooks](https://core.telegram.org/bots/api#setwebhook), [Neon](https://neon.com/docs).
