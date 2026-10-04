import argparse
import json
import logging
import os
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import time

from rafflebot.api import Telegram, TelegramError
from rafflebot.bot import Bot
from rafflebot.config import ROOT, load
from rafflebot.storage import Store


def lock_process(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Бот уже запущен из этой папки") from None
    return handle


def setup(api):
    api.call("setMyCommands", commands=[{"command": "start", "description": "Главное меню"},
        {"command": "help", "description": "Как провести розыгрыш"}, {"command": "cancel", "description": "Отменить ввод"}])
    api.call("setMyDescription", description="Бот для розыгрышей в Telegram-каналах: призы, подписки, приглашения друзей и автоматические итоги. Нажмите /start, чтобы создать розыгрыш или открыть меню.")
    api.call("setMyShortDescription", short_description="Розыгрыши в каналах · Проверка подписок · Автоматические итоги")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Проверить подключение, не запускать бота")
    parser.add_argument("--setup", action="store_true", help="Обновить описание и команды бота")
    args = parser.parse_args()
    token, dbpath = load()
    api = Telegram(token)
    if args.check:
        me = api.call("getMe")
        info = api.call("getWebhookInfo")
        print(json.dumps({"username": me["username"], "webhook_configured": bool(info.get("url")), "ok": True}, ensure_ascii=False))
        return
    if args.setup:
        setup(api)
        print("Описание и команды обновлены.")
        return
    process_lock = lock_process(Path(dbpath).parent / "bot.lock")
    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), RotatingFileHandler(logdir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")])
    logger = logging.getLogger("runner")
    store = Store(dbpath)
    me = None
    while me is None:
        try:
            me = api.call("getMe")
            if api.call("getWebhookInfo").get("url"):
                logger.error("A webhook is configured; polling stopped")
                store.set_meta("last_error", "Webhook настроен; запуск остановлен")
                return
        except TelegramError as e:
            logger.warning("Startup waiting for Telegram: code=%s", e.code)
            if e.code == 401:
                store.set_meta("last_error", "Ключ бота недействителен")
                return
            time.sleep(max(10, e.retry_after))
    bot = Bot(api, store, me)
    logger.info("Bot @%s started", me["username"])
    store.set_meta("last_error", "")
    offset = int(store.meta("offset", "0"))
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            try:
                bot.tick()
                updates = api.call("getUpdates", offset=offset, timeout=10,
                                   allowed_updates=["message", "callback_query"])
                store.set_meta("heartbeat", int(time.time()))
                for update in updates:
                    try:
                        bot.handle(update)
                    except (ValueError, TelegramError) as e:
                        user = update.get("callback_query", {}).get("from") or update.get("message", {}).get("from")
                        code = e.code if isinstance(e, TelegramError) else None
                        if code is not None:
                            logger.warning("Update failed: code=%s", code)
                        if user:
                            message = str(e) if isinstance(e, ValueError) else {
                                0: "Связь с Telegram прервалась. Попробуйте снова. Если вы публиковали пост, откройте «Мои розыгрыши» для проверки публикации.",
                                429: "Telegram просит немного подождать. Попробуйте снова через минуту.",
                                403: "Telegram запретил действие. Проверьте доступ бота к каналу.",
                                400: "Telegram не принял действие. Проверьте права бота. Если задан премиум-эмодзи, попробуйте убрать его в настройках кнопки или через «Призы → Эмодзи для мест»."}.get(code, "Telegram временно недоступен. Попробуйте снова.")
                            try:
                                from rafflebot.bot import esc
                                bot.say(user["id"], "⚠️ " + esc(message))
                            except TelegramError:
                                pass
                    except Exception:
                        logger.exception("Unexpected update error (update_id=%s)", update["update_id"])
                        user = update.get("callback_query", {}).get("from") or update.get("message", {}).get("from")
                        if user:
                            try:
                                bot.say(user["id"], "⚠️ Не удалось обработать действие. Откройте /start и попробуйте снова.")
                            except TelegramError:
                                pass
                    finally:
                        offset = update["update_id"] + 1
                        store.set_meta("offset", offset)
                store.set_meta("last_error", "")
            except TelegramError as e:
                logger.warning("Telegram connection: code=%s", e.code)
                store.set_meta("last_error", f"Telegram error {e.code}")
                if e.code in (401, 409):
                    logger.error("Polling stopped: invalid token or another polling process")
                    break
                time.sleep(max(5, e.retry_after))
            except Exception:
                logger.exception("Runner failed; retrying")
                time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        store.db.close()
        process_lock.close()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, TelegramError) as e:
        print(str(e) if isinstance(e, RuntimeError) else f"Ошибка Telegram: {e.code}", file=sys.stderr)
        sys.exit(1)
