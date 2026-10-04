"""Small DB-API bridge for the existing SQLite queries and row access."""
import re


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def translate(query):
    ignore = bool(re.search(r'INSERT OR IGNORE', query, re.I))
    query = re.sub(r'INSERT OR IGNORE', 'INSERT', query, flags=re.I)
    query = query.replace('max(next_attempt,?)', 'greatest(next_attempt,?)')
    if ignore:
        query = query.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
    return query.replace('%', '%%').replace('?', '%s')


class Connection:
    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row
        self.connection = psycopg.connect(url, autocommit=True, row_factory=dict_row,
                                         connect_timeout=15, prepare_threshold=None)
        self.transactions = []

    def execute(self, query, params=()):
        query = translate(query)
        returning = bool(re.match(r'\s*INSERT INTO (raffles|campaigns)\s*\(', query, re.I))
        if returning:
            query += ' RETURNING id'
        cur = self.connection.execute(query, params)
        lastrowid = cur.fetchone()['id'] if returning else None
        return Cursor(cur, lastrowid)

    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip() and not statement.strip().startswith('PRAGMA'):
                statement = statement.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY')
                statement = re.sub(r'\bINTEGER\b', 'BIGINT', statement)
                self.execute(statement)

    def __enter__(self):
        transaction = self.connection.transaction()
        transaction.__enter__()
        self.transactions.append(transaction)
        return self

    def __exit__(self, *args):
        return self.transactions.pop().__exit__(*args)

    def close(self):
        self.connection.close()
