import copy
import hashlib
import json
from pathlib import Path
from datetime import datetime
import tempfile
import time
import unittest
from unittest.mock import patch

from rafflebot.api import TelegramError
from rafflebot.bot import Bot, MSK, chat_reference, parse_deadline
from rafflebot.draw import commitment, weighted_draw
from rafflebot.storage import Store
from tools.verify import verify

OWNER = {"id": 1, "first_name": "Организатор", "username": "owner_name", "is_bot": False}
CHANNEL = {"id": -1001234567890, "type": "channel", "title": "Тест", "username": "testchannel"}
ME = {"id": 999, "username": "testgiveaway_bot"}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.members = {}
        self.fail = {}
        self.counter = 100

    def call(self, method, **params):
        self.calls.append((method, copy.deepcopy(params)))
        if method in self.fail:
            raise self.fail[method]
        if method == "getChat":
            if params["chat_id"] == CHANNEL["id"] or isinstance(params["chat_id"], str):
                return dict(CHANNEL)
            return {"id": params["chat_id"], "type": "private", "username": "user_name"}
        if method == "getChatMember":
            uid = params["user_id"]
            if uid == ME["id"]:
                return {"status": "administrator", "can_post_messages": True, "can_edit_messages": True}
            if uid == OWNER["id"]:
                return {"status": "creator"}
            return self.members.get(uid, {"status": "member"})
        if method == 'getCustomEmojiStickers':
            return [{'custom_emoji_id': eid, 'emoji': '💎', 'type': 'custom_emoji'} for eid in params['custom_emoji_ids'] if eid != '0']
        if method.startswith("send"):
            self.counter += 1
            return {"message_id": self.counter}
        return True

    def document(self, chat_id, filename, content, caption=""):
        self.calls.append(("document", {"chat_id": chat_id, "filename": filename, "content": content}))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.api = FakeAPI()
        self.bot = Bot(self.api, self.store, ME)
        self.bot.create(OWNER["id"])
        self.bot.input(OWNER, {"text": "@testchannel"}, self.store.session(1))
        self.r = self.store.list(owner=1)[0]

    def tearDown(self):
        self.store.db.close()

    def configure(self, field, text=None, **message):
        self.bot.input(OWNER, {"text": text or "", **message}, {"rid": self.r["id"], "field": field})
        self.r = self.store.get(self.r["id"])

    def active(self, **changes):
        self.configure("post", "Подарки! <текст>")
        self.r.update(mode="manual", **changes)
        self.store.save(self.r)
        self.bot.publish(1, self.r)
        self.r = self.store.get(self.r["id"])
        return self.r

    @staticmethod
    def user(uid, username=True):
        return {"id": uid, "first_name": f"Человек {uid}", "is_bot": False, **({"username": f"user_{uid}"} if username else {})}

    def test_repost_preserves_raffle_and_participants(self):
        self.active()
        self.bot.join(self.user(101), self.r['id'])
        before = self.store.get(self.r['id'])
        self.bot.callback(OWNER, f"g:{self.r['id']}:repost", 1)
        self.assertEqual(self.store.get(self.r['id']), before)
        self.bot.callback(OWNER, f"g:{self.r['id']}:confirmrepost", 1)
        after = self.store.get(self.r['id'])
        self.assertNotEqual(after['message_id'], before['message_id'])
        self.assertEqual({k: v for k, v in after.items() if k != 'message_id'}, {k: v for k, v in before.items() if k != 'message_id'})
        self.assertEqual(self.store.count(after['id']), 1)
        with self.assertRaises(ValueError):
            self.bot.callback(self.user(102), f"g:{after['id']}:confirmrepost", 1)

    def test_repost_failure_keeps_original_post(self):
        self.active()
        before = self.store.get(self.r['id'])
        self.api.fail['sendMessage'] = TelegramError('failed', 400)
        with self.assertRaises(TelegramError):
            self.bot.callback(OWNER, f"g:{self.r['id']}:confirmrepost", 1)
        self.assertEqual(self.store.get(self.r['id']), before)

    def test_creation_and_every_settings_screen(self):
        self.assertEqual(self.r["status"], "draft")
        for action in ("winners", "prizes", "timing", "conditions", "bonuses", "guard", "extra", "promo"):
            self.bot.callback(OWNER, f"g:{self.r['id']}:{action}", 1)
        for method, params in self.api.calls:
            for row in params.get("reply_markup", {}).get("inline_keyboard", []):
                for b in row:
                    if "callback_data" in b:
                        self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_owner_access_is_checked(self):
        with self.assertRaisesRegex(ValueError, "создателю"):
            self.bot.callback(self.user(2), f"g:{self.r['id']}:setwin:100", 1)
        self.assertEqual(self.store.get(self.r["id"])["winners"], 1)

    def test_repeat_preserves_settings_without_previous_run(self):
        self.active()
        self.r.update(status='finished', mode='time', deadline=123,
                      prizes={'1': 'Телефон'}, prize_emojis=[{'start': 1, 'end': 1, 'id': '123', 'fallback': '💎'}],
                      winning_users=[2], proof={'seed': 'old'}, reminder_sent=True, finished=456)
        self.store.save(self.r)
        self.store.join(self.r['id'], self.user(2))
        original = self.store.get(self.r['id'])
        self.bot.callback(OWNER, f"g:{self.r['id']}:repeat", 1)
        draft = self.store.list(owner=1)[0]
        self.assertNotEqual(draft['id'], original['id'])
        self.assertEqual(draft['status'], 'draft')
        for key in ('channel', 'post', 'prizes', 'prize_emojis', 'button', 'conditions', 'winners'):
            self.assertEqual(draft[key], original[key])
        self.assertIsNone(draft['deadline'])
        self.assertNotEqual(draft['seed'], original['seed'])
        for key in ('message_id', 'published', 'finished', 'winning_users', 'proof', 'reminder_sent'):
            self.assertNotIn(key, draft)
        self.assertEqual(self.store.count(draft['id']), 0)
        draft['prizes']['1'] = 'Новый приз'
        self.store.save(draft)
        self.assertEqual(self.store.get(original['id']), original)
        self.assertEqual(self.store.count(original['id']), 1)

    def test_repeat_requires_owner_and_completed_raffle(self):
        self.r['status'] = 'canceled'
        self.store.save(self.r)
        with self.assertRaisesRegex(ValueError, 'создателю'):
            self.bot.callback(self.user(2), f"g:{self.r['id']}:repeat", 1)
        self.bot.callback(OWNER, f"g:{self.r['id']}:repeat", 1)
        self.assertEqual(len(self.store.list(owner=1)), 2)
        draft = self.store.list(owner=1)[0]
        with self.assertRaisesRegex(ValueError, 'завершённый'):
            self.bot.callback(OWNER, f"g:{draft['id']}:repeat", 1)
        self.assertEqual(len(self.store.list(owner=1)), 2)

    def test_load_configuration_into_existing_draft_keeps_channel(self):
        self.configure('post', 'Старый пост')
        self.r.update(status='finished', winners=3, prizes={'all': 'Приз'},
                      conditions=[dict(CHANNEL)], deadline=123, message_id=777,
                      winning_users=[2], proof={'old': True})
        self.store.save(self.r)
        original = self.store.get(self.r['id'])
        target = self.store.create(1, self.bot.configuration(original))
        target['channel'] = {**CHANNEL, 'id': -1009999999999, 'title': 'Новый канал'}
        target['prize_emojis'] = [{'start': 1, 'end': 3, 'id': '123'}]
        self.store.save(target)
        self.bot.callback(OWNER, f"g:{target['id']}:loadfrom:{original['id']}", 1)
        self.assertEqual(self.store.get(target['id'])['prize_emojis'], target['prize_emojis'])
        self.bot.callback(OWNER, f"g:{target['id']}:confirmload:{original['id']}", 1)
        loaded = self.store.get(target['id'])
        self.assertEqual(loaded['channel'], target['channel'])
        for key in ('post', 'prizes', 'winners', 'conditions'):
            self.assertEqual(loaded[key], original[key])
        self.assertIsNone(loaded['deadline'])
        for key in ('prize_emojis', 'proof', 'winning_users', 'message_id'):
            self.assertNotIn(key, loaded)
        self.assertEqual(self.store.get(original['id']), original)
        self.assertEqual(self.store.count(target['id']), 0)

    def test_configuration_picker_and_load_are_owner_only(self):
        own = self.store.create(1, self.bot.configuration(self.r))
        other = self.store.create(2, self.bot.configuration(self.r))
        self.bot.callback(OWNER, f"g:{self.r['id']}:loadconfig", 1)
        buttons = self.api.calls[-1][1]['reply_markup']['inline_keyboard']
        data = [b.get('callback_data') for row in buttons for b in row]
        self.assertIn(f"g:{self.r['id']}:loadfrom:{own['id']}", data)
        self.assertNotIn(f"g:{self.r['id']}:loadfrom:{other['id']}", data)
        with self.assertRaisesRegex(ValueError, 'недоступен'):
            self.bot.callback(OWNER, f"g:{self.r['id']}:confirmload:{other['id']}", 1)
        self.r['status'] = 'active'
        self.store.save(self.r)
        with self.assertRaisesRegex(ValueError, 'зафиксированы'):
            self.bot.callback(OWNER, f"g:{self.r['id']}:confirmload:{own['id']}", 1)
    def test_channel_admin_check(self):
        self.api.members[2] = {"status": "member"}
        with self.assertRaisesRegex(ValueError, "администратор"):
            self.bot.check_channel(CHANNEL["id"], 2)

    def test_ranges_and_winner_reduction(self):
        self.configure("winners", "5")
        self.configure("prizeplaces", "1: Телефон\n2-5: Наушники")
        self.assertEqual(len(self.r["prizes"]), 5)
        self.configure("winners", "3")
        self.assertEqual(set(self.r["prizes"]), {"1", "2", "3"})
        with self.assertRaises(ValueError):
            self.configure("prizeplaces", "1-3: Приз\n2: Ещё приз")

    def test_publish_requires_post_and_date(self):
        with self.assertRaises(ValueError):
            self.bot.publish(1, self.r)
        self.configure("post", "Пост")
        self.bot.publish(1, self.r)
        self.assertEqual(self.store.session(1)['field'], 'deadline')
        self.configure("deadline", "31.12.2099 12:00")
        self.bot.publish(1, self.r)
        self.assertEqual(self.store.get(self.r["id"])["status"], "active")

    def test_publish_missing_date_opens_input_and_tomorrow_asks_clock(self):
        self.configure('post', 'Пост')
        self.bot.callback(OWNER, f"g:{self.r['id']}:publish", 1)
        self.assertEqual(self.store.session(1)['field'], 'deadline')
        prompt = self.api.calls[-1][1]['text']
        self.assertIn('ДД.ММ.ГГГГ ЧЧ:ММ', prompt)
        self.assertIn('Москва', prompt)
        self.bot.handle({'message': {'from': OWNER, 'chat': {'id': 1, 'type': 'private'}, 'text': 'завтра'}})
        session = self.store.session(1)
        self.assertEqual(session['field'], 'deadline_clock')
        self.assertIn('Во сколько по Москве', self.api.calls[-1][1]['text'])
        self.bot.handle({'message': {'from': OWNER, 'chat': {'id': 1, 'type': 'private'}, 'text': '18:00'}})
        updated = self.store.get(self.r['id'])
        self.assertEqual(datetime.fromtimestamp(updated['deadline'], MSK).strftime('%d.%m.%Y %H:%M'), session['day'] + ' 18:00')
        self.assertEqual(updated['status'], 'draft')
        self.assertEqual(self.store.session(1), {})

    def test_bad_time_retains_input_state_and_quick_button_sets_hours(self):
        self.configure('post', 'Пост')
        self.bot.callback(OWNER, f"g:{self.r['id']}:confirmpublish", 1)
        self.assertEqual(self.store.session(1)['field'], 'deadline')
        with self.assertRaisesRegex(ValueError, 'Одного числа недостаточно'):
            self.bot.input(OWNER, {'text': '1'}, self.store.session(1))
        self.assertEqual(self.store.session(1)['field'], 'deadline')
        with patch('rafflebot.bot.time.time', return_value=2000000000):
            self.bot.callback(OWNER, f"g:{self.r['id']}:delay:3600", 1)
        self.assertEqual(self.store.get(self.r['id'])['deadline'], 2000003600)
        self.assertEqual(self.store.get(self.r['id'])['status'], 'draft')

    def test_post_entities_and_photo(self):
        self.configure("post", "Жирный", entities=[{"type": "bold", "offset": 0, "length": 6}])
        method, params = self.bot.compose(self.r)
        self.assertEqual(params["entities"][0]["type"], "bold")
        self.configure("post", photo=[{"file_id": "file"}], caption="Фото", caption_entities=[])
        method, params = self.bot.compose(self.r)
        self.assertEqual(method, "sendPhoto")
        self.assertEqual(params["caption"], "Фото")
        self.assertNotIn("Отпечаток", params["caption"])

    def test_public_text_length_checked_before_save(self):
        with self.assertRaisesRegex(ValueError, "слишком длинный"):
            self.configure("post", "🙂" * 3000)
        self.assertIsNone(self.store.get(self.r["id"])["post"])

    def test_standard_caption_matches_reference_layout(self):
        self.configure("winners", "100")
        self.configure("prizeplaces", "1: 15 000 ₽\n2: 15 000 ₽\n3: 10 000 ₽\n4-8: 7 000 ₽\n9-20: Gold Dyson\n21-60: Telegram Premium на год\n61-100: Telegram Premium на 3 месяца")
        self.r.update(mode="count", target=333, conditions=[self.r["channel"]])
        self.store.save(self.r)
        self.configure("post", photo=[{"file_id": "file"}])
        _, params = self.bot.compose(self.r)
        caption = params["caption"]
        self.assertTrue(caption.startswith("⭐ 100 призов - 100 победителей\n\nП Р И З Ы :"))
        self.assertIn("🥇 1 место — 15 000 ₽", caption)
        self.assertIn("🥈 2 место — 15 000 ₽", caption)
        self.assertIn("4-8 место — 7 000 ₽", caption)
        self.assertIn("61-100 место — Telegram Premium на 3 месяца", caption)
        self.assertIn("Поставить любую реакцию", caption)
        self.assertIn("Итоги на 333 участников!", caption)
        self.assertTrue(caption.endswith("Нажать кнопку участвовать ↓"))
        for technical_text in ("Отпечаток", "ключ", "жеребьёвка", "билет", "обезлич", "см. условия"):
            self.assertNotIn(technical_text, caption)
        self.assertLessEqual(len(caption.encode("utf-16-le")) // 2, 1024)
        link = next(e for e in params["caption_entities"] if e["type"] == "text_link")
        data = caption.encode("utf-16-le")
        self.assertEqual(data[link["offset"] * 2:(link["offset"] + link["length"]) * 2].decode("utf-16-le"), CHANNEL["title"])

    def test_custom_caption_is_unchanged_and_keeps_photo(self):
        self.configure("post", photo=[{"file_id": "original_photo"}])
        text = "⭐ Мой розыгрыш\n\nП Р И З Ы :\n🥇 Телефон"
        entities = [{"type": "bold", "offset": 0, "length": 16}]
        self.configure("caption", text, entities=entities)
        method, params = self.bot.compose(self.r)
        self.assertEqual(method, "sendPhoto")
        self.assertEqual(params["photo"], "original_photo")
        self.assertEqual(params["caption"], text)
        self.assertEqual(params["caption_entities"], entities)
        self.configure("caption", "-")
        _, params = self.bot.compose(self.r)
        self.assertIn("Нажать кнопку участвовать", params["caption"])

    def test_all_50_prizes_visible_in_menu_and_preview_with_custom_caption(self):
        self.configure('winners', '50')
        self.configure('prizeplaces', '1: Телефон\n2: Наушники\n3: Часы\n4-5: 10 000 ₽\n6-7: 7 000 ₽\n8-25: 5 000 ₽\n26-50: Telegram Premium на год')
        self.configure('post', photo=[{'file_id': 'photo'}], caption='Мой розыгрыш 🥵')
        self.bot.settings(1, self.r, 'prizes', mid=1)
        menu = self.api.calls[-1][1]['text']
        self.assertIn('Призы заданы для 50 мест', menu)
        self.assertIn('8–25: 5 000 ₽', menu)
        self.assertIn('26–50: Telegram Premium на год', menu)
        self.bot.callback(OWNER, f"g:{self.r['id']}:preview", 1)
        preview = next(params for method, params in reversed(self.api.calls) if method == 'sendPhoto')
        self.assertTrue(preview['caption'].startswith('Мой розыгрыш 🥵\n\n'))
        self.assertIn('8–25 место — 5 000 ₽', preview['caption'])
        self.assertIn('26–50 место — Telegram Premium на год', preview['caption'])
        self.assertNotIn('Отпечаток', preview['caption'])
        self.assertEqual(len(self.store.get(self.r['id'])['prizes']), 50)
        entity = preview['caption_entities'][0]
        data = preview['caption'].encode('utf-16-le')
        self.assertEqual(data[entity['offset']*2:(entity['offset']+entity['length'])*2].decode('utf-16-le'), '🎁 П Р И З Ы :')

    def test_prize_lines_have_spacing_and_custom_emoji_entities(self):
        self.configure('winners', '6')
        self.configure('prizeplaces', '1-6: Сертификат')
        self.configure('post', photo=[{'file_id': 'photo'}], caption='Розыгрыш 🥵')
        self.configure('prizeemoji', '4-5: 5368324170671202286')
        _, params = self.bot.compose(self.r)
        self.assertIn('\n\n💎 4–5 место — Сертификат', params['caption'])
        self.assertIn('\n\n🎁 6 место — Сертификат', params['caption'])
        emojis = [e for e in params['caption_entities'] if e['type'] == 'custom_emoji']
        self.assertEqual(len(emojis), 1)
        self.assertEqual(emojis[0]['custom_emoji_id'], '5368324170671202286')
        raw = params['caption'].encode('utf-16-le')
        e = emojis[0]
        self.assertEqual(raw[e['offset']*2:(e['offset']+e['length'])*2].decode('utf-16-le'), '💎')
        label = next(e for e in params['caption_entities'] if e['type'] == 'bold' and raw[e['offset']*2:(e['offset']+e['length'])*2].decode('utf-16-le') == '4–5 место')
        self.assertGreater(label['offset'], emojis[0]['offset'])
        self.assertIn((4, 5, 'Сертификат'), self.bot.prize_groups(self.r))

    def test_emoji_overrides_reset_and_invalid_id_does_not_save(self):
        self.configure('winners', '6')
        self.configure('prizeplaces', '1-6: Сертификат')
        self.configure('prizeemoji', '4-6: 111')
        self.configure('prizeemoji', '5: 222')
        self.assertEqual(self.bot.prize_icon(self.r, 4)[1], '111')
        self.assertEqual(self.bot.prize_icon(self.r, 5)[1], '222')
        self.configure('prizeemoji', '5: -')
        self.assertEqual(self.bot.prize_icon(self.r, 5), ('🎁', None))
        original = self.r.get('prize_emojis')
        with self.assertRaises(ValueError):
            self.configure('prizeemoji', '6: 0')
        self.assertEqual(self.store.get(self.r['id'])['prize_emojis'], original)
        with self.assertRaises(ValueError):
            self.configure('prizeemoji', '7: 111')
        self.bot.callback(OWNER, f"g:{self.r['id']}:clearprizeemoji", 1)
        self.assertEqual(self.store.get(self.r['id'])['prize_emojis'], [])

    def test_custom_emoji_rules_split_shared_prize_without_expanding_huge_count(self):
        r = dict(self.r, winners=1000000, prizes={'all': 'Сертификат'}, prize_emojis=[{'start': 100, 'end': 200, 'id': '111', 'fallback': '💎'}])
        groups = self.bot.prize_groups(r)
        self.assertEqual([(a,b) for a,b,_ in groups], [(1,99),(100,200),(201,1000000)])

    def test_double_entry_and_self_referral(self):
        self.active(referrals=True)
        user = self.user(2)
        self.bot.join(user, self.r["id"], 2)
        self.bot.join(user, self.r["id"], 2)
        self.assertEqual(self.store.count(self.r["id"]), 1)
        self.assertIsNone(self.store.participants(self.r["id"])[0]["referrer"])

    def test_subscription_and_username_restrictions(self):
        self.active(conditions=[self.r["channel"]], require_username=True)
        with self.assertRaisesRegex(ValueError, "username"):
            self.bot.join(self.user(2, False), self.r["id"])
        self.api.members[2] = {"status": "left"}
        self.bot.join(self.user(2), self.r["id"])
        self.assertEqual(self.store.count(self.r["id"]), 0)
        self.api.members[2] = {"status": "member"}
        self.bot.join(self.user(2), self.r["id"])
        self.assertEqual(self.store.count(self.r["id"]), 1)

    def test_published_settings_frozen(self):
        self.active()
        with self.assertRaises(ValueError):
            self.bot.callback(OWNER, f"g:{self.r['id']}:setwin:100", 1)
        with self.assertRaises(ValueError):
            self.configure("title", "Changed")

    def test_count_closes_and_selects_unique_winners(self):
        self.active(winners=2, target=3)
        self.r["mode"] = "count"
        self.store.save(self.r)
        for uid in (2, 3, 4):
            self.bot.join(self.user(uid), self.r["id"])
        result = self.store.get(self.r["id"])
        self.assertEqual(result["status"], "finished")
        self.assertEqual(len({x["id"] for x in result["winning_users"]}), 2)
        self.assertTrue(verify(result["proof"]))
        with self.assertRaises(ValueError):
            self.bot.join(self.user(5), self.r["id"])

    def test_recheck_subscriptions_and_cap_referral_bonus(self):
        self.active(referrals=True, referral_bonus=100, conditions=[self.r["channel"]], winners=100)
        self.bot.join(self.user(2), self.r["id"])
        for uid in range(3, 10):
            self.bot.join(self.user(uid), self.r["id"], 2)
        self.api.members[9] = {"status": "left"}
        self.bot.finish(self.store.get(self.r["id"]))
        result = self.store.get(self.r["id"])
        self.assertEqual(len(result["winning_users"]), 7)
        self.assertEqual(max(x["weight"] for x in result["proof"]["eligible"]), 400)
        self.assertNotIn(9, [x["id"] for x in result["winning_users"]])

    def test_referral_without_eligible_friend_does_not_count(self):
        self.active(referrals=True, conditions=[self.r["channel"]])
        self.bot.join(self.user(2), self.r["id"])
        self.bot.join(self.user(3), self.r["id"], 2)
        self.api.members[3] = {"status": "left"}
        self.bot.finish(self.store.get(self.r["id"]))
        proof = self.store.get(self.r["id"])["proof"]
        self.assertEqual(proof["eligible"][0]["weight"], 100)

    def test_api_failure_does_not_draw_or_disqualify(self):
        self.active(conditions=[self.r["channel"]])
        self.bot.join(self.user(2), self.r["id"])
        self.api.fail["getChatMember"] = TelegramError(0, "Network down")
        with self.assertRaises(TelegramError):
            self.bot.finish(self.store.get(self.r["id"]))
        self.assertEqual(self.store.get(self.r["id"])["status"], "active")

    def test_finish_does_not_redraw_and_notification_jobs_unique(self):
        self.active(notify_losers=True)
        for uid in range(2, 5):
            self.bot.join(self.user(uid), self.r["id"])
        self.bot.finish(self.store.get(self.r["id"]))
        before = self.store.get(self.r["id"])
        jobs = self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
        self.bot.finish(self.store.get(self.r["id"]))
        self.assertEqual(self.store.get(self.r["id"])["proof"], before["proof"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], jobs)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE task_key LIKE 'loser:%'").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE task_key LIKE 'results:%'").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE task_key LIKE 'winner:%'").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT state FROM outbox WHERE task_key LIKE 'counter:%'").fetchone()[0], "superseded")

    def test_timed_draw_after_offline_period(self):
        self.active()
        self.bot.join(self.user(2), self.r["id"])
        self.r = self.store.get(self.r["id"])
        self.r.update(mode="time", deadline=int(time.time()) - 3600)
        self.store.save(self.r)
        with patch("rafflebot.bot.time.sleep"):
            self.bot.tick()
        self.assertEqual(self.store.get(self.r["id"])["status"], "finished")

    def test_publish_ambiguous_failure_requires_recovery(self):
        self.configure("post", "Пост")
        self.r["mode"] = "manual"
        self.store.save(self.r)
        self.api.fail["sendMessage"] = TelegramError(0, "Timeout")
        with self.assertRaises(TelegramError):
            self.bot.publish(1, self.r)
        self.assertEqual(self.store.get(self.r["id"])["status"], "publishing")
        del self.api.fail["sendMessage"]
        message = {"text": "Пост " + commitment(self.r["seed"]), "forward_origin": {"type": "channel", "chat": CHANNEL, "message_id": 150}}
        self.bot.input(OWNER, message, {"field": "recover", "rid": self.r["id"]})
        self.assertEqual(self.store.get(self.r["id"])["message_id"], 150)
        self.assertEqual(self.store.get(self.r["id"])["status"], "active")

    def test_invalid_publish_is_retryable(self):
        self.configure("post", "Пост")
        self.r["mode"] = "manual"
        self.api.fail["sendMessage"] = TelegramError(400, "Invalid emoji")
        with self.assertRaises(TelegramError):
            self.bot.publish(1, self.r)
        self.assertEqual(self.store.get(self.r["id"])["status"], "draft")

    def test_outbox_rate_limit_and_blocked_user(self):
        rid = self.r["id"]
        self.store.enqueue(rid, "a", "sendMessage", {"chat_id": 2, "text": "hello"})
        self.store.enqueue(rid, "b", "sendMessage", {"chat_id": 3, "text": "hello"})
        self.api.fail["sendMessage"] = TelegramError(429, "Rate limit", 60)
        self.bot.drain()
        self.assertEqual(len(self.store.pending()), 0)
        self.api.fail["sendMessage"] = TelegramError(403, "Blocked")
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET next_attempt=0")
        self.store.set_meta('outbound_pause', 0)
        self.bot.drain()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE state='failed'").fetchone()[0], 2)

    def test_cancel_and_draft_delete(self):
        self.bot.callback(OWNER, f"g:{self.r['id']}:confirmdelete", 1)
        self.assertEqual(self.store.get(self.r["id"])["status"], "deleted")
        with self.assertRaises(ValueError):
            self.bot.participation(self.user(2), self.r["id"])

    def test_start_message_routes_to_menu(self):
        self.bot.handle({"update_id": 1, "message": {"from": self.user(2), "chat": {"id": 2, "type": "private"}, "text": "/start"}})
        method, params = self.api.calls[-1]
        self.assertEqual(method, "sendMessage")
        self.assertIn("keyboard", params["reply_markup"])
        self.assertIn("Создать розыгрыш", params["reply_markup"]["keyboard"][0][0]["text"])

    def test_participation_card_is_grouped_and_repeat_entry_is_short(self):
        self.active(prizes={'1': 'Телефон', '2': 'Наушники', '3': 'Часы', '4': 'Сертификат', '5': 'Сертификат'})
        user = self.user(2)
        self.bot.participation(user, self.r['id'])
        card = self.api.calls[-1][1]
        self.assertIn('<b>🎁 Призы</b>\n\n', card['text'])
        self.assertIn('<b>4–5 место</b> — Сертификат', card['text'])
        self.bot.join(user, self.r['id'], mid=100)
        method, params = self.api.calls[-1]
        self.assertEqual(method, 'editMessageText')
        self.assertEqual(params['reply_markup']['inline_keyboard'], [])
        self.assertNotIn('Участников:', params['text'])
        self.bot.participation(user, self.r['id'])
        self.assertEqual(self.api.calls[-1][1]['text'], '✅ <b>Вы уже участвуете!</b>')

    def test_only_winners_get_notifications_without_public_result_list(self):
        self.active(notify_losers=True)
        for uid in (2, 3):
            self.bot.join(self.user(uid), self.r['id'])
        self.bot.finish(self.store.get(self.r['id']))
        result = self.store.get(self.r['id'])
        jobs = self.store.pending(100)
        sends = [json.loads(job['params']) for job in jobs if job['method'] == 'sendMessage']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]['chat_id'], result['winning_users'][0]['id'])
        self.assertIn('<b>Вы выиграли!</b>', sends[0]['text'])
        self.assertNotIn('reply_markup', sends[0])
        self.bot.results(9, result)
        text = self.api.calls[-1][1]['text']
        self.assertNotIn('user_', text)
        self.assertNotIn('Участников:', text)

    def test_legacy_queued_result_list_is_suppressed(self):
        self.store.enqueue(self.r['id'], 'results:legacy', 'sendMessage', {'chat_id': 2, 'text': 'Old winners'})
        self.store.enqueue(self.r['id'], 'loser:legacy', 'sendMessage', {'chat_id': 3, 'text': 'Old result'})
        self.api.calls.clear()
        self.bot.drain()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE state='superseded'").fetchone()[0], 2)

    def test_cancel_closes_participation_and_counter(self):
        self.active()
        self.bot.join(self.user(2), self.r["id"])
        self.bot.callback(OWNER, f"g:{self.r['id']}:confirmcancel", 1)
        self.assertEqual(self.store.get(self.r["id"])["status"], "canceled")
        self.assertEqual(self.store.db.execute("SELECT state FROM outbox WHERE task_key LIKE 'counter:%'").fetchone()[0], "superseded")
        with self.assertRaises(ValueError):
            self.bot.join(self.user(3), self.r["id"])

    def test_proof_detects_tampering(self):
        self.active()
        self.bot.join(self.user(2), self.r["id"])
        self.bot.finish(self.store.get(self.r["id"]))
        proof = self.store.get(self.r["id"])["proof"]
        original = proof["commitment"]
        self.assertTrue(verify(proof, original))
        with self.assertRaises(ValueError):
            verify(proof, "0" * 64)
        proof["eligible"][0]["weight"] = 200
        with self.assertRaises(ValueError):
            verify(proof)

    def test_blocked_winner_never_wins(self):
        self.active(winners=10)
        blocked_uid = 5402217209
        self.bot.join(self.user(blocked_uid), self.r["id"])
        self.bot.join(self.user(101), self.r["id"])
        self.bot.join(self.user(102), self.r["id"])
        self.bot.finish(self.store.get(self.r["id"]))
        result = self.store.get(self.r["id"])
        winner_ids = [w["id"] for w in result["winning_users"]]
        self.assertNotIn(blocked_uid, winner_ids)
        self.assertIn(101, winner_ids)
        self.assertIn(102, winner_ids)

        # Even if blocked user is the only participant, they still do not win
        r2 = self.store.create(1, {"channel": self.r["channel"], "title": "Test 2", "post": None,
                                   "winners": 1, "prizes": {}, "mode": "manual", "deadline": None, "target": None,
                                   "conditions": [], "referrals": False, "referral_bonus": 100, "require_username": False,
                                   "notify_losers": False, "contact": "", "button": {"text": "🎁 Участвовать"},
                                   "extra": [], "show_count": True, "reminder": False, "reminder_minutes": 60,
                                   "pin": False, "seed": "a" * 64, "message_id": 555})
        r2["status"] = "active"
        self.store.save(r2)
        self.bot.join(self.user(blocked_uid), r2["id"])
        self.bot.finish(self.store.get(r2["id"]))
        result2 = self.store.get(r2["id"])
        self.assertEqual(result2["winning_users"], [])


