import http.client
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from rafflebot.postgres import Row, translate
from rafflebot.storage import Store
from rafflebot.web import Application, handler, settings


class WebTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.app = Application(Mock(), self.store, 's' * 32, 'https://example.com/telegram')
        self.app.bot = Mock()
        self.app.ready = True
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.db.close()

    def request(self, path, body=None, secret=None, method='POST'):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        headers = {'Content-Type': 'application/json'}
        if secret:
            headers['X-Telegram-Bot-Api-Secret-Token'] = secret
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        code = response.status
        response.read()
        conn.close()
        return code

    def test_authenticated_webhook_is_durable_and_duplicates_are_ignored(self):
        update = {'update_id': 123, 'message': {'text': '/start'}}
        self.assertEqual(self.request('/telegram', json.dumps(update)), 403)
        self.assertEqual(len(self.store.pending_updates()), 0)
        for _ in range(2):
            self.assertEqual(self.request('/telegram', json.dumps(update), 's' * 32), 200)
        self.assertEqual(len(self.store.pending_updates()), 1)
        self.app.process_updates()
        self.app.bot.handle.assert_called_once_with(update)
        self.assertEqual(self.request('/telegram', json.dumps(update), 's' * 32), 200)
        self.app.process_updates()
        self.app.bot.handle.assert_called_once()

    def test_persistence_failure_makes_telegram_retry(self):
        with patch.object(self.store, 'receive_update', side_effect=RuntimeError):
            self.assertEqual(self.request('/telegram', '{"update_id":1}', 's' * 32), 503)

    def test_bad_payload_and_unauthorized_tasks_are_rejected(self):
        self.assertEqual(self.request('/telegram', 'not json', 's' * 32), 400)
        self.assertEqual(self.request('/telegram', '{"update_id":true}', 's' * 32), 400)
        self.assertEqual(self.request('/tasks', '{}'), 403)
        self.assertFalse(self.app.wake.is_set())
        self.assertEqual(self.request('/health', method='GET'), 200)
        self.app.ready = False
        self.assertEqual(self.request('/health', method='GET'), 503)

    def test_inbox_survives_reopening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/bot.sqlite3'
            store = Store(path)
            store.receive_update({'update_id': 55})
            store.db.close()
            store = Store(path)
            self.assertEqual(store.pending_updates()[0]['id'], 55)
            store.db.close()

    def test_web_mode_refuses_ephemeral_sqlite(self):
        with patch.dict('os.environ', {}, clear=True), patch('rafflebot.web.load', return_value=('123:abc', ':memory:')):
            with self.assertRaisesRegex(RuntimeError, 'PostgreSQL'):
                settings()

    def test_postgres_query_conversion_and_indexed_rows(self):
        self.assertEqual(translate('INSERT OR IGNORE INTO sessions VALUES(?,?)'),
                         'INSERT INTO sessions VALUES(%s,%s) ON CONFLICT DO NOTHING')
        self.assertIn('greatest(next_attempt,%s)', translate('UPDATE outbox SET next_attempt=max(next_attempt,?)'))
        row = Row(id=8259479085, title='test')
        self.assertEqual(row[0], row['id'])
        self.assertEqual(row[1], 'test')


if __name__ == '__main__':
    unittest.main()
