# Запуск на Render

1. В Render нажмите **New → Blueprint** и подключите репозиторий `podpiskagemini8-ops/keska`.
2. Render прочитает `render.yaml`: один **Background Worker** и постоянный диск на 1 ГБ. Это платный сервис; стоимость показывается перед созданием.
3. Введите токен вашего бота в **BOT_TOKEN**. Не добавляйте его в GitHub.
4. Перед запуском сервиса остановите бота на компьютере и отключите его автозапуск:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\stop-bot.ps1
   powershell -NoProfile -ExecutionPolicy Bypass -File .\remove-autostart.ps1
   ```

5. Создайте сервис. В **Logs** должно появиться `Bot @... started`. Откройте бота и отправьте `/start`.

Бот получает сообщения через polling: ему не нужен сайт, порт или webhook. Для одного токена должен работать только один экземпляр. Ошибка Telegram 409 означает, что этот токен уже используется другим процессом. Автоматическое обновление сервиса из GitHub использует тот же постоянный диск.

## Сохранение данных

`BOT_DB=/var/data/bot.sqlite3` размещает базу на постоянном диске. Здесь сохраняются пользователи, черновики, настройки старых розыгрышей, участники и очереди уведомлений. Без диска эти данные теряются при перезапуске или обновлении Render.

Исходный код не содержит вашу локальную базу: новый сервис создаст пустую. Для переноса старых розыгрышей нужно отдельно перенести базу после остановки локального бота. Создайте согласованную копию командой:

```powershell
python tools/backup_db.py --output data/render-transfer.sqlite3
```

Храните эту копию приватно. Перед первым запуском бота на Render разместите её на диске как `/var/data/bot.sqlite3` (например, через доступ Render по SSH/SCP). Не заменяйте базу, пока бот работает. После переноса не запускайте прежнюю локальную копию параллельно с Render.

Документация: [Background Workers](https://render.com/docs/background-workers), [Persistent Disks](https://render.com/docs/disks), [Blueprint](https://render.com/docs/infrastructure-as-code).
