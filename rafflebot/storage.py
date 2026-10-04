import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS marketing(user_id INTEGER PRIMARY KEY REFERENCES users(id), enabled INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS campaign_deliveries(campaign INTEGER NOT NULL REFERENCES campaigns(id), user_id INTEGER NOT NULL REFERENCES users(id), state TEXT NOT NULL DEFAULT 'pending', next_attempt INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(campaign,user_id));
        CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS raffles(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', data TEXT NOT NULL,
            created INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS raffle_owner ON raffles(owner, id);
        CREATE TABLE IF NOT EXISTS participants(
            raffle INTEGER NOT NULL REFERENCES raffles(id), user_id INTEGER NOT NULL,
            data TEXT NOT NULL, referrer INTEGER, joined INTEGER NOT NULL,
            PRIMARY KEY(raffle, user_id));
        CREATE TABLE IF NOT EXISTS outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT, raffle INTEGER NOT NULL,
            task_key TEXT UNIQUE NOT NULL, method TEXT NOT NULL, params TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', next_attempt INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
        """)
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(users)')}
        with self.db:
            for name, definition in [('first_seen', 'INTEGER NOT NULL DEFAULT 0'), ('last_seen', 'INTEGER NOT NULL DEFAULT 0'), ('can_message', 'INTEGER NOT NULL DEFAULT 1')]:
                if name not in columns:
                    self.db.execute(f'ALTER TABLE users ADD COLUMN {name} {definition}')

    def meta(self, key, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def user(self, user):
        with self.db:
            now = int(time.time())
            self.db.execute("INSERT INTO users(id,data,first_seen,last_seen) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,last_seen=excluded.last_seen,can_message=1", (user["id"], json.dumps(user), now, now))

    def advertising(self, uid, enabled=None):
        if enabled is not None:
            with self.db:
                self.db.execute('INSERT INTO marketing VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled', (uid, int(enabled)))
        row = self.db.execute('SELECT enabled FROM marketing WHERE user_id=?', (uid,)).fetchone()
        return bool(row and row[0])

    def advertising_count(self):
        return self.db.execute('SELECT count(*) FROM marketing m JOIN users u ON u.id=m.user_id WHERE m.enabled=1 AND u.can_message=1').fetchone()[0]

    def session(self, uid, data=None):
        if data is not None:
            with self.db:
                self.db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data", (uid, json.dumps(data)))
            return data
        row = self.db.execute("SELECT data FROM sessions WHERE user_id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def create(self, owner, data):
        with self.db:
            cur = self.db.execute("INSERT INTO raffles(owner,data,created) VALUES(?,?,?)", (owner, json.dumps(data), int(time.time())))
        return self.get(cur.lastrowid)

    @staticmethod
    def unpack(row):
        return {**json.loads(row["data"]), "id": row["id"], "owner": row["owner"], "status": row["status"]} if row else None

    def get(self, rid):
        return self.unpack(self.db.execute("SELECT * FROM raffles WHERE id=?", (rid,)).fetchone())

    def save(self, raffle):
        data = {k: v for k, v in raffle.items() if k not in {"id", "owner", "status"}}
        with self.db:
            self.db.execute("UPDATE raffles SET status=?,data=? WHERE id=?", (raffle["status"], json.dumps(data), raffle["id"]))

    def list(self, owner=None, status=None):
        clauses, values = [], []
        if owner is not None:
            clauses.append("owner=?")
            values.append(owner)
        if status:
            clauses.append("status=?")
            values.append(status)
        query = "SELECT * FROM raffles" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id DESC"
        return [self.unpack(row) for row in self.db.execute(query, values)]

    def participants(self, rid):
        return [{**json.loads(row["data"]), "id": row["user_id"], "referrer": row["referrer"], "joined": row["joined"]}
                for row in self.db.execute("SELECT * FROM participants WHERE raffle=? ORDER BY user_id", (rid,))]

    def count(self, rid):
        return self.db.execute("SELECT count(*) FROM participants WHERE raffle=?", (rid,)).fetchone()[0]

    def participant(self, rid, uid):
        return self.db.execute("SELECT 1 FROM participants WHERE raffle=? AND user_id=?", (rid, uid)).fetchone() is not None

    def join(self, rid, user, referrer=None):
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO participants VALUES(?,?,?,?,?)", (rid, user["id"], json.dumps(user), referrer, int(time.time())))
        return cur.rowcount == 1

    def enqueue(self, rid, key, method, params):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO outbox(raffle,task_key,method,params) VALUES(?,?,?,?)", (rid, key, method, json.dumps(params)))

    def pending(self, limit=10):
        return self.db.execute("SELECT * FROM outbox WHERE state='pending' AND next_attempt<=? ORDER BY id LIMIT ?", (int(time.time()), limit)).fetchall()
