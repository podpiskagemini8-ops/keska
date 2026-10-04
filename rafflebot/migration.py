"""One-time import into an empty dedicated database; atomic and repeat-safe."""
import base64
import json

TABLES = ('meta', 'users', 'marketing', 'campaigns', 'campaign_deliveries', 'sessions',
          'raffles', 'participants', 'outbox', 'webhook_updates')


def import_snapshot(store, encoded):
    payload = json.loads(base64.b64decode(encoded, validate=True))
    if payload.get('version') != 1 or set(payload['tables']) - set(TABLES):
        raise RuntimeError('Invalid migration format')
    with store.db:
        if store.meta('import_completed_v1'):
            return
        if any(store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES):
            raise RuntimeError('Migration target must be empty')
        for table in TABLES:
            for row in payload['tables'].get(table, []):
                # Accept only actual column names from this table's schema.
                if store.postgres:
                    allowed = {r[0] for r in store.db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (table,))}
                else:
                    allowed = {r[1] for r in store.db.execute(f'PRAGMA table_info({table})')}
                if set(row) - allowed:
                    raise RuntimeError('Invalid migration columns')
                keys = list(row)
                store.db.execute(f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", tuple(row.values()))
        if store.postgres:
            for table in ('raffles', 'campaigns', 'outbox'):
                store.db.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), coalesce(max(id),1), max(id) IS NOT NULL) FROM {table}")
        store.set_meta('import_completed_v1', '1')
