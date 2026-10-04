"""Write a private initial-import env file; never commit it or print its contents."""
import base64
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rafflebot.config import load, ROOT
from rafflebot.migration import TABLES


def main():
    _, source = load()
    db = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    with db:
        # A consistent read transaction even if another connection previously wrote.
        db.execute('BEGIN')
        tables = {}
        for table in TABLES:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                tables[table] = [dict(row) for row in db.execute(f'SELECT * FROM {table}')]
    db.close()
    encoded = base64.b64encode(json.dumps({'version': 1, 'tables': tables}).encode()).decode()
    output = ROOT / '.env.render-import'
    output.write_text('IMPORT_DATA_BASE64=' + encoded + '\nCHECK_POSTGRES=1\n', encoding='utf-8')
    print('Private import file created: .env.render-import (not tracked in Git).')


if __name__ == '__main__':
    main()
