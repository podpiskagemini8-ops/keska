"""Copy a private SQLite backup into an empty dedicated PostgreSQL database."""
import argparse
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rafflebot.config import load
from rafflebot.storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    args = parser.parse_args()
    _, url = load()
    if not url.startswith(('postgres://', 'postgresql://')):
        raise SystemExit('Set DATABASE_URL to the dedicated target database.')
    target = Store(url)
    source = sqlite3.connect(Path(args.source).resolve().as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    tables = ('meta', 'users', 'marketing', 'campaigns', 'campaign_deliveries', 'sessions',
              'raffles', 'participants', 'outbox', 'webhook_updates')
    try:
        with target.db:
            if any(target.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables):
                raise RuntimeError('Target database is not empty; nothing was copied.')
            for table in tables:
                if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                for row in source.execute(f'SELECT * FROM {table}'):
                    columns = list(row.keys())
                    query = f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})"
                    target.db.execute(query, tuple(row))
            for table in ('raffles', 'campaigns', 'outbox'):
                target.db.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), coalesce(max(id),1), max(id) IS NOT NULL) FROM {table}")
        print('Migration completed. Start only the Render bot.')
    except Exception as error:
        print('Migration failed: ' + type(error).__name__ + '. No credentials are logged.', file=sys.stderr)
        raise SystemExit(1)
    finally:
        source.close()
        target.db.close()


if __name__ == '__main__':
    main()
