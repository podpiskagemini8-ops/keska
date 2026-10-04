"""Create a consistent private SQLite backup without copying WAL files."""
import argparse
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rafflebot.config import load


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    _, source = load()
    target = Path(args.output).resolve()
    if target == Path(source).resolve() or target.exists():
        raise SystemExit('Choose a new backup path; existing files are not overwritten.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise SystemExit('Backup integrity check failed.')
    print('Database backup created (keep it private).')


if __name__ == '__main__':
    main()
