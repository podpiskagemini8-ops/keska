"""Webhook HTTP service with a durable inbox and one serialized bot worker."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import re
import signal
import threading
import time

from .api import Telegram, TelegramError
from .bot import Bot, esc
from .config import load
from .storage import Store

LOG = logging.getLogger('web')


def settings():
    token, database = load()
    if not database.startswith(('postgresql://', 'postgres://')):
        raise RuntimeError('Web Service требует DATABASE_URL внешней PostgreSQL-базы')
    url = os.environ.get('WEBHOOK_URL') or os.environ.get('RENDER_EXTERNAL_URL', '')
    if not url.startswith('https://'):
        raise RuntimeError('Задайте HTTPS-адрес сервиса в WEBHOOK_URL')
    secret = os.environ.get('WEBHOOK_SECRET', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', secret):
        raise RuntimeError('WEBHOOK_SECRET: 32–256 латинских букв, цифр, дефисов или подчёркиваний')
    return token, database, url.rstrip('/') + '/telegram', secret


class Application:
    def __init__(self, api, store, secret, webhook_url):
        self.api, self.store = api, store
        self.secret, self.webhook_url = secret, webhook_url
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.ready = False
        self.bot = None
        self.leader = False
        # Same key on overlapping deploys prevents two workers drawing/sending.
        self.lock_key = int.from_bytes(hashlib.sha256(webhook_url.encode()).digest()[:8], 'big', signed=True)

    def receive(self, update):
        try:
            with self.lock:
                self.store.receive_update(update)
        finally:
            # A stale connection after database sleep also wakes reconnect work.
            self.wake.set()

    def start_bot(self):
        if self.store.postgres and not self.leader:
            self.leader = bool(self.store.db.execute('SELECT pg_try_advisory_lock(?)', (self.lock_key,)).fetchone()[0])
            if not self.leader:
                # The new instance can persist incoming updates while the old
                # deploy drains. Healthy standby lets Render stop the old one.
                self.ready = True
                return False
        me = self.api.call('getMe')
        self.api.call('setWebhook', url=self.webhook_url, secret_token=self.secret,
                      max_connections=1, allowed_updates=['message', 'callback_query'],
                      drop_pending_updates=False)
        self.bot = Bot(self.api, self.store, me)
        self.ready = True
        LOG.info('Webhook bot @%s ready', me['username'])
        return True

    def process_updates(self):
        for row in self.store.pending_updates():
            update = json.loads(row['data'])
            # Do not repeat ambiguous publication or messages after a crash.
            with self.store.db:
                self.store.db.execute("UPDATE webhook_updates SET state='processing' WHERE id=?", (row['id'],))
            state = 'done'
            try:
                self.bot.handle(update)
            except (ValueError, TelegramError) as error:
                user = update.get('callback_query', {}).get('from') or update.get('message', {}).get('from')
                message = str(error) if isinstance(error, ValueError) else 'Не удалось выполнить действие в Telegram. Попробуйте снова; после публикации проверьте «Мои розыгрыши».'
                if user:
                    try:
                        self.bot.say(user['id'], '⚠️ ' + esc(message))
                    except TelegramError:
                        pass
            except Exception as error:
                state = 'failed'
                LOG.error('Update %s failed (%s)', row['id'], type(error).__name__)
            with self.store.db:
                self.store.db.execute("UPDATE webhook_updates SET state=?,data='{}' WHERE id=?", (state, row['id']))

    def next_wait(self):
        """Sleep until work is due; idle health checks don't keep Neon awake."""
        now = time.time()
        due = []
        if self.store.pending_updates():
            return 0.1
        for raffle in self.store.list(status='active'):
            if raffle['mode'] == 'time' and raffle.get('deadline'):
                due.append(raffle['deadline'])
                if raffle['reminder'] and not raffle.get('reminder_sent'):
                    due.append(raffle['deadline'] - raffle['reminder_minutes'] * 60)
        for query in (
            "SELECT min(next_attempt) FROM outbox WHERE state='pending'",
            "SELECT min(d.next_attempt) FROM campaign_deliveries d JOIN campaigns c ON c.id=d.campaign WHERE c.status='queued' AND d.state='pending'",
        ):
            value = self.store.db.execute(query).fetchone()[0]
            if value is not None:
                due.append(max(value, int(self.store.meta('outbound_pause', '0'))))
        return max(1, min(due) - now) if due else 3600

    def work(self):
        while not self.stopped.is_set():
            self.wake.clear()
            delay = 10
            try:
                with self.lock:
                    if self.bot is None and not self.start_bot():
                        delay = 10
                    else:
                        self.process_updates()
                        self.bot.last_tick = 0
                        self.bot.tick()
                        delay = self.next_wait()
            except Exception as error:
                # Never log connection strings, tokens or update payloads.
                LOG.error('Worker deferred (%s)', type(error).__name__)
                self.ready = False
                self.bot = None
                if self.store.postgres and self.store.db.connection.closed:
                    self.leader = False
                    try:
                        with self.lock:
                            self.store = Store(os.environ['DATABASE_URL'])
                    except Exception:
                        LOG.error('Database reconnect deferred')
            self.wake.wait(delay)


def handler(application):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, message):
            body = json.dumps({'status': message}).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ('/', '/health'):
                return self.respond(200 if application.ready else 503, 'ready' if application.ready else 'starting')
            self.respond(404, 'not found')

        def do_POST(self):
            if self.path == '/tasks':
                if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + application.secret):
                    return self.respond(403, 'forbidden')
                application.wake.set()
                return self.respond(202, 'scheduled')
            if self.path != '/telegram':
                return self.respond(404, 'not found')
            if not hmac.compare_digest(self.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), application.secret):
                return self.respond(403, 'forbidden')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1_000_000:
                    return self.respond(413, 'invalid size')
                self.connection.settimeout(15)
                update = json.loads(self.rfile.read(size))
                if not isinstance(update, dict) or type(update.get('update_id')) is not int:
                    return self.respond(400, 'invalid update')
            except (ValueError, TimeoutError):
                return self.respond(400, 'invalid request')
            try:
                application.receive(update)
            except Exception:
                LOG.error('Webhook persistence failed')
                return self.respond(503, 'retry later')
            # Telegram may retry safely: update_id is a unique durable key.
            self.respond(200, 'accepted')
    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token, database, webhook_url, secret = settings()
    store = Store(database)
    if os.environ.get('IMPORT_DATA_BASE64'):
        from .migration import import_snapshot
        import_snapshot(store, os.environ['IMPORT_DATA_BASE64'])
    if os.environ.get('CHECK_POSTGRES') == '1' and not store.meta('postgres_checked_v1'):
        from tools.check_postgres import check
        check(database)
        store.set_meta('postgres_checked_v1', '1')
    application = Application(Telegram(token), store, secret, webhook_url)
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '10000'))), handler(application))
    server.daemon_threads = True
    worker = threading.Thread(target=application.work, daemon=True)
    worker.start()
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info('Web service stopping')
    finally:
        application.stopped.set()
        application.wake.set()
        server.server_close()
        worker.join(timeout=20)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Sanitized startup failures: PostgreSQL errors can contain credentials.
        LOG.error('Web startup failed (%s)', type(error).__name__)
        raise SystemExit(1)
