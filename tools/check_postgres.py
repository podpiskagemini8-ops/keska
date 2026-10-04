"""Run business tests in disposable schemas, never touching production rows."""
import io
import re
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tests'))
from rafflebot.config import load
from rafflebot.storage import Store


def check(url):
    import psycopg
    from psycopg import sql
    import test_bot
    import test_admin
    schemas = []
    control = psycopg.connect(url, autocommit=True, connect_timeout=15)
    def factory(path):
        if path != ':memory:':
            return Store(path)
        schema = 'check_' + uuid.uuid4().hex
        control.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        schemas.append(schema)
        return Store(url, schema=schema)
    try:
        suite = unittest.TestSuite()
        for module in (test_bot, test_admin):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
        stream = io.StringIO()
        with patch.object(test_bot, 'Store', factory), patch.object(test_admin, 'Store', factory):
            result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        if not result.wasSuccessful():
            # Don't print database errors or tracebacks containing credentials.
            print('PostgreSQL check failed: ' + ', '.join(test.id() for test, _ in result.errors + result.failures))
            for test, detail in (result.errors + result.failures)[:2]:
                summary = detail.strip().splitlines()[-1]
                summary = re.sub(r'postgres(?:ql)?://\S+', '[connection hidden]', summary)
                print(test.id() + ': ' + summary)
            raise RuntimeError('PostgreSQL compatibility checks failed')
        print(f'PostgreSQL compatibility: {result.testsRun} tests passed.')
    finally:
        for schema in schemas:
            control.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        control.close()


if __name__ == '__main__':
    try:
        _, url = load()
        check(url)
    except Exception as error:
        print('Database check failed: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
