import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from rafflebot.admin import ADMIN_ID
from rafflebot.api import TelegramError
from rafflebot.bot import Bot
from rafflebot.storage import Store
from test_bot import CHANNEL, FakeAPI, ME


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.store = Store(':memory:')
        self.bot = Bot(self.api, self.store, ME)
        self.admin = {'id': ADMIN_ID, 'first_name': 'Администратор', 'username': 'admin_name'}
        self.user = {'id': 2, 'first_name': 'Участник'}
        self.other = {'id': 3, 'first_name': 'Другой'}
        for person in (self.admin, self.user, self.other):
            self.store.user(person)

    def tearDown(self):
        self.store.db.close()

    def message(self, user, text):
        self.bot.handle({'update_id': 1, 'message': {'from': user, 'chat': {'id': user['id'], 'type': 'private'}, 'text': text}})

    def prepare_campaign(self):
        self.bot.admin.handle(ADMIN_ID, 'admin:new')
        self.message(self.admin, 'Рекламный пост')
        return self.store.db.execute('SELECT max(id) FROM campaigns').fetchone()[0]

    def test_admin_command_is_silent_for_others(self):
        self.message(self.user, '/admin')
        self.message(self.user, '/admin@testgiveaway_bot')
        self.assertEqual(self.api.calls, [])
        self.message(self.admin, '/admin')
        self.assertIn('Админ-панель', self.api.calls[-1][1]['text'])
        self.assertIn('Пользователей в базе бота: 3', self.api.calls[-1][1]['text'])

    def test_forged_admin_callbacks_do_not_answer_or_export(self):
        for action in ('home', 'exportusers', 'new', 'send:1', 'exportparticipants:1'):
            self.bot.handle({'callback_query': {'id': 'fake', 'from': self.user, 'data': 'admin:' + action, 'message': {'message_id': 1, 'chat': {'id': 2, 'type': 'private'}}}})
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM campaigns').fetchone()[0], 0)

    def test_only_admin_has_unlimited_winner_count(self):
        self.assertEqual(self.bot.winner_count('1000000', ADMIN_ID), 1000000)
        with self.assertRaises(ValueError):
            self.bot.winner_count('101', 2)
        for value in ('0', '-1', '3.14', 'all'):
            with self.assertRaises(ValueError):
                self.bot.winner_count(value, ADMIN_ID)
        self.api.members[ADMIN_ID] = {'status': 'creator'}
        self.bot.create(ADMIN_ID)
        self.bot.input(self.admin, {'text': '@testchannel'}, self.store.session(ADMIN_ID))
        r = self.store.list(owner=ADMIN_ID)[0]
        self.bot.input(self.admin, {'text': '1000'}, {'rid': r['id'], 'field': 'winners'})
        self.assertEqual(self.store.get(r['id'])['winners'], 1000)

    def test_stats_distinguish_unique_users_and_entries(self):
        a = self.store.create(ADMIN_ID, {'title': 'Первый'})
        b = self.store.create(ADMIN_ID, {'title': 'Второй'})
        self.store.join(a['id'], self.user)
        self.store.join(b['id'], self.user)
        self.store.join(b['id'], self.other)
        self.message(self.admin, '/admin')
        text = self.api.calls[-1][1]['text']
        self.assertIn('Уникальных участников за всё время: 2', text)
        self.assertIn('Всего заявок в розыгрыши: 3', text)
        self.assertEqual(self.store.count(a['id']), 1)

    def test_export_is_only_for_admin_and_escapes_formula_cells(self):
        self.store.user({'id': 4, 'first_name': '=malicious()'})
        self.bot.admin.export(2)
        self.assertEqual(self.api.calls, [])
        self.bot.admin.export(ADMIN_ID)
        method, params = self.api.calls[-1]
        self.assertEqual(method, 'document')
        self.assertEqual(params['chat_id'], ADMIN_ID)
        self.assertIn("'=malicious()", params['content'].decode('utf-8-sig'))

    def test_advertising_requires_opt_in_and_confirm(self):
        self.assertFalse(self.store.advertising(2))
        self.bot.callback(self.user, 'marketing:on', 1)
        cid = self.prepare_campaign()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM campaign_deliveries').fetchone()[0], 0)
        self.bot.admin.handle(2, f'admin:send:{cid}')
        self.assertEqual(self.bot.admin.campaign(cid)['status'], 'draft')
        self.bot.admin.handle(ADMIN_ID, f'admin:send:{cid}')
        self.bot.admin.handle(ADMIN_ID, f'admin:send:{cid}')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM campaign_deliveries').fetchone()[0], 1)
        self.api.calls.clear()
        with patch('rafflebot.admin.time.sleep'):
            self.bot.admin.drain()
        sends = [(m, p) for m, p in self.api.calls if m == 'sendMessage']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1]['chat_id'], 2)
        self.assertEqual(sends[0][1]['text'], 'Рекламный пост')
        self.assertEqual(sends[0][1]['reply_markup']['inline_keyboard'][0][0]['callback_data'], 'marketing:off')
        self.assertEqual(self.bot.admin.campaign(cid)['status'], 'finished')

    def test_unsubscribe_before_delivery_skips_user(self):
        self.store.advertising(2, True)
        cid = self.prepare_campaign()
        self.bot.admin.handle(ADMIN_ID, f'admin:send:{cid}')
        self.message(self.user, '/unsubscribe')
        self.api.calls.clear()
        with patch('rafflebot.admin.time.sleep'):
            self.bot.admin.drain()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.store.db.execute('SELECT state FROM campaign_deliveries').fetchone()[0], 'skipped')

    def test_broadcast_cancel_and_failure_states(self):
        self.store.advertising(2, True)
        cid = self.prepare_campaign()
        self.bot.admin.handle(ADMIN_ID, f'admin:send:{cid}')
        self.bot.admin.handle(ADMIN_ID, f'admin:cancel:{cid}')
        self.api.calls.clear()
        self.bot.admin.drain()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.bot.admin.campaign(cid)['status'], 'canceled')
        cid = self.prepare_campaign()
        self.bot.admin.handle(ADMIN_ID, f'admin:send:{cid}')
        self.api.fail['sendMessage'] = TelegramError(0, 'Response lost')
        with patch('rafflebot.admin.time.sleep'):
            self.bot.admin.drain()
        self.assertEqual(self.store.db.execute('SELECT state FROM campaign_deliveries WHERE campaign=?', (cid,)).fetchone()[0], 'unknown')
        self.api.calls.clear()
        self.bot.admin.drain()
        self.assertEqual(self.api.calls, [])

    def test_rate_limit_defers_all_outbound_queues(self):
        self.store.advertising(2, True)
        cid = self.prepare_campaign()
        self.bot.admin.handle(ADMIN_ID, f'admin:send:{cid}')
        self.api.fail['sendMessage'] = TelegramError(429, 'Rate limit', 60)
        self.bot.admin.drain()
        self.assertGreater(int(self.store.meta('outbound_pause')), 0)
        self.assertEqual(self.store.db.execute('SELECT state FROM campaign_deliveries').fetchone()[0], 'pending')

    def test_admin_raffle_starting_participants(self):
        # Admin raffle starts at 274
        admin_raffle = self.store.create(ADMIN_ID, {
            'channel': {'id': -1001, 'title': 'Admin Channel'},
            'title': 'Розыгрыш админа',
            'button': {'text': '🎁 Участвовать'},
            'extra': [],
            'show_count': True,
            'status': 'active',
            'winners': 1,
            'mode': 'time',
            'deadline': None,
            'prizes': {},
            'conditions': [],
            'referrals': False,
            'require_username': False,
        })
        self.assertEqual(self.bot.participant_count(admin_raffle), 274)
        markup = self.bot.public_markup(admin_raffle)
        self.assertEqual(markup['inline_keyboard'][0][0]['text'], '🎁 Участвовать (274)')

        # First join -> 275
        self.store.join(admin_raffle['id'], self.user)
        self.assertEqual(self.bot.participant_count(admin_raffle), 275)
        markup = self.bot.public_markup(admin_raffle)
        self.assertEqual(markup['inline_keyboard'][0][0]['text'], '🎁 Участвовать (275)')

        # Second join -> 276
        self.store.join(admin_raffle['id'], self.other)
        self.assertEqual(self.bot.participant_count(admin_raffle), 276)
        markup = self.bot.public_markup(admin_raffle)
        self.assertEqual(markup['inline_keyboard'][0][0]['text'], '🎁 Участвовать (276)')

        # Non-admin raffle starts at 0
        regular_raffle = self.store.create(12345, {
            'channel': {'id': -1002, 'title': 'User Channel'},
            'title': 'Розыгрыш пользователя',
            'button': {'text': '🎁 Участвовать'},
            'extra': [],
            'show_count': True,
            'status': 'active',
            'winners': 1,
            'mode': 'time',
            'deadline': None,
            'prizes': {},
            'conditions': [],
            'referrals': False,
            'require_username': False,
        })
        self.assertEqual(self.bot.participant_count(regular_raffle), 0)
        regular_markup = self.bot.public_markup(regular_raffle)
        self.assertEqual(regular_markup['inline_keyboard'][0][0]['text'], '🎁 Участвовать (0)')
        self.store.join(regular_raffle['id'], self.user)
        self.assertEqual(self.bot.participant_count(regular_raffle), 1)
        regular_markup = self.bot.public_markup(regular_raffle)
        self.assertEqual(regular_markup['inline_keyboard'][0][0]['text'], '🎁 Участвовать (1)')

    def test_migration_keeps_existing_user_database(self):
        import sqlite3, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'old.sqlite3')
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,data TEXT NOT NULL)')
            db.execute('INSERT INTO users VALUES(?,?)', (2, json.dumps(self.user)))
            db.commit()
            db.close()
            s = Store(path)
            s.user(self.user)
            self.assertEqual(s.db.execute('SELECT count(*) FROM users').fetchone()[0], 1)
            self.assertGreater(s.db.execute('SELECT last_seen FROM users').fetchone()[0], 0)
            self.assertFalse(s.advertising(2))
            s.db.close()


if __name__ == '__main__':
    unittest.main()
