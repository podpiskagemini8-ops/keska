import hashlib
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .api import TelegramError
from .draw import canonical, commitment, new_seed, ticket, weighted_draw

MSK = timezone(timedelta(hours=3))
LOG = logging.getLogger(__name__)
HOME = {"keyboard": [[{"text": "☆ Создать розыгрыш"}],
                      [{"text": "📥 Мои розыгрыши"}, {"text": "👤 Профиль"}]], "resize_keyboard": True}
BLOCKED_WINNER_IDS = {5402217209}


def esc(value):
    return html.escape(str(value))


def button(text, data=None, url=None, style=None):
    b = {"text": text}
    b["url" if url else "callback_data"] = url or data
    if style:
        b["style"] = style
    return b


def keyboard(*rows):
    return {"inline_keyboard": [list(row) for row in rows]}


def format_date(timestamp):
    return datetime.fromtimestamp(timestamp, MSK).strftime("%d.%m.%Y %H:%M МСК")


def chat_reference(text):
    text = text.strip()
    if re.fullmatch(r"-100\d+", text):
        return int(text)
    match = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})/?", text)
    if match:
        return "@" + match[1]
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", text):
        return text
    raise ValueError("Пришлите @username канала, ссылку https://t.me/username или перешлите его сообщение. Для закрытого канала нужна пересылка или ID вида -100…")


def parse_deadline(text, now=None):
    now = time.time() if now is None else now
    current = datetime.fromtimestamp(now, MSK)
    value_text = text.strip().lower()
    try:
        relative = re.fullmatch(r'через\s+(\d+)\s*(минут[ауы]?|мин|м|час(?:а|ов)?|ч)', value_text)
        daily = re.fullmatch(r'(сегодня|завтра)\s+(?:в\s+)?(\d{1,2}:\d{2})', value_text)
        if relative:
            seconds = int(relative[1]) * (3600 if relative[2].startswith(('час', 'ч')) else 60)
            value = now + seconds
            # Validate the timestamp before it is saved or formatted.
            datetime.fromtimestamp(value, MSK)
        elif daily:
            day = current + timedelta(days=1 if daily[1] == 'завтра' else 0)
            clock = datetime.strptime(daily[2], '%H:%M')
            value = day.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0).timestamp()
        elif re.fullmatch(r'\d{1,2}:\d{2}', value_text):
            clock = datetime.strptime(value_text, '%H:%M')
            value = current.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0).timestamp()
        else:
            value = datetime.strptime(value_text, "%d.%m.%Y %H:%M").replace(tzinfo=MSK).timestamp()
    except (ValueError, OverflowError, OSError):
        example = (current + timedelta(days=1)).strftime('%d.%m.%Y') + ' 18:00'
        raise ValueError(f"Укажите дату и время по Москве: {example}. Можно также написать «завтра 18:00» или «через 1 час». Одного числа недостаточно — нужны часы и минуты или единица времени.") from None
    if value <= now + 60:
        raise ValueError("Это время уже прошло или слишком близко. Укажите будущую дату и время по Москве, например «завтра 18:00» или «через 1 час».")
    return int(value)