class DrawTests(unittest.TestCase):
    def test_reproducibility_without_replacement(self):
        entries = [{"ticket": str(i), "weight": (i + 1) * 100} for i in range(20)]
        seed = "12" * 32
        result = weighted_draw(entries, 50, seed)
        self.assertEqual(len(result), 20)
        self.assertEqual(len(set(result)), 20)
        self.assertEqual(result, weighted_draw(list(reversed(entries)), 50, seed))
        self.assertEqual(weighted_draw([], 5, seed), [])

    def test_weights_have_effect(self):
        entries = [{"ticket": "a", "weight": 100}, {"ticket": "b", "weight": 400}]
        b_wins = sum(weighted_draw(entries, 1, f"{i:064x}")[0] == "b" for i in range(1000))
        self.assertTrue(740 < b_wins < 860, b_wins)

    def test_invalid_weights_rejected(self):
        for weight in (0, -1, 0.5, True):
            with self.assertRaises(ValueError):
                weighted_draw([{"ticket": "a", "weight": weight}], 1, "11" * 32)

    def test_storage_survives_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "db.sqlite3")
            s = Store(path)
            r = s.create(1, {"seed": "12" * 32})
            s.join(r["id"], {"id": 2})
            s.set_meta("offset", 100)
            s.db.close()
            s = Store(path)
            self.assertEqual(s.get(r["id"])["seed"], "12" * 32)
            self.assertEqual(s.count(r["id"]), 1)
            self.assertEqual(s.meta("offset"), "100")
            s.db.close()

    def test_parsers(self):
        self.assertEqual(chat_reference("https://t.me/testchannel"), "@testchannel")
        with self.assertRaises(ValueError):
            chat_reference("https://evil.example/testchannel")
        with self.assertRaises(ValueError):
            parse_deadline("01.01.2020 12:00")
        self.assertEqual(Bot.parse_button("Участвовать | зелёный")["style"], "success")
        self.assertEqual(Bot.parse_button("Правила | https://example.com | синий", True)["url"], "https://example.com")
        with self.assertRaises(ValueError):
            Bot.parse_button("Правила | javascript:alert(1)", True)

    def test_date_formats_and_relative_times_use_moscow(self):
        now = datetime(2026, 10, 4, 12, 0, tzinfo=MSK).timestamp()
        self.assertEqual(parse_deadline('через 1 час', now), now + 3600)
        self.assertEqual(parse_deadline('через 30 минут', now), now + 1800)
        self.assertEqual(parse_deadline('завтра 18:00', now), datetime(2026, 10, 5, 18, 0, tzinfo=MSK).timestamp())
        self.assertEqual(parse_deadline('05.10.2026 18:00', now), parse_deadline('завтра в 18:00', now))
        self.assertEqual(parse_deadline('18:00', now), datetime(2026, 10, 4, 18, 0, tzinfo=MSK).timestamp())
        for text in ('1', 'завтра', '11:00', 'через 0 часов', '31.02.2027 12:00'):
            with self.assertRaises(ValueError):
                parse_deadline(text, now)


if __name__ == "__main__":
    unittest.main()

