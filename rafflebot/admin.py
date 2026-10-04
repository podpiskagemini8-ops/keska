import csv
import io
import json
import time

from .api import TelegramError
from .bot import button, esc, format_date, keyboard

ADMIN_ID = 8259479085


class AdminPanel:
    def __init__(self, bot):
        self.bot, self.store, self.api = bot, bot.store, bot.api

    def show(self, uid, mid=None):
        if uid != ADMIN_ID:
            return
        self.store.session(uid, {})
        db = self.store.db
        users = db.execute('SELECT count(*) FROM users').fetchone()[0]
        distinct = db.execute('SELECT count(DISTINCT user_id) FROM participants').fetchone()[0]
        entries = db.execute('SELECT count(*) FROM participants').fetchone()[0]
        active = db.execute("SELECT count(*) FROM raffles WHERE status='active'").fetchone()[0]
        active_people = db.execute("SELECT count(DISTINCT p.user_id) FROM participants p JOIN raffles r ON r.id=p.raffle WHERE r.status='active'").fetchone()[0]
        text = (f'🔐 <b>Админ-панель</b>\n\n👤 Пользователей в базе бота: {users}\n'
                f'👥 Уникальных участников за всё время: {distinct}\n🎟 Всего заявок в розыгрыши: {entries}\n'
                f'🟢 Активных розыгрышей: {active}\n👥 Участников в активных розыгрышах: {active_people}\n'
                f'📣 Подписались на рекламу и доступны для отправки: {self.store.advertising_count()}\n\n'
                'Это сохранённые пользователи, которые обращались к боту; Telegram не показывает, кто сейчас онлайн.')
        self.bot.say(uid, text, keyboard([button('👥 База пользователей', 'admin:users:0')],
            [button('🎉 Розыгрыши и участники', 'admin:raffles:0')],
            [button('📣 Подготовить рассылку', 'admin:new')],
            [button('📨 Рассылки и статистика', 'admin:campaigns:0')],
            [button('🔄 Обновить', 'admin:home')]), mid)

    def users(self, uid, page=0, mid=None):
        if uid != ADMIN_ID:
            return
        total = self.store.db.execute('SELECT count(*) FROM users').fetchone()[0]
        page = max(0, min(page, max(0, (total - 1) // 10)))
        rows = self.store.db.execute('SELECT u.*,coalesce(m.enabled,0) AS subscribed FROM users u LEFT JOIN marketing m ON m.user_id=u.id ORDER BY u.id LIMIT 10 OFFSET ?', (page * 10,)).fetchall()
        lines = [f'👥 <b>База пользователей</b> · {total}']
        for row in rows:
            user = json.loads(row['data'])
            lines.append(f"\n<code>{row['id']}</code> · {esc(user.get('first_name', ''))} {esc('@' + user['username'] if user.get('username') else '')}\nРеклама: {'подписан' if row['subscribed'] else 'не подписан'}")
        nav = self.nav('users', page, total, 10)
        self.bot.say(uid, '\n'.join(lines), keyboard(*nav, [button('📥 Выгрузить CSV', 'admin:exportusers')], [button('⬅️ Панель', 'admin:home')]), mid)

    @staticmethod
    def nav(kind, page, total, size):
        buttons = []
        if page > 0:
            buttons.append(button('⬅️', f'admin:{kind}:{page-1}'))
        if (page + 1) * size < total:
            buttons.append(button('➡️', f'admin:{kind}:{page+1}'))
        return [buttons] if buttons else []

    def raffles(self, uid, page=0, mid=None):
        if uid != ADMIN_ID:
            return
        items = [r for r in self.store.list() if r['status'] != 'deleted']
        page = max(0, min(page, max(0, (len(items)-1)//8)))
        lines = ['🎉 <b>Розыгрыши</b>']
        buttons = []
        for r in items[page*8:page*8+8]:
            actual = self.store.count(r['id'])
            shown = self.bot.participant_count(r)
            count_str = f"{shown}" if shown == actual else f"{shown} (в базе: {actual})"
            lines.append(f"\n#{r['id']} {esc(r['title'])}\nСтатус: {esc(r['status'])} · участников: {count_str}\nСоздатель: <code>{r['owner']}</code>")
            buttons.append([
                button(f"📥 Участники #{r['id']}", f"admin:exportparticipants:{r['id']}"),
                button(f"👥 Счётчик #{r['id']}", f"admin:setcount:{r['id']}")
            ])
        self.bot.say(uid, '\n'.join(lines), keyboard(*buttons, *self.nav('raffles', page, len(items), 8), [button('⬅️ Панель', 'admin:home')]), mid)

    @staticmethod
    def csv_bytes(columns, rows):
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value for value in row])
        return stream.getvalue().encode('utf-8-sig')

    def export(self, uid, rid=None):
        if uid != ADMIN_ID:
            return
        if rid is not None:
            r = self.store.get(rid)
            if not r:
                raise ValueError('Розыгрыш не найден.')
            people = self.store.participants(rid)
            rows = [(p['id'], p.get('username', ''), p.get('first_name', ''), p.get('last_name', ''), format_date(p['joined'])) for p in people]
            columns = ['telegram_id', 'username', 'first_name', 'last_name', 'joined_msk']
            filename = f'giveaway-{rid}-participants.csv'
        else:
            rows = []
            for row in self.store.db.execute('SELECT u.*,coalesce(m.enabled,0) AS subscribed FROM users u LEFT JOIN marketing m ON m.user_id=u.id ORDER BY u.id'):
                p = json.loads(row['data'])
                rows.append((row['id'], p.get('username', ''), p.get('first_name', ''), p.get('last_name', ''), format_date(row['last_seen']) if row['last_seen'] else '', row['subscribed'], row['can_message']))
            columns = ['telegram_id', 'username', 'first_name', 'last_name', 'last_seen_msk', 'advertising_opt_in', 'can_message']
            filename = 'bot-users.csv'
        self.api.document(uid, filename, self.csv_bytes(columns, rows), 'База пользователей' if rid is None else f'Участники розыгрыша #{rid}')

    def campaign(self, cid):
        return self.store.db.execute('SELECT * FROM campaigns WHERE id=?', (cid,)).fetchone()

    def campaign_info(self, uid, cid, mid=None):
        if uid != ADMIN_ID:
            return
        row = self.campaign(cid)
        if not row:
            raise ValueError('Рассылка не найдена.')
        counts = {r[0]: r[1] for r in self.store.db.execute('SELECT state,count(*) FROM campaign_deliveries WHERE campaign=? GROUP BY state', (cid,))}
        text = (f"📨 <b>Рассылка #{cid}</b> · {esc(row['status'])}\n\n"
                f"Доставлено: {counts.get('sent', 0)}\nВ очереди: {counts.get('pending', 0)}\n"
                f"Не доставлено: {counts.get('failed', 0)}\nОтменено / отписались: {counts.get('skipped', 0)}\n"
                f"Ответ Telegram потерян: {counts.get('unknown', 0)}")
        buttons = []
        if row['status'] == 'draft':
            text += f'\n\nПодписавшихся получателей сейчас: {self.store.advertising_count()}. После подтверждения бот отправит им этот пост.'
            buttons.append([button('👁 Предпросмотр', f'admin:preview:{cid}')])
            buttons.append([button('📣 Подтвердить отправку', f'admin:send:{cid}', style='success')])
        if row['status'] in ('draft', 'queued'):
            buttons.append([button('⛔ Отменить рассылку', f'admin:cancel:{cid}', style='danger')])
        buttons += [[button('🔄 Обновить', f'admin:campaign:{cid}')], [button('⬅️ Панель', 'admin:home')]]
        self.bot.say(uid, text, keyboard(*buttons), mid)

    def input(self, uid, message):
        if uid != ADMIN_ID:
            return
        session = self.store.session(uid)
        field = session.get('field')
        if field == 'admin_set_count':
            text = message.get('text', '').strip()
            if not text.isdigit():
                raise ValueError('Пришлите целое неотрицательное число (например: 274).')
            target_count = int(text)
            rid = session.get('rid')
            r = self.store.get(rid)
            if not r:
                self.store.session(uid, {})
                raise ValueError('Розыгрыш не найден.')
            actual = self.store.count(r['id'])
            r['visual_offset'] = target_count - actual
            self.store.save(r)
            self.store.session(uid, {})
            if r.get('status') == 'active' and r.get('message_id') and r.get('show_count'):
                self.bot.update_count(r)
                self.bot.drain()
            next_count = target_count + 1
            return self.bot.say(uid,
                f"✅ <b>Счётчик участников обновлён!</b>\n\n"
                f"Розыгрыш: <b>#{r['id']} {esc(r['title'])}</b>\n"
                f"• Теперь отображается: <b>{target_count}</b>\n"
                f"• Реальных участников в базе: <b>{actual}</b>\n\n"
                f"В канале на кнопке теперь отображается {target_count}. "
                f"Когда следующий пользователь нажмёт кнопку, отобразится {next_count}, затем {next_count + 1} и так далее.",
                keyboard([button('⬅️ К розыгрышам', 'admin:raffles:0')]))
        kind = next((k for k in ('photo', 'video', 'animation', 'document') if k in message), 'text')
        text = message.get('text', '') if kind == 'text' else message.get('caption', '')
        if kind == 'text' and not text:
            raise ValueError('Пришлите текст или фото, видео, GIF, документ с рекламным постом.')
        if len(text.encode('utf-16-le')) // 2 > (4096 if kind == 'text' else 1024):
            raise ValueError('Слишком длинный текст рекламного поста.')
        post = {'type': kind, 'text': text, 'entities': message.get('entities' if kind == 'text' else 'caption_entities', [])}
        if kind != 'text':
            media = message[kind][-1] if kind == 'photo' else message[kind]
            post['file_id'] = media['file_id']
        with self.store.db:
            cid = self.store.db.execute('INSERT INTO campaigns(data,created) VALUES(?,?)', (json.dumps(post), int(time.time()))).lastrowid
        self.store.session(uid, {})
        self.preview(uid, cid)
        self.campaign_info(uid, cid)

    @staticmethod
    def post_params(post, uid):
        unsubscribe = keyboard([button('Отписаться от рекламы', 'marketing:off')])
        if post['type'] == 'text':
            return 'sendMessage', {'chat_id': uid, 'text': post['text'], 'entities': post['entities'], 'reply_markup': unsubscribe}
        kind = post['type']
        return 'send' + kind.title(), {'chat_id': uid, kind: post['file_id'], 'caption': post['text'], 'caption_entities': post['entities'], 'reply_markup': unsubscribe}

    def preview(self, uid, cid):
        if uid != ADMIN_ID:
            return
        row = self.campaign(cid)
        if not row:
            raise ValueError('Рассылка не найдена.')
        method, params = self.post_params(json.loads(row['data']), uid)
        self.api.call(method, **params)

    def handle(self, uid, data, mid=None):
        if uid != ADMIN_ID:
            return
        parts = data.split(':')
        action = parts[1]
        number = int(parts[2]) if len(parts) > 2 else 0
        if action == 'home':
            return self.show(uid, mid)
        if action == 'users':
            return self.users(uid, number, mid)
        if action == 'raffles':
            return self.raffles(uid, number, mid)
        if action == 'exportusers':
            return self.export(uid)
        if action == 'exportparticipants':
            return self.export(uid, number)
        if action == 'setcount':
            r = self.store.get(number)
            if not r:
                raise ValueError('Розыгрыш не найден.')
            actual = self.store.count(r['id'])
            shown = self.bot.participant_count(r)
            self.store.session(uid, {'field': 'admin_set_count', 'rid': r['id']})
            text = (f"👥 <b>Изменение счётчика участников #{r['id']}</b>\n\n"
                    f"Розыгрыш: <b>{esc(r['title'])}</b>\n"
                    f"Статус: {esc(r['status'])}\n"
                    f"📣 Канал: {esc(r['channel']['title'])}\n\n"
                    f"• Сейчас отображается: <b>{shown}</b>\n"
                    f"• Реальных участников в базе: <b>{actual}</b>\n\n"
                    f"Пришлите новое визуальное число участников (например: <code>274</code>).\n"
                    f"Счётчик на кнопке в канале обновится сразу, а при последующих нажатиях участников продолжит расти (275, 276...).")
            actions = []
            if r.get('visual_offset'):
                actions.append([button('🔄 Сбросить на реальное число', f'admin:resetcount:{r["id"]}')])
            actions.append([button('⬅️ Назад к розыгрышам', 'admin:raffles:0')])
            return self.bot.say(uid, text, keyboard(*actions), mid)
        if action == 'resetcount':
            r = self.store.get(number)
            if not r:
                raise ValueError('Розыгрыш не найден.')
            r.pop('visual_offset', None)
            r.pop('initial_participants', None)
            self.store.save(r)
            if r.get('status') == 'active' and r.get('message_id') and r.get('show_count'):
                self.bot.update_count(r)
                self.bot.drain()
            actual = self.store.count(r['id'])
            self.store.session(uid, {})
            return self.bot.say(uid, f"✅ Счётчик розыгрыша #{r['id']} сброшен на реальное число участников ({actual}).",
                                keyboard([button('⬅️ К розыгрышам', 'admin:raffles:0')]), mid)
        if action == 'new':
            self.store.session(uid, {'field': 'admin_campaign'})
            return self.bot.say(uid, '📣 Пришлите рекламный пост: текст или файл с подписью. Сначала будет предпросмотр; отправка произойдёт только после вашей кнопки подтверждения. Получатели — пользователи, разрешившие рекламу в профиле.', keyboard([button('Отмена', 'admin:home')]), mid)
        if action == 'campaigns':
            total = self.store.db.execute('SELECT count(*) FROM campaigns').fetchone()[0]
            number = max(0, min(number, max(0, (total-1)//8)))
            rows = self.store.db.execute('SELECT id,status FROM campaigns ORDER BY id DESC LIMIT 8 OFFSET ?', (number*8,)).fetchall()
            return self.bot.say(uid, '📨 <b>Рассылки</b>', keyboard(*[[button(f"#{r['id']} · {r['status']}", f"admin:campaign:{r['id']}")] for r in rows], *self.nav('campaigns', number, total, 8), [button('⬅️ Панель', 'admin:home')]), mid)
        if action == 'campaign':
            return self.campaign_info(uid, number, mid)
        if action == 'preview':
            self.preview(uid, number)
            return self.campaign_info(uid, number)
        if action == 'send':
            with self.store.db:
                cur = self.store.db.execute("UPDATE campaigns SET status='queued' WHERE id=? AND status='draft'", (number,))
                if cur.rowcount:
                    self.store.db.execute("INSERT OR IGNORE INTO campaign_deliveries(campaign,user_id) SELECT ?,u.id FROM users u JOIN marketing m ON m.user_id=u.id WHERE m.enabled=1 AND u.can_message=1", (number,))
            return self.campaign_info(uid, number, mid)
        if action == 'cancel':
            with self.store.db:
                self.store.db.execute("UPDATE campaigns SET status='canceled' WHERE id=? AND status IN ('draft','queued')", (number,))
                self.store.db.execute("UPDATE campaign_deliveries SET state='skipped' WHERE campaign=? AND state='pending'", (number,))
            return self.campaign_info(uid, number, mid)

    def drain(self):
        now = int(time.time())
        if now < int(self.store.meta('outbound_pause', '0')):
            return
        jobs = self.store.db.execute("SELECT d.*,c.data FROM campaign_deliveries d JOIN campaigns c ON c.id=d.campaign WHERE c.status='queued' AND d.state='pending' AND d.next_attempt<=? ORDER BY d.campaign,d.user_id LIMIT 8", (now,)).fetchall()
        for job in jobs:
            if not self.store.advertising(job['user_id']):
                state = 'skipped'
            else:
                method, params = self.post_params(json.loads(job['data']), job['user_id'])
                # Claim before sending: a crash cannot silently repeat an ad.
                with self.store.db:
                    self.store.db.execute("UPDATE campaign_deliveries SET state='unknown' WHERE campaign=? AND user_id=?", (job['campaign'], job['user_id']))
                try:
                    self.api.call(method, **params)
                    state = 'sent'
                except TelegramError as e:
                    state = 'unknown' if e.code == 0 else 'failed'
                    if e.code == 429:
                        pause = now + max(1, e.retry_after)
                        self.store.set_meta('outbound_pause', pause)
                        with self.store.db:
                            self.store.db.execute("UPDATE campaign_deliveries SET state='pending',next_attempt=? WHERE campaign=? AND user_id=?", (pause, job['campaign'], job['user_id']))
                        break
                    if e.code == 403:
                        with self.store.db:
                            self.store.db.execute('UPDATE users SET can_message=0 WHERE id=?', (job['user_id'],))
            with self.store.db:
                self.store.db.execute('UPDATE campaign_deliveries SET state=? WHERE campaign=? AND user_id=?', (state, job['campaign'], job['user_id']))
            time.sleep(0.06)
        with self.store.db:
            self.store.db.execute("UPDATE campaigns SET status='finished' WHERE status='queued' AND NOT EXISTS(SELECT 1 FROM campaign_deliveries d WHERE d.campaign=campaigns.id AND d.state='pending')")