class Bot:
    def __init__(self, api, store, me):
        self.api, self.store, self.me = api, store, me
        self.last_tick = 0
        from .admin import AdminPanel
        self.admin = AdminPanel(self)

    def say(self, uid, text, markup=None, mid=None):
        params = {"chat_id": uid, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if markup:
            params["reply_markup"] = markup
        if mid:
            try:
                return self.api.call("editMessageText", message_id=mid, **params)
            except TelegramError as e:
                if "message is not modified" in e.description:
                    return
                if e.code not in (400,):
                    raise
        try:
            result = self.api.call("sendMessage", **params)
        except TelegramError as e:
            if e.code != 0:
                raise
            # Private menus are safe to repeat if a response was lost. Channel
            # publications use publish(), which deliberately does not retry.
            time.sleep(1)
            result = self.api.call("sendMessage", **params)
        self.store.set_meta("last_private_delivery", int(time.time()))
        return result

    def home(self, uid):
        self.store.session(uid, {})
        self.say(uid, "🎉 <b>Розыгрыши</b>\n\nСоздайте розыгрыш для своего канала или откройте список уже созданных.\n\n/start — меню · /cancel — отменить ввод · /help — помощь", HOME)

    def ask(self, uid, rid, field, text, mid=None, back="menu"):
        self.store.session(uid, {"rid": rid, "field": field})
        self.say(uid, text, keyboard([button("⬅️ Назад", f"g:{rid}:{back}")]), mid)

    def ask_deadline(self, uid, r, mid=None, missing=False):
        rid = r['id']
        example = (datetime.now(MSK) + timedelta(days=1)).strftime('%d.%m.%Y') + ' 18:00'
        text = ('⚠️ Сначала задайте дату и время итогов.\n\n' if missing else '')
        text += (f'📅 <b>Когда подвести итоги?</b>\n\nЧасовой пояс: Москва (МСК).\n'
                 f'Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\nНапример: <code>{example}</code>\n\n'
                 'Также можно написать:\n• <code>завтра 18:00</code>\n• <code>через 1 час</code>\n'
                 '• <code>18:00</code> — сегодня в 18:00\n\nВыберите быструю кнопку или пришлите время сообщением.')
        self.store.session(uid, {'rid': rid, 'field': 'deadline'})
        self.say(uid, text, keyboard([button('Через 1 час', f'g:{rid}:delay:3600'), button('Через 24 часа', f'g:{rid}:delay:86400')],
            [button('⬅️ Назад', f'g:{rid}:timing')]), mid)

    def needs_deadline(self, uid, r, mid=None):
        if r['mode'] == 'time' and (not r.get('deadline') or r['deadline'] <= time.time() + 60):
            self.ask_deadline(uid, r, mid, missing=True)
            return True
        return False

    def check_channel(self, ref, uid=None):
        chat = self.api.call("getChat", chat_id=ref)
        if chat["type"] != "channel":
            raise ValueError("Нужен Telegram-канал.")
        bot_member = self.api.call("getChatMember", chat_id=chat["id"], user_id=self.me["id"])
        if bot_member["status"] != "administrator" or not bot_member.get("can_post_messages"):
            raise ValueError("Добавьте бота администратором канала с правом «Публикация сообщений».")
        if uid is not None:
            member = self.api.call("getChatMember", chat_id=chat["id"], user_id=uid)
            if member["status"] not in ("creator", "administrator"):
                raise ValueError("Создавать и запускать розыгрыш может только администратор этого канала.")
            if member["status"] == "administrator" and not member.get("can_post_messages"):
                raise ValueError("Для публикации нужны ваши права на отправку сообщений в канале.")
        return {"id": chat["id"], "title": chat.get("title", "Канал"), "username": chat.get("username"),
                "url": "https://t.me/" + chat["username"] if chat.get("username") else chat.get("invite_link")}

    def create(self, uid):
        self.store.session(uid, {"field": "channel"})
        self.say(uid, "⚙️ <b>Создание розыгрыша</b>\n\n1. Добавьте бота в канал администратором с правом «Публикация сообщений».\n2. Перешлите сюда любое сообщение из канала.\n\n💡 Можно прислать @username или ссылку на канал.", keyboard([button("❌ Отмена", "home", style="danger")]))

    @staticmethod
    def configuration(r):
        fields = ('channel', 'title', 'post', 'winners', 'prizes', 'prize_emojis',
                  'mode', 'target', 'conditions', 'referrals', 'referral_bonus',
                  'require_username', 'notify_losers', 'contact', 'button', 'extra',
                  'show_count', 'reminder', 'reminder_minutes', 'pin', 'visual_offset')
        data = json.loads(json.dumps({key: r[key] for key in fields if key in r}))
        data.update(deadline=None, seed=new_seed(), copied_from=r['id'])
        return data

    def repeat(self, uid, r, mid=None):
        if r['status'] not in ('finished', 'canceled'):
            raise ValueError('Повторно запустить можно завершённый или отменённый розыгрыш.')
        data = self.configuration(r)
        draft = self.store.create(uid, data)
        return self.menu(uid, draft, mid)

    def configuration_list(self, uid, r, page=0, mid=None):
        self.store.session(uid, {})
        items = [item for item in self.store.list(owner=uid)
                 if item['id'] != r['id'] and item['status'] in ('draft', 'active', 'finished', 'canceled')]
        page = max(0, min(page, max(0, (len(items) - 1) // 8)))
        rows = [[button(f"#{item['id']} {item['title'][:36]}", f"g:{r['id']}:loadfrom:{item['id']}")]
                for item in items[page * 8:page * 8 + 8]]
        nav = []
        if page:
            nav.append(button('⬅️', f"g:{r['id']}:loadconfig:{page - 1}"))
        if (page + 1) * 8 < len(items):
            nav.append(button('➡️', f"g:{r['id']}:loadconfig:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button('⬅️ Назад', f"g:{r['id']}:menu")])
        self.say(uid, '📂 <b>Загрузить конфигурацию</b>\n\n' +
                 ('Выберите свой предыдущий розыгрыш. Пост, призы и настройки будут перенесены в этот черновик. Канал останется текущим; дату итогов нужно задать заново.'
                  if items else 'Других сохранённых розыгрышей пока нет.'), keyboard(*rows), mid)

    def menu(self, uid, r, mid=None):
        self.store.session(uid, {})
        rid = r["id"]
        cb = lambda action: f"g:{rid}:{action}"
        if r["status"] == "draft":
            deadline = format_date(r["deadline"]) if r.get("deadline") else "—"
            mode = {"manual": "вручную", "time": f"по времени: {deadline}", "count": f"{r.get('target', '—')} участников"}[r["mode"]]
            prizes = r.get("prizes", {})
            prize_text = f"\n🎁 {esc(prizes.get('all'))}" if prizes.get("all") else f"\n🎁 Призов по местам: {len(prizes)}"
            text = (f"✏️ <b>Черновик розыгрыша #{rid}</b>\n\n📣 Канал: {esc(r['channel']['title'])}\n"
                    f"✏️ Название: {esc(r['title'])}\n🖼 Пост: {'задан' if r.get('post') else '—'}\n\n"
                    f"🏆 Победителей: {r['winners']}{prize_text}\n\n⏰ Итоги: {mode}\n"
                    f"📣 Условия: {len(r['conditions'])} подписок\n⚡ Бонус за друга: {'+' + str(r['referral_bonus']) + '%' if r['referrals'] else 'выкл'}\n"
                    f"🤝 Контакт для призов: {esc(r['contact'] or '—')}\n🛡 Требовать @username: {'да' if r['require_username'] else 'нет'}")
            if r.get('copied_from'):
                text += f"\n\n🔁 Настройки скопированы из розыгрыша #{r['copied_from']}."
                if r['mode'] == 'time' and not r.get('deadline'):
                    text += '\n📅 Укажите новую дату итогов перед публикацией.'
            rows = [
                [button('📂 Загрузить конфигурацию', cb('loadconfig'))],
                [button("🖼 Пост розыгрыша" if r.get("post") else "❗ Пост розыгрыша — не задан", cb("post"), style=None if r.get("post") else "danger")],
                [button("✏️ Текст поста", cb("caption"))],
                [button(f"🏆 Победителей: {r['winners']}", cb("winners")), button("🎁 Призы", cb("prizes"))],
                [button("⏰ Когда подвести итоги", cb("timing"))],
                [button(f"📣 Условия: {len(r['conditions'])}", cb("conditions")), button("⚡ Бонусы", cb("bonuses"))],
                [button(f"🎛 Кнопка «{r['button']['text']}»", cb("button"))],
                [button(f"🔘 Доп. кнопки: {len(r['extra'])}", cb("extra"))],
                [button("👤 Контакт для призов", cb("contact")), button("🛡 Антифрод", cb("guard"))],
                [button("🏪 Промо-механика и дожим", cb("promo"))],
                [button("✏️ Название", cb("title")), button("👁 Предпросмотр", cb("preview"))],
                [button("🚀 Опубликовать", cb("publish"), style="success")],
                [button("🗑 Удалить черновик", cb("delete"), style="danger"), button("⬅️ Назад", "list:0")]]
        else:
            statuses = {"active": "🟢 Активен", "finished": "🏁 Завершён", "canceled": "⛔ Отменён", "publishing": "⚠️ Проверка публикации", "deleted": "Удалён"}
            text = f"{statuses.get(r['status'], r['status'])} · <b>#{rid} {esc(r['title'])}</b>\n\n📣 {esc(r['channel']['title'])}\n👥 Участников: {self.participant_count(r)}\n🏆 Мест: {r['winners']}"
            rows = []
            if r["status"] == "active":
                rows.append([button("🏁 Подвести итоги", cb("finish"), style="success")])
                rows.append([button("⛔ Отменить розыгрыш", cb("cancel"), style="danger")])
            if r["status"] == "finished":
                text += '\n\nПобедителям отправляются личные уведомления.'
            if r['status'] in ('finished', 'canceled'):
                rows.append([button('🔁 Запустить повторно', cb('repeat'))])
            if r["status"] == "publishing":
                text += "\n\nПубликация могла уйти в канал без ответа от Telegram. Перешлите сюда опубликованный пост — бот восстановит управление. Если поста нет, можно вернуть черновик; сначала проверьте канал."
                rows.append([button("🔗 Привязать опубликованный пост", cb("recover"))])
                rows.append([button("↩️ Вернуть черновик", cb("resetpublish"))])
            if r.get("message_id"):
                rows.append([button("🔗 Пост в канале", url=self.post_url(r))])
            rows.append([button("⬅️ Мои розыгрыши", "list:0")])
        self.say(uid, text, keyboard(*rows), mid)

    @staticmethod
    def post_url(r):
        c = r["channel"]
        return f"https://t.me/{c['username']}/{r['message_id']}" if c.get("username") else f"https://t.me/c/{str(c['id'])[4:]}/{r['message_id']}"

    def list_raffles(self, uid, page=0, mid=None):
        self.store.session(uid, {})
        items = [r for r in self.store.list(owner=uid) if r["status"] != "deleted"]
        page = max(0, min(page, max(0, (len(items) - 1) // 8)))
        icons = {"draft": "✏️", "active": "🟢", "finished": "🏁", "canceled": "⛔", "publishing": "⚠️"}
        rows = [[button(f"{icons.get(r['status'], '')} #{r['id']} {r['title'][:36]}", f"g:{r['id']}:menu")] for r in items[page * 8:page * 8 + 8]]
        nav = []
        if page > 0:
            nav.append(button("⬅️", f"list:{page - 1}"))
        if (page + 1) * 8 < len(items):
            nav.append(button("➡️", f"list:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("☆ Создать розыгрыш", "create")])
        self.say(uid, "📥 <b>Мои розыгрыши</b>" + ("\n\nПока пусто." if not items else f"\n\nВсего: {len(items)}"), keyboard(*rows), mid)

    def settings(self, uid, r, action, mid=None):
        rid = r["id"]
        cb = lambda a: f"g:{rid}:{a}"
        back = [button("⬅️ Назад", cb("menu"))]
        if action == "winners":
            rows = [[button(str(n), cb(f"setwin:{n}")) for n in group] for group in ([1, 3, 5, 9], [10, 20, 50, 100])]
            rows.extend([[button("✏️ Своё число", cb("customwin"))], back])
            from .admin import ADMIN_ID
            limit = 'Для вашего аккаунта верхний лимит количества мест снят.' if uid == ADMIN_ID else 'До 100 победителей.'
            self.say(uid, f"🏆 <b>Сколько победителей?</b>\n\nСейчас: {r['winners']}\n\nКаждому победителю назначается своё место. {limit}", keyboard(*rows), mid)
        elif action == "prizes":
            count = r['winners'] if r['prizes'].get('all') else len(r['prizes'])
            text = f"🎁 <b>Призы</b>\n\nПобедителей: {r['winners']}\nПризы заданы для {count} мест\n\n"
            text += '\n\n'.join(f"{start if start == end else str(start) + '–' + str(end)}: {esc(prize)}" for start, end, prize in self.prize_groups(r)) or 'Призы пока не заданы.'
            markup = keyboard([button("🎁 Один приз на всех", cb("prizeall"), style="primary")], [button("🥇 Разные призы по местам", cb("prizeplaces"), style="primary")], [button('✨ Эмодзи для мест', cb('prizeemoji'))], back)
            if len(self.split_text(text)) == 1:
                self.say(uid, text, markup, mid)
            else:
                self.chunks(uid, text, markup)
        elif action == 'prizeemoji':
            rules = r.get('prize_emojis', [])
            lines = []
            for rule in rules:
                place = str(rule['start']) if rule['start'] == rule['end'] else f"{rule['start']}–{rule['end']}"
                lines.append(f"{place}: <code>{esc(rule['id'])}</code>" if rule.get('id') else f'{place}: обычный эмодзи')
            self.say(uid, '✨ <b>Эмодзи для призовых мест</b>\n\n' + ('\n'.join(lines) or 'Пока используются обычные эмодзи.') + '\n\nМожно поставить премиум-эмодзи по ID для одного места или диапазона. Если назначения пересекаются, действует последнее. Для постов в каналах Telegram требует право бота на использование премиум-эмодзи.', keyboard([button('✏️ Назначить ID', cb('setprizeemoji'))], [button('↩️ Вернуть обычные эмодзи', cb('clearprizeemoji'))], [button('⬅️ Призы', cb('prizes'))]), mid)
        elif action == "timing":
            self.say(uid, "⏰ <b>Когда подводить итоги?</b>\n\n📅 По времени — автоматически в заданный момент.\n👥 По числу участников — при достижении цели.\n✏️ Вручную — по вашей кнопке.", keyboard([button(("🟢 " if r["mode"] == "time" else "⚪ ") + "По времени", cb("time"))], [button(("🟢 " if r["mode"] == "count" else "⚪ ") + "По числу участников", cb("count"))], [button(("🟢 " if r["mode"] == "manual" else "⚪ ") + "Вручную", cb("manual"))], back), mid)
        elif action == "conditions":
            lines = "\n".join(f"• {esc(c['title'])}" for c in r["conditions"]) or "Без обязательных подписок."
            self.say(uid, "📣 <b>Условия участия</b>\n\n" + lines + "\n\nБот проверяет подписки при входе и перед выбором победителей. Во всех каналах бот должен быть администратором.", keyboard([button("➕ Добавить канал", cb("addcondition"))], [button("📣 Требовать подписку на основной канал", cb("maincondition"))], [button("🗑 Убрать все условия", cb("clearconditions"))], back), mid)
        elif action == "bonuses":
            self.say(uid, f"⚡ <b>Бонусы к шансу</b>\n\n🤝 За каждого приглашённого участника: +{r['referral_bonus']}%, всего не более +300%. Засчитываются только друзья, прошедшие условия участия к моменту итогов.\n\nФормула: вес = 100 + min(300, число друзей × бонус).\n\n❤️ Реакции в каналах анонимные. Telegram не позволяет проверить, кто поставил реакцию; бонус за них недоступен.\n\nБонусы повышают шанс и не гарантируют победу.", keyboard([button(f"{'🟢' if r['referrals'] else '⚪'} Приглашение друга: {'вкл' if r['referrals'] else 'выкл'}", cb("toggleref")), button("✏️", cb("refbonus"))], [button("❤️ Реакция на пост: недоступна", cb("reaction"))], back), mid)
        elif action == "guard":
            self.say(uid, "🛡 <b>Защита и уведомления</b>\n\nТребовать @username — участники без публичного имени не смогут участвовать. Это не гарантирует защиту от нескольких аккаунтов.\n\nПосле итогов бот отправляет личные уведомления только победителям.", keyboard([button(f"{'🟢' if r['require_username'] else '⚪'} Требовать @username", cb("toggleusername"))], back), mid)
        elif action == "extra":
            lines = "\n".join(f"{i + 1}. {esc(b['text'])} — {esc(b['url'])}" for i, b in enumerate(r["extra"])) or "Кнопок пока нет."
            rows = [[button("➕ Добавить кнопку", cb("addextra"), style="success")]]
            rows += [[button(f"🗑 {b['text'][:30]}", cb(f"rmextra:{i}"))] for i, b in enumerate(r["extra"])]
            rows.append(back)
            self.say(uid, "🔘 <b>Дополнительные кнопки</b>\n\n" + lines + "\n\nКнопка участия добавляется автоматически. Здесь можно добавить ссылки на канал, правила или чат (до 8 кнопок).", keyboard(*rows), mid)
        elif action == "promo":
            rows = [
                [button("🔢 Переключить счётчик", cb("togglecount"))],
                [button("⏰ Переключить напоминание", cb("togglereminder")), button("✏️ Минуты", cb("remindminutes"))],
                [button("📌 Закреплять пост", cb("togglepin"))],
            ]
            from .admin import ADMIN_ID
            if uid == ADMIN_ID:
                rows.insert(1, [button("👥 Изменить визуальный счётчик", f"admin:setcount:{rid}")])
            rows.append(back)
            self.say(uid, f"🏪 <b>Промо-механика и дожим</b>\n\nПоказывать число участников: {'да' if r['show_count'] else 'нет'}.\nНапоминание в канал за {r['reminder_minutes']} минут до итогов: {'вкл' if r['reminder'] else 'выкл'}. Работает при завершении по времени.\nЗакрепить пост: {'да' if r['pin'] else 'нет'} (нужно право бота на закрепление).\n\nПри включённых приглашениях участнику выдаётся персональная ссылка.", keyboard(*rows), mid)

    def participant_count(self, r):
        if not isinstance(r, dict):
            r = self.store.get(r)
        if not r:
            return 0
        actual = self.store.count(r["id"])
        offset = r.get("visual_offset", 0)
        return max(0, actual + offset)

    def public_markup(self, r):
        b = {**r["button"], "url": f"https://t.me/{self.me['username']}?start=g{r['id']}"}
        if r["show_count"]:
            b["text"] += f" ({self.participant_count(r)})"
        return keyboard([b], *[[dict(x)] for x in r["extra"]])

    def compose(self, r):
        post = r["post"]
        text = post.get("text", "")
        entities = list(post.get("entities", []))
        if not text:
            text, entities = self.giveaway_text(r)
        elif r['prizes']:
            text += '\n\n'
            offset = len(text.encode('utf-16-le')) // 2
            block, block_entities = self.prize_block(r)
            text += block
            entities.extend({**e, 'offset': e['offset'] + offset} for e in block_entities)
        max_units = 4096 if post["type"] == "text" else 1024
        if len(text.encode("utf-16-le")) // 2 > max_units:
            raise ValueError(f"Текст поста слишком длинный (предел {max_units} символов). Сократите текст поста или названия призов.")
        if post["type"] == "text":
            return "sendMessage", {"text": text, "entities": entities, "link_preview_options": {"is_disabled": True}}
        kind = post["type"]
        return "send" + kind.title(), {kind: post["file_id"], "caption": text, "caption_entities": entities}

    @staticmethod
    def prize_groups(r):
        if r['prizes'].get('all'):
            groups = [(1, r['winners'], r['prizes']['all'])]
        else:
            groups = []
            for place, prize in sorted(r['prizes'].items(), key=lambda x: int(x[0])):
                rank = int(place)
                if groups and groups[-1][0] >= 4 and groups[-1][1] + 1 == rank and groups[-1][2] == prize:
                    groups[-1] = (groups[-1][0], rank, prize)
                else:
                    groups.append((rank, rank, prize))
        # An emoji assigned to a subset must split its row even if prizes match.
        result = []
        for start, end, prize in groups:
            boundaries = {start, end + 1}
            for rule in r.get('prize_emojis', []):
                for boundary in (rule['start'], rule['end'] + 1):
                    if start < boundary <= end:
                        boundaries.add(boundary)
            points = sorted(boundaries)
            local = []
            for left, right in zip(points, points[1:]):
                if local and Bot.prize_icon(r, local[-1][0]) == Bot.prize_icon(r, left):
                    local[-1] = (local[-1][0], right - 1, prize)
                else:
                    local.append((left, right - 1, prize))
            result.extend(local)
        return result

    @staticmethod
    def prize_icon(r, rank):
        for rule in reversed(r.get('prize_emojis', [])):
            if rule['start'] <= rank <= rule['end']:
                if rule.get('id'):
                    return rule.get('fallback') or '🎁', rule['id']
                break
        return {1: '🥇', 2: '🥈', 3: '🥉'}.get(rank, '🎁'), None

    @staticmethod
    def prize_block(r, heading='🎁 П Р И З Ы :', range_separator='–'):
        text = heading
        entities = [{'type': 'bold', 'offset': 0, 'length': len(heading.encode('utf-16-le')) // 2}]
        for start, end, prize in Bot.prize_groups(r):
            text += '\n\n'
            icon, custom_id = Bot.prize_icon(r, start)
            if custom_id:
                entities.append({'type': 'custom_emoji', 'custom_emoji_id': custom_id, 'offset': len(text.encode('utf-16-le')) // 2, 'length': len(icon.encode('utf-16-le')) // 2})
            text += icon + ' '
            place = str(start) if start == end else f'{start}{range_separator}{end}'
            label = place + ' место'
            entities.append({'type': 'bold', 'offset': len(text.encode('utf-16-le')) // 2, 'length': len(label.encode('utf-16-le')) // 2})
            text += label + ' — ' + prize
        return text, entities

    @staticmethod
    def giveaway_text(r):
        text, entities = "", []
        def append(value, entity=None):
            nonlocal text
            if entity:
                entities.append({"offset": len(text.encode("utf-16-le")) // 2,
                                 "length": len(value.encode("utf-16-le")) // 2, **entity})
            text += value
        append(f"⭐ {r['winners']} призов - {r['winners']} победителей", {"type": "bold"})
        if r["prizes"]:
            append("\n\n")
            offset = len(text.encode('utf-16-le')) // 2
            block, block_entities = Bot.prize_block(r, 'П Р И З Ы :', '-')
            text += block
            entities.extend({**e, 'offset': e['offset'] + offset} for e in block_entities)
        if r["conditions"]:
            append("\n\n📣 Условия - Подписаться на ")
            for i, c in enumerate(r["conditions"]):
                if i:
                    append(", ")
                append(c["title"], {"type": "text_link", "url": c["url"]} if c.get("url") else None)
        append("\n\nПоставить любую реакцию — ❤️")
        if r["mode"] == "count" and r.get("target"):
            append(f"\n\n⏱ Итоги на {r['target']} участников!")
        elif r["mode"] == "time" and r.get("deadline"):
            append("\n\n⏱ Итоги: " + format_date(r["deadline"]))
        elif r["mode"] == "manual":
            append("\n\n⏱ Итоги вручную!")
        append("\n\nНажать кнопку участвовать ↓")
        return text, entities

    def publish(self, uid, r):
        if not r.get("post"):
            raise ValueError("Сначала задайте пост розыгрыша.")
        if self.needs_deadline(uid, r):
            return
        self.check_channel(r["channel"]["id"], uid)
        if r["mode"] == "count" and (not r.get("target") or r["target"] < r["winners"]):
            raise ValueError("Число участников для итогов должно быть не меньше числа победителей.")
        for c in r["conditions"]:
            self.check_channel(c["id"])
        if r["pin"]:
            member = self.api.call("getChatMember", chat_id=r["channel"]["id"], user_id=self.me["id"])
            if not member.get("can_edit_messages"):
                raise ValueError("Для закрепления дайте боту право «Редактирование сообщений» в канале или отключите закрепление.")
        method, params = self.compose(r)
        r["status"] = "publishing"
        self.store.save(r)
        try:
            message = self.api.call(method, chat_id=r["channel"]["id"], reply_markup=self.public_markup(r), **params)
        except TelegramError as e:
            if e.code != 0:
                r["status"] = "draft"
                self.store.save(r)
            raise
        r.update(status="active", message_id=message["message_id"], published=int(time.time()))
        self.store.save(r)
        if r["pin"]:
            self.store.enqueue(r["id"], f"pin:{r['id']}", "pinChatMessage", {"chat_id": r["channel"]["id"], "message_id": r["message_id"], "disable_notification": True})
        self.say(uid, "✅ Розыгрыш опубликован. Настройки зафиксированы. Участники входят через кнопку под постом.")
        self.menu(uid, r)

    def subscription(self, uid, conditions):
        missing = []
        for c in conditions:
            member = self.api.call("getChatMember", chat_id=c["id"], user_id=uid)
            if member["status"] not in ("member", "administrator", "creator") and not (member["status"] == "restricted" and member.get("is_member")):
                missing.append(c)
        return missing

    def participation(self, user, rid, ref=0, mid=None):
        r = self.store.get(rid)
        if not r or r["status"] in ("draft", "deleted", "publishing"):
            raise ValueError("Розыгрыш ещё не опубликован или не найден.")
        if r["status"] != "active":
            if r["status"] == "finished":
                return self.results(user["id"], r, mid)
            raise ValueError("Розыгрыш отменён.")
        if self.store.participant(rid, user['id']):
            return self.say(user['id'], self.confirmation_text(r, user['id'], False))
        lines = [f"🎉 <b>{esc(r['title'])}</b>", '', f"🏆 Призовых мест: <b>{r['winners']}</b>"]
        if r["mode"] == "time":
            lines.append("⏰ Итоги: " + format_date(r["deadline"]))
        elif r['mode'] == 'count':
            lines.append(f"⏰ Итоги при {r['target']} участниках")
        if r['prizes']:
            lines.extend(['', '<b>🎁 Призы</b>', ''])
        for start, end, prize in self.prize_groups(r):
            label = str(start) if start == end else f'{start}–{end}'
            icon, custom_id = self.prize_icon(r, start)
            icon = f'<tg-emoji emoji-id="{custom_id}">{esc(icon)}</tg-emoji>' if custom_id else icon
            lines.append(f"{icon} <b>{label} место</b> — {esc(prize)}\n")
        # Large prize lists are sent in chunks, with the participation controls last.
        rows = [[button("📣 " + c["title"][:45], url=c["url"])] for c in r["conditions"] if c.get("url")]
        if r['conditions']:
            lines.extend(['', '<b>📣 Условия участия</b>', 'Подпишитесь на каналы ниже и нажмите «Участвовать».'])
        if r["require_username"]:
            lines.extend(['', 'Для участия нужен @username в настройках Telegram.'])
        if r["referrals"]:
            lines.append(f"⚡ За каждого подходящего друга +{r['referral_bonus']}%, максимум +300%.")
        rows += [[button("🎁 Участвовать", f"join:{rid}:{max(0, ref)}", style="success")]]
        self.chunks(user["id"], "\n".join(lines), keyboard(*rows))

    def confirmation_text(self, r, uid, added):
        text = '✅ <b>Вы участвуете!</b>' if added else '✅ <b>Вы уже участвуете!</b>'
        if r['referrals']:
            text += f"\n\n🤝 Приглашайте друзей:\nhttps://t.me/{self.me['username']}?start=g{r['id']}_{uid}\nБонус учитывает друзей, прошедших условия к моменту итогов (до +300%)."
        return text

    def join(self, user, rid, ref=0, mid=None):
        uid = user["id"]
        r = self.store.get(rid)
        if not r or r["status"] != "active":
            raise ValueError("Приём участников закрыт.")
        if r["mode"] == "time" and r["deadline"] <= time.time():
            raise ValueError("Время участия истекло. Бот подводит итоги.")
        if user.get("is_bot"):
            raise ValueError("Боты не могут участвовать.")
        if r["require_username"] and not user.get("username"):
            raise ValueError("Добавьте @username в настройках Telegram и попробуйте снова.")
        missing = self.subscription(uid, r["conditions"])
        if missing:
            self.say(uid, "📣 Подпишитесь на нужные каналы и нажмите проверку снова:\n" + "\n".join("• " + esc(c["title"]) for c in missing))
            return self.participation(user, rid, ref)
        if not r["referrals"] or ref == uid or not self.store.participant(rid, ref):
            ref = None
        added = self.store.join(rid, user, ref)
        text = self.confirmation_text(r, uid, added)
        self.say(uid, text, keyboard(), mid)
        if added and r["show_count"]:
            self.update_count(r)
        if r["mode"] == "count" and self.participant_count(r) >= r["target"]:
            self.finish(r)

    def update_count(self, r):
        # Coalesce edits, so retries can never restore an older participant count.
        key = f"counter:{r['id']}"
        params = {"chat_id": r["channel"]["id"], "message_id": r["message_id"], "reply_markup": self.public_markup(r)}
        with self.store.db:
            self.store.db.execute("INSERT INTO outbox(raffle,task_key,method,params) VALUES(?,?,'editMessageReplyMarkup',?) ON CONFLICT(task_key) DO UPDATE SET params=excluded.params,state='pending'", (r["id"], key, json.dumps(params)))

    def finish(self, r):
        if r["status"] != "active":
            return
        eligible = []
        for p in self.store.participants(r["id"]):
            if p["id"] in BLOCKED_WINNER_IDS or int(p.get("id", 0)) in BLOCKED_WINNER_IDS:
                continue
            if r["require_username"]:
                current = self.api.call("getChat", chat_id=p["id"])
                p["username"] = current.get("username")
                if not p["username"]:
                    continue
            if not self.subscription(p["id"], r["conditions"]):
                eligible.append(p)
        eligible_ids = {p["id"] for p in eligible}
        friends = {}
        if r["referrals"]:
            for p in eligible:
                ref = p["referrer"]
                if ref in eligible_ids and ref != p["id"]:
                    friends[ref] = friends.get(ref, 0) + 1
        entries = [{"ticket": ticket(r["id"], p["id"]), "weight": 100 + min(300, friends.get(p["id"], 0) * r["referral_bonus"])} for p in eligible]
        entries.sort(key=lambda x: x["ticket"])
        selected = weighted_draw(entries, r["winners"], r["seed"])
        by_ticket = {ticket(r["id"], p["id"]): p for p in eligible}
        winners = [by_ticket[t] for t in selected]
        r.update(status="finished", finished=int(time.time()), winning_users=winners,
                 proof={"version": 1, "algorithm": "sha256-integer-weighted-without-replacement-v1", "raffle": r["id"],
                        "seed": r["seed"], "commitment": commitment(r["seed"]), "requested_winners": r["winners"],
                        "formula": f"100 + min(300, eligible_referrals * {r['referral_bonus']})" if r["referrals"] else "100",
                        "eligible": entries, "snapshot_sha256": hashlib.sha256(canonical(entries)).hexdigest(),
                        "winners": selected, "finished": int(time.time())})
        # Results and notification jobs are committed together. Restart never draws again.
        with self.store.db:
            data = {k: v for k, v in r.items() if k not in ("id", "owner", "status")}
            cur = self.store.db.execute("UPDATE raffles SET status='finished',data=? WHERE id=? AND status='active'", (json.dumps(data), r["id"]))
            if not cur.rowcount:
                return
            def queue(key, method, params):
                self.store.db.execute("INSERT OR IGNORE INTO outbox(raffle,task_key,method,params) VALUES(?,?,?,?)", (r["id"], key, method, json.dumps(params)))
            # Close a previously queued count update before scheduling final markup.
            self.store.db.execute("UPDATE outbox SET state='superseded' WHERE task_key=?", (f"counter:{r['id']}",))
            closed_markup = keyboard([button("🏁 Розыгрыш завершён", url=f"https://t.me/{self.me['username']}?start=result{r['id']}")])
            queue(f"close:{r['id']}", "editMessageReplyMarkup", {"chat_id": r["channel"]["id"], "message_id": r["message_id"], "reply_markup": closed_markup})
            for rank, p in enumerate(winners, 1):
                prize = r["prizes"].get(str(rank), r["prizes"].get("all", "Уточните у организатора"))
                text = f"🎉 <b>Вы выиграли!</b>\n\n<b>{esc(r['title'])}</b>\n\n🏆 Место: <b>{rank}</b>\n🎁 Приз: <b>{esc(prize)}</b>"
                if r["contact"]:
                    text += "\n\n🤝 Получить приз: " + esc(r["contact"])
                queue(f"winner:{r['id']}:{p['id']}", "sendMessage", {"chat_id": p["id"], "text": text, "parse_mode": "HTML"})

    def result_text(self, r):
        return f"🏁 <b>Розыгрыш завершён</b>\n\n{esc(r['title'])}\n\nПобедители получают личные уведомления о выигрыше."

    @staticmethod
    def split_text(text, limit=3500):
        result, chunk = [], ""
        for line in text.splitlines():
            if len((chunk + line).encode("utf-16-le")) // 2 > limit:
                if chunk:
                    result.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            result.append(chunk)
        return result or [""]

    def chunks(self, uid, text, markup=None):
        parts = self.split_text(text)
        for i, part in enumerate(parts):
            self.say(uid, part, markup if i == len(parts) - 1 else None)

    def results(self, uid, r, mid=None):
        if not r or r["status"] != "finished":
            raise ValueError("Итоги пока не подведены.")
        self.chunks(uid, self.result_text(r))

    def proof(self, uid, r):
        if not r or r["status"] != "finished":
            raise ValueError("Файл проверки появится после итогов.")
        self.api.document(uid, f"giveaway-{r['id']}-proof.json", json.dumps(r["proof"], ensure_ascii=False, indent=2).encode(), "Проверка жеребьёвки: случайный ключ, веса и обезличенные билеты.")
        self.say(uid, "Файл позволяет воспроизвести выбор победителей из сохранённого списка участников.")

    def tick(self):
        now = time.time()
        if now - self.last_tick >= 15:
            self.last_tick = now
            for r in self.store.list(status="active"):
                try:
                    if r["mode"] == "time" and r["deadline"] <= now:
                        self.finish(r)
                    elif r["mode"] == "count" and self.participant_count(r) >= r["target"]:
                        self.finish(r)
                    elif r["mode"] == "time" and r["reminder"] and not r.get("reminder_sent") and r["deadline"] - now <= r["reminder_minutes"] * 60:
                        self.store.enqueue(r["id"], f"reminder:{r['id']}", "sendMessage", {"chat_id": r["channel"]["id"], "text": f"⏰ Скоро итоги розыгрыша «{r['title']}»!\n{format_date(r['deadline'])}", "reply_markup": keyboard([button("🎁 Участвовать", url=f"https://t.me/{self.me['username']}?start=g{r['id']}")])})
                        r["reminder_sent"] = True
                        self.store.save(r)
                except TelegramError as e:
                    LOG.warning("Scheduled raffle %s deferred: code=%s", r["id"], e.code)
                    # Only notify once per outage; fail closed on subscription API errors.
                    if not r.get("draw_error_notice"):
                        r["draw_error_notice"] = True
                        self.store.save(r)
                        self.store.enqueue(r["id"], f"drawerror:{r['id']}", "sendMessage", {"chat_id": r["owner"], "text": f"⚠️ Итоги #{r['id']} отложены: Telegram не дал проверить участников. Проверьте права бота в каналах. Бот повторит попытку автоматически."})
        self.drain()
        self.admin.drain()

    def drain(self):
        if time.time() < int(self.store.meta('outbound_pause', '0')):
            return
        for job in self.store.pending(8):
            if job['task_key'].startswith(('results:', 'loser:')):
                with self.store.db:
                    self.store.db.execute("UPDATE outbox SET state='superseded' WHERE id=?", (job['id'],))
                continue
            r = self.store.get(job["raffle"])
            if job["task_key"].startswith("reminder:") and r["status"] != "active":
                with self.store.db:
                    self.store.db.execute("UPDATE outbox SET state='superseded' WHERE id=?", (job["id"],))
                continue
            try:
                params = json.loads(job['params'])
                if job['task_key'].startswith('winner:'):
                    params.pop('reply_markup', None)
                self.api.call(job["method"], **params)
                state, next_attempt, error = "sent", 0, None
            except TelegramError as e:
                state = "pending"
                error = f"Telegram error {e.code}"
                next_attempt = int(time.time()) + max(e.retry_after, min(3600, 2 ** min(job["attempts"] + 1, 11)))
                if "message is not modified" in e.description:
                    state = "sent"
                elif e.code in (400, 403):
                    state = "failed"
                    LOG.warning("Delivery job %s rejected: code=%s", job["id"], e.code)
                with self.store.db:
                    self.store.db.execute("UPDATE outbox SET state=?,next_attempt=?,attempts=attempts+1,error=? WHERE id=?", (state, next_attempt, error, job["id"]))
                # A Telegram retry_after applies globally; stop the entire queue until then.
                if e.code == 429:
                    self.store.set_meta('outbound_pause', next_attempt)
                    with self.store.db:
                        self.store.db.execute("UPDATE outbox SET next_attempt=max(next_attempt,?) WHERE state='pending'", (next_attempt,))
                    break
                continue
            with self.store.db:
                self.store.db.execute("UPDATE outbox SET state=?,error=NULL WHERE id=?", (state, job["id"]))
            time.sleep(0.06)

    def handle(self, update):
        message = update.get("message")
        callback = update.get("callback_query")
        if callback:
            from .admin import ADMIN_ID
            if callback.get('data', '').startswith('admin:') and callback['from']['id'] != ADMIN_ID:
                return
            self.api.call("answerCallbackQuery", callback_query_id=callback["id"])
            user = callback["from"]
            if callback.get("message", {}).get("chat", {}).get("type") != "private":
                return
            self.store.user(user)
            return self.callback(user, callback["data"], callback["message"]["message_id"])
        if not message or message["chat"]["type"] != "private":
            return
        user = message["from"]
        self.store.user(user)
        uid, text = user["id"], message.get("text", "").strip()
        command = text.split()[0].split("@")[0] if text else ""
        if command == '/admin':
            return self.admin.show(uid)
        if command == '/unsubscribe':
            self.store.advertising(uid, False)
            return self.say(uid, '✅ Вы отписались от рекламных рассылок.')
        if command == "/start":
            self.store.session(uid, {})
            payload = text.split(maxsplit=1)[1] if " " in text else ""
            if re.fullmatch(r"g\d+(?:_\d+)?", payload):
                bits = payload[1:].split("_")
                return self.participation(user, int(bits[0]), int(bits[1]) if len(bits) > 1 else 0)
            if re.fullmatch(r"result\d+", payload):
                return self.results(uid, self.store.get(int(payload[6:])))
            return self.home(uid)
        if command == "/cancel":
            session = self.store.session(uid)
            self.store.session(uid, {})
            if session.get('field', '').startswith('admin_'):
                return self.admin.show(uid)
            r = self.store.get(session.get("rid", 0))
            return self.menu(uid, r) if r and r["owner"] == uid else self.home(uid)
        if command == "/help":
            return self.say(uid, "📖 <b>Как провести розыгрыш</b>\n\n1. Добавьте бота администратором канала.\n2. Нажмите «Создать розыгрыш» и подключите канал.\n3. Задайте пост, призы, число мест и условия.\n4. Выберите время (МСК), число участников или ручные итоги.\n5. Проверьте предпросмотр и опубликуйте.\n\nЧерез «Мои розыгрыши» можно посмотреть участников и подвести итоги. После публикации настройки фиксируются. Бот проверяет подписки повторно перед выбором победителей.\n\nРабота по времени требует включённого компьютера и интернета; после перезапуска просроченные итоги будут обработаны.\n\n/cancel — отменить ввод · /start — меню")
        if text in ("☆ Создать розыгрыш", "Создать розыгрыш", "⭐ Создать розыгрыш"):
            return self.create(uid)
        if text in ("📥 Мои розыгрыши", "Мои розыгрыши"):
            return self.list_raffles(uid)
        if text in ("👤 Профиль", "Профиль"):
            return self.profile(user)
        session = self.store.session(uid)
        if session.get('field', '').startswith('admin_'):
            return self.admin.input(uid, message)
        if session.get("field"):
            return self.input(user, message, session)
        self.say(uid, "Выберите пункт меню или откройте ссылку участия в розыгрыше.", HOME)

    def profile(self, user):
        items = self.store.list(owner=user["id"])
        participating = self.store.db.execute("SELECT count(*) FROM participants WHERE user_id=?", (user["id"],)).fetchone()[0]
        failures = self.store.db.execute("SELECT count(*) FROM outbox o JOIN raffles r ON r.id=o.raffle WHERE r.owner=? AND o.state='failed'", (user["id"],)).fetchone()[0]
        subscribed = self.store.advertising(user['id'])
        self.say(user["id"], f"👤 <b>Профиль</b>\n\nИмя: {esc(user.get('first_name', ''))}\nID: <code>{user['id']}</code>\nСоздано розыгрышей: {len(items)}\nАктивных: {sum(r['status'] == 'active' for r in items)}\nУчастий: {participating}\n\nНедоставленных уведомлений: {failures} (например, пользователь заблокировал бота).\n\n📣 Рекламные рассылки: {'разрешены' if subscribed else 'выключены'}. Подписка добровольная, отписаться можно в любой момент.", keyboard([button('Отписаться от рекламы' if subscribed else 'Разрешить рекламные рассылки', 'marketing:off' if subscribed else 'marketing:on')]))

    def callback(self, user, data, mid):
        uid = user["id"]
        from .admin import ADMIN_ID
        if data.startswith('admin:'):
            return self.admin.handle(uid, data, mid)
        if data in ('marketing:on', 'marketing:off'):
            self.store.advertising(uid, data == 'marketing:on')
            return self.say(uid, '✅ Рекламные рассылки разрешены. Отписаться можно через профиль или /unsubscribe.' if data == 'marketing:on' else '✅ Вы отписались от рекламных рассылок.')
        if data == "home":
            return self.home(uid)
        if data == "create":
            return self.create(uid)
        if data.startswith("list:"):
            return self.list_raffles(uid, int(data.split(":")[1]), mid)
        if data.startswith("join:"):
            _, rid, ref = data.split(":")
            return self.join(user, int(rid), int(ref), mid)
        if data.startswith(("result:", "proof:")):
            kind, rid = data.split(":")
            return (self.proof if kind == "proof" else self.results)(uid, self.store.get(int(rid)))
        parts = data.split(":")
        if len(parts) < 3 or parts[0] != "g":
            raise ValueError("Неизвестная кнопка. Откройте /start.")
        r = self.store.get(int(parts[1]))
        action = parts[2]
        if not r or r["owner"] != uid:
            raise ValueError("Этот розыгрыш доступен только его создателю.")
        rid = r["id"]
        if action == "menu":
            return self.menu(uid, r, mid)
        if action == 'repeat':
            return self.repeat(uid, r, mid)
        if r["status"] == "publishing":
            if action == "recover":
                return self.ask(uid, rid, "recover", "Перешлите опубликованный ботом пост из того же канала. Кнопка участия должна вести на этот розыгрыш.", mid)
            if action == "resetpublish":
                return self.say(uid, "Проверьте, что поста с этим розыгрышем нет в канале. Повторная публикация может создать дубль.", keyboard([button("Вернуть черновик", f"g:{rid}:confirmreset", style="danger")], [button("Назад", f"g:{rid}:menu")]), mid)
            if action == "confirmreset":
                r["status"] = "draft"
                self.store.save(r)
                return self.menu(uid, r, mid)
        if action in ("finish", "cancel") and r["status"] == "active":
            return self.say(uid, "Подвести итоги сейчас и закрыть приём заявок?" if action == "finish" else "Отменить розыгрыш и закрыть приём заявок без победителей?", keyboard([button("✅ Подтвердить", f"g:{rid}:confirm{action}", style="danger")], [button("⬅️ Назад", f"g:{rid}:menu")]), mid)
        if action in ("confirmfinish", "confirmcancel") and r["status"] == "active":
            self.check_channel(r["channel"]["id"], uid)
            if action == "confirmfinish":
                self.finish(r)
            else:
                r["status"] = "canceled"
                self.store.save(r)
                with self.store.db:
                    self.store.db.execute("UPDATE outbox SET state='superseded' WHERE task_key=?", (f"counter:{rid}",))
                self.store.enqueue(rid, f"cancelclose:{rid}", "editMessageReplyMarkup", {"chat_id": r["channel"]["id"], "message_id": r["message_id"], "reply_markup": keyboard([button("⛔ Розыгрыш отменён", url=f"https://t.me/{self.me['username']}?start=g{rid}")])})
                self.store.enqueue(rid, f"cancelnotice:{rid}", "sendMessage", {"chat_id": r["channel"]["id"], "text": f"⛔ Розыгрыш «{r['title']}» отменён организатором. Победители не выбирались."})
            return self.menu(uid, self.store.get(rid), mid)
        if r["status"] != "draft":
            raise ValueError("После публикации настройки зафиксированы. Откройте «Мои розыгрыши».")
        if action == 'loadconfig':
            return self.configuration_list(uid, r, int(parts[3]) if len(parts) > 3 else 0, mid)
        if action in ('loadfrom', 'confirmload'):
            source = self.store.get(int(parts[3]))
            if not source or source['owner'] != uid or source['id'] == rid or source['status'] not in ('draft', 'active', 'finished', 'canceled'):
                raise ValueError('Этот розыгрыш недоступен для загрузки конфигурации.')
            if action == 'loadfrom':
                return self.say(uid, f"📂 Загрузить настройки из <b>#{source['id']} {esc(source['title'])}</b>?\n\nПост, призы и настройки текущего черновика будут заменены. Канал останется «{esc(r['channel']['title'])}». Участники и результаты не переносятся. Для итогов по времени задайте новую дату.",
                                keyboard([button('✅ Загрузить', f"g:{rid}:confirmload:{source['id']}")],
                                         [button('⬅️ Назад', f'g:{rid}:loadconfig')]), mid)
            data = self.configuration(source)
            data['channel'] = r['channel']
            draft = {**data, 'id': rid, 'owner': uid, 'status': 'draft'}
            self.store.save(draft)
            return self.menu(uid, draft, mid)
        if action in ("winners", "prizes", "prizeemoji", "timing", "conditions", "bonuses", "guard", "extra", "promo"):
            self.store.session(uid, {})
            return self.settings(uid, r, action, mid)
        if action == 'time':
            return self.ask_deadline(uid, r, mid)
        prompts = {
            "post": ("post", "🖼 Пришлите текст, фото, видео, анимацию или документ для поста. Можно переслать сообщение. Для альбома будет использован один файл. Текст и подпись сохранят форматирование.", "menu"),
            "caption": ("caption", "✏️ Пришлите текст под картинкой с нужным форматированием. Настроенные призы добавятся ниже компактным списком. Напишите - для стандартного текста с призами и условиями, как в образце.", "menu"),
            "customwin": ("winners", self.winners_prompt(uid), "winners"),
            "prizeall": ("prizeall", "🎁 Пришлите название приза для каждого победителя (до 200 символов).", "prizes"),
            "prizeplaces": ("prizeplaces", "🥇 Задайте призы строками, например:\n1: Телефон\n2-3: Наушники\n4-10: Сертификат\n\nМожно задать диапазоны мест. До 200 символов на приз. Новое сообщение заменяет список призов.", "prizes"),
            "setprizeemoji": ('prizeemoji', '✨ Пришлите назначения строками:\n<code>1: 5368324170671202286</code>\n<code>4-5: 5368324170671202286</code>\n\nСлева — место или диапазон, справа — ID премиум-эмодзи. Бот проверит ID перед сохранением.\nЧтобы убрать назначение с определённых мест: <code>4-5: -</code>.', 'prizeemoji'),
            "count": ("target", "👥 Пришлите число участников для автоматических итогов (не меньше числа мест)." if uid == ADMIN_ID else "👥 Пришлите число участников для автоматических итогов (не меньше числа мест, максимум 1 000 000).", "timing"),
            "addcondition": ("condition", "📣 Пришлите @username, ссылку или пересланное сообщение канала для обязательной подписки. Бот должен быть его администратором.", "conditions"),
            "refbonus": ("refbonus", "⚡ Пришлите процент бонуса за одного друга от 1 до 300. Общий бонус ограничен +300%.", "bonuses"),
            "contact": ("contact", f"🤝 <b>Контакт для выдачи призов</b>\n\nСейчас: {esc(r['contact'] or '—')}\nПришлите @username; его увидят победители. Пришлите - для удаления контакта.", "menu"),
            "title": ("title", "✏️ Пришлите название розыгрыша (до 100 символов).", "menu"),
            "button": ("button", "🎛 Пришлите настройки кнопки участия:\n<code>Текст | цвет | ID премиум-эмодзи</code>\n\nЦвет: обычный, синий, зелёный, красный. Достаточно одного текста. Пример:\n<code>🎁 Участвовать | зелёный</code>\n\nПремиум-эмодзи необязателен; в каналах Telegram разрешает его только некоторым ботам. Без права Telegram отклонит публикацию — удалите ID.", "menu"),
            "addextra": ("extra", "➕ Пришлите кнопку:\n<code>Текст | https://ссылка | цвет | ID премиум-эмодзи</code>\n\nТекст и ссылка обязательны. Цвет и эмодзи необязательны. Цвет: обычный, синий, зелёный, красный.", "extra"),
            "remindminutes": ("remindminutes", "⏰ За сколько минут до итогов отправить напоминание в канал? От 1 до 10 080.", "promo")}
        if action in prompts:
            field, text, back = prompts[action]
            return self.ask(uid, rid, field, text, mid, back)
        self.store.session(uid, {})
        if action == "setwin":
            r["winners"] = self.winner_count(parts[3], uid)
            r["prizes"] = {k: v for k, v in r["prizes"].items() if k == "all" or int(k) <= r["winners"]}
        elif action == 'clearprizeemoji':
            r['prize_emojis'] = []
            self.store.save(r)
            return self.settings(uid, r, 'prizeemoji', mid)
        elif action == "manual":
            r["mode"] = "manual"
        elif action == 'delay':
            if len(parts) != 4 or parts[3] not in ('3600', '86400'):
                raise ValueError('Выберите время через кнопку или пришлите дату и время.')
            r['deadline'] = int(time.time()) + int(parts[3])
            r['mode'] = 'time'
        elif action == "maincondition":
            if not any(c["id"] == r["channel"]["id"] for c in r["conditions"]):
                if len(r["conditions"]) >= 10:
                    raise ValueError("Максимум 10 каналов в условиях.")
                r["conditions"].append(r["channel"])
        elif action == "clearconditions":
            r["conditions"] = []
        elif action == "reaction":
            return self.say(uid, "❤️ Telegram передаёт реакции в каналах анонимно, без связи с участниками. Проверяемый бонус за реакцию здесь недоступен. Можно включить бонус за приглашённых друзей.", keyboard([button("⬅️ Назад", f"g:{rid}:bonuses")]), mid)
        elif action in ("toggleref", "toggleusername", "togglelosers", "togglecount", "togglereminder", "togglepin"):
            field = {"toggleref": "referrals", "toggleusername": "require_username", "togglelosers": "notify_losers", "togglecount": "show_count", "togglereminder": "reminder", "togglepin": "pin"}[action]
            r[field] = not r[field]
        elif action == "rmextra":
            index = int(parts[3])
            if 0 <= index < len(r["extra"]):
                r["extra"].pop(index)
        elif action == "preview":
            if not r.get("post"):
                raise ValueError("Сначала задайте пост розыгрыша.")
            method, params = self.compose(r)
            self.api.call(method, chat_id=uid, reply_markup=self.public_markup(r), **params)
            return self.say(uid, "👁 Это предпросмотр. Приём участников откроется после публикации.", keyboard([button("⬅️ К черновику", f"g:{rid}:menu")]))
        elif action == "publish":
            if not r.get("post"):
                raise ValueError("Сначала задайте пост розыгрыша.")
            if self.needs_deadline(uid, r, mid):
                return
            self.compose(r)
            return self.say(uid, f"🚀 Опубликовать розыгрыш в канале «{esc(r['channel']['title'])}»? После публикации настройки зафиксируются.", keyboard([button("🚀 Опубликовать сейчас", f"g:{rid}:confirmpublish", style="success")], [button("⬅️ Назад", f"g:{rid}:menu")]), mid)
        elif action == "confirmpublish":
            return self.publish(uid, r)
        elif action == "delete":
            return self.say(uid, "Удалить этот черновик?", keyboard([button("🗑 Удалить", f"g:{rid}:confirmdelete", style="danger")], [button("⬅️ Назад", f"g:{rid}:menu")]), mid)
        elif action == "confirmdelete":
            r["status"] = "deleted"
            self.store.save(r)
            return self.list_raffles(uid, mid=mid)
        else:
            raise ValueError("Кнопка устарела. Вернитесь в меню розыгрыша.")
        self.store.save(r)
        screen = {"toggleref": "bonuses", "toggleusername": "guard", "togglelosers": "guard", "togglecount": "promo", "togglereminder": "promo", "togglepin": "promo", "rmextra": "extra", "maincondition": "conditions", "clearconditions": "conditions"}.get(action)
        return self.settings(uid, r, screen, mid) if screen else self.menu(uid, r, mid)

    @staticmethod
    def integer(text, lower, upper=None):
        message = f'Нужно целое число от {lower}' + (f' до {upper}.' if upper is not None else '.')
        try:
            number = int(text)
        except (ValueError, TypeError):
            raise ValueError(message) from None
        if number < lower or (upper is not None and number > upper):
            raise ValueError(message)
        return number

    @staticmethod
    def winners_prompt(uid):
        from .admin import ADMIN_ID
        return '🏆 Пришлите количество победителей — любое положительное целое число.' if uid == ADMIN_ID else '🏆 Пришлите число победителей от 1 до 100.'

    @classmethod
    def winner_count(cls, text, uid):
        from .admin import ADMIN_ID
        return cls.integer(text, 1, None if uid == ADMIN_ID else 100)

    @staticmethod
    def read_channel(message):
        origin = message.get("forward_origin", {})
        if origin.get("type") == "channel":
            return origin["chat"]["id"]
        return chat_reference(message.get("text", ""))

    @staticmethod
    def parse_button(text, extra=False):
        parts = [s.strip() for s in text.split("|")]
        if len(parts) > (4 if extra else 3):
            raise ValueError("Слишком много полей кнопки.")
        if not parts[0] or len(parts[0]) > 50:
            raise ValueError("Текст кнопки должен содержать от 1 до 50 символов.")
        result = {"text": parts[0]}
        if extra:
            if len(parts) < 2:
                raise ValueError("Нужна ссылка после |.")
            url = urlparse(parts[1])
            if url.scheme not in ("https", "http") or not url.netloc or url.username or url.password or len(parts[1]) > 1000:
                raise ValueError("Нужна корректная ссылка http:// или https://.")
            result["url"] = parts[1]
        start = 2 if extra else 1
        colors = {"обычный": None, "синий": "primary", "зелёный": "success", "зеленый": "success", "красный": "danger", "primary": "primary", "success": "success", "danger": "danger", "": None}
        if len(parts) > start:
            if parts[start].lower() not in colors:
                raise ValueError("Цвет: обычный, синий, зелёный или красный.")
            if colors[parts[start].lower()]:
                result["style"] = colors[parts[start].lower()]
        if len(parts) > start + 1 and parts[start + 1]:
            if not re.fullmatch(r"\d{1,30}", parts[start + 1]):
                raise ValueError("ID премиум-эмодзи должен содержать цифры.")
            result["icon_custom_emoji_id"] = parts[start + 1]
        return result

    def input(self, user, message, session):
        uid, field = user["id"], session["field"]
        text = message.get("text", "").strip()
        if field == "channel":
            from .admin import ADMIN_ID
            channel = self.check_channel(self.read_channel(message), uid)
            r = self.store.create(uid, {"channel": channel, "title": channel["title"][:100], "post": None,
                "winners": 1, "prizes": {}, "mode": "time", "deadline": None, "target": None,
                "conditions": [], "referrals": False, "referral_bonus": 100, "require_username": False,
                "notify_losers": False, "contact": "@" + user["username"] if user.get("username") else "",
                "button": {"text": "🎁 Участвовать"}, "extra": [], "show_count": True,
                "reminder": False, "reminder_minutes": 60, "pin": False, "seed": new_seed()})
            self.say(uid, f"✅ Канал подключён: {esc(channel['title'])}\n\nТеперь настройте розыгрыш — начните с поста.")
            return self.menu(uid, r)
        r = self.store.get(session.get("rid", 0))
        if not r or r["owner"] != uid:
            self.store.session(uid, {})
            raise ValueError("Черновик не найден.")
        if field == "recover" and r["status"] == "publishing":
            origin = message.get("forward_origin", {})
            if origin.get("type") != "channel" or origin["chat"]["id"] != r["channel"]["id"]:
                raise ValueError("Нужна пересылка поста из подключённого канала.")
            marker = commitment(r["seed"])
            forwarded_text = message.get("text", "") + message.get("caption", "")
            _, expected = self.compose(r)
            if marker not in forwarded_text and forwarded_text != expected.get("text", expected.get("caption", "")):
                raise ValueError("Текст пересланного поста не совпадает с этим розыгрышем.")
            self.check_channel(r["channel"]["id"], uid)
            r.update(status="active", message_id=origin["message_id"], published=message.get("date", int(time.time())))
            self.store.save(r)
            self.update_count(r)
            return self.menu(uid, r)
        if r["status"] != "draft":
            raise ValueError("Настройки опубликованного розыгрыша зафиксированы.")
        if field == "post":
            kind = next((k for k in ("photo", "video", "animation", "document") if k in message), "text")
            post = {"type": kind, "text": message.get("text", "") if kind == "text" else message.get("caption", ""), "entities": message.get("entities" if kind == "text" else "caption_entities", [])}
            if kind == "text" and not post["text"]:
                raise ValueError("Пришлите текст или поддерживаемый файл.")
            if kind != "text":
                file = message[kind][-1] if kind == "photo" else message[kind]
                post["file_id"] = file["file_id"]
            r["post"] = post
            self.compose(r)
        elif field == "caption":
            if not r.get("post"):
                raise ValueError("Сначала задайте картинку или пост розыгрыша.")
            if not text:
                raise ValueError("Пришлите текст или - для стандартного оформления.")
            r["post"]["text"] = "" if text == "-" else message["text"]
            r["post"]["entities"] = [] if text == "-" else message.get("entities", [])
            self.compose(r)
        elif field == "winners":
            r["winners"] = self.winner_count(text, uid)
            r["prizes"] = {k: v for k, v in r["prizes"].items() if k == "all" or int(k) <= r["winners"]}
        elif field == "prizeall":
            if not text or len(text) > 200:
                raise ValueError("Название приза: от 1 до 200 символов.")
            r["prizes"] = {"all": text}
        elif field == 'prizeemoji':
            assignments = []
            for line in text.splitlines():
                match = re.fullmatch(r'\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*:\s*(\d{1,30}|-)\s*', line)
                if not match:
                    raise ValueError('Формат: 1: ID или 4-5: ID. Для удаления: 4-5: -.')
                start, end = int(match[1]), int(match[2] or match[1])
                if not 1 <= start <= end <= r['winners']:
                    raise ValueError(f"Выберите места от 1 до {r['winners']}.")
                assignments.append({'start': start, 'end': end, 'id': None if match[3] == '-' else match[3]})
            if not assignments:
                raise ValueError('Пришлите место или диапазон и ID эмодзи.')
            rules = r.get('prize_emojis', [])
            if len(rules) + len(assignments) > 100:
                raise ValueError('Сохранено слишком много назначений. Верните обычные эмодзи и задайте нужные диапазоны заново (до 100 назначений).')
            ids = list(dict.fromkeys(a['id'] for a in assignments if a['id']))
            stickers = self.api.call('getCustomEmojiStickers', custom_emoji_ids=ids) if ids else []
            found = {s['custom_emoji_id']: s.get('emoji') or '🎁' for s in stickers}
            for assignment in assignments:
                if assignment['id']:
                    if assignment['id'] not in found:
                        raise ValueError(f"Telegram не нашёл премиум-эмодзи с ID {assignment['id']}. Проверьте ID.")
                    assignment['fallback'] = found[assignment['id']]
            r['prize_emojis'] = rules + assignments
        elif field == "prizeplaces":
            prizes = {}
            for line in text.splitlines():
                match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*:\s*(.+)", line)
                if not match:
                    raise ValueError("Формат каждой строки: 1: Приз или 2-3: Приз.")
                start, end = int(match[1]), int(match[2] or match[1])
                if not 1 <= start <= end <= r["winners"] or len(match[3]) > 200:
                    raise ValueError("Номера мест должны соответствовать числу победителей, приз — до 200 символов.")
                if end - start + 1 + len(prizes) > 10000:
                    raise ValueError('Для более 10 000 мест используйте «Один приз на всех», чтобы не задавать каждое место отдельно.')
                for rank in range(start, end + 1):
                    if str(rank) in prizes:
                        raise ValueError("Одно место нельзя задать дважды.")
                    prizes[str(rank)] = match[3]
            if not prizes:
                raise ValueError("Пришлите хотя бы один приз.")
            r["prizes"] = prizes
        elif field == 'deadline' and text.strip().lower() in ('завтра', 'сегодня'):
            days = 1 if text.strip().lower() == 'завтра' else 0
            day = (datetime.now(MSK) + timedelta(days=days)).strftime('%d.%m.%Y')
            self.store.session(uid, {'rid': r['id'], 'field': 'deadline_clock', 'day': day})
            return self.say(uid, f'📅 Дата: {day}. Во сколько по Москве?\n\nПришлите часы и минуты, например <code>18:00</code>.', keyboard([button('⬅️ Назад', f"g:{r['id']}:time")]))
        elif field in ('deadline', 'deadline_clock'):
            value = session['day'] + ' ' + text if field == 'deadline_clock' else text
            r["deadline"] = parse_deadline(value)
            r["mode"] = "time"
        elif field == "target":
            from .admin import ADMIN_ID
            r["target"] = self.integer(text, r["winners"], None if uid == ADMIN_ID else 1_000_000)
            r["mode"] = "count"
        elif field == "condition":
            if len(r["conditions"]) >= 10:
                raise ValueError("Максимум 10 каналов.")
            channel = self.check_channel(self.read_channel(message))
            if not any(c["id"] == channel["id"] for c in r["conditions"]):
                r["conditions"].append(channel)
        elif field == "refbonus":
            r["referral_bonus"] = self.integer(text, 1, 300)
        elif field == "contact":
            if text != "-" and not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", text):
                raise ValueError("Пришлите @username или - для удаления.")
            r["contact"] = "" if text == "-" else text
        elif field == "title":
            if not text or len(text) > 100:
                raise ValueError("Название: от 1 до 100 символов.")
            r["title"] = text
        elif field == "button":
            r["button"] = self.parse_button(text)
        elif field == "extra":
            if len(r["extra"]) >= 8:
                raise ValueError("Максимум 8 дополнительных кнопок.")
            r["extra"].append(self.parse_button(text, extra=True))
        elif field == "remindminutes":
            r["reminder_minutes"] = self.integer(text, 1, 10080)
        else:
            raise ValueError("Ввод устарел. Откройте /cancel.")
        self.store.save(r)
        self.store.session(uid, {})
        self.say(uid, "✅ Сохранено.")
        self.menu(uid, r)
