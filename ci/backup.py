"""Copy a consistent SQLite snapshot to a new file, including committed WAL data."""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3


def copy_database(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=20)) as original:
        # Exclusive creation protects existing databases and previous backups.
        with destination.open('xb'):
            pass
        try:
            with closing(sqlite3.connect(destination, timeout=20)) as copy:
                original.backup(copy)
                if copy.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('SQLite integrity check failed')
                if copy.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise ValueError('SQLite foreign key check failed')
        except BaseException:
            destination.unlink()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    try:
        copy_database(args.source, args.destination)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'Copy failed: {exc}\n')
    print('Verified SQLite copy:', args.destination.resolve())


if __name__ == '__main__':
    main()
