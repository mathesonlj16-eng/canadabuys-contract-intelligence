"""Initialize, refresh, and serve the local procurement database."""
from __future__ import annotations

import argparse
import json
import sqlite3
import math
import sys
import time
from contextlib import closing
from pathlib import Path

from . import core


def refresh(con, feeds):
    succeeded = True
    for feed in feeds:
        print('Downloading', core.OFFICIAL[feed], flush=True)
        try:
            print(core.refresh_feed(con, feed), flush=True)
        except Exception as exc:
            print(f'{feed}: {exc}', file=sys.stderr, flush=True)
            succeeded = False
    return 0 if succeeded else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='Contract Intelligence')
    parser.add_argument('--db', default=str(core.DB_PATH), help='SQLite database path')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create database')
    commands.add_parser('seed-known', help='Load prior research as historical records')
    commands.add_parser('demo', help='Load fictional fixtures and prior research')
    run = commands.add_parser('refresh', help='Download official CanadaBuys tender CSVs')
    run.add_argument('--feed', choices=(*core.OFFICIAL, 'all'), default='canadabuys-open')
    watch = commands.add_parser('watch', help='Refresh periodically while this process is running')
    watch.add_argument('--interval-hours', type=float, default=6)
    watch.add_argument('--feed', choices=(*core.OFFICIAL, 'all'), default='all')
    local = commands.add_parser('import-csv', help='Load a local CSV without marking it as verified current')
    local.add_argument('file')
    local.add_argument('--feed', choices=(*core.OFFICIAL, 'demo'), default='canadabuys-open')
    links = commands.add_parser('backfill-source-urls',
                                help='Fill missing CanadaBuys links derivable from the reference (dry run by default)')
    links.add_argument('--apply', action='store_true', help='Write changes after a verified backup copy')
    links.add_argument('--backup-dir', default=None, help='Where to put the pre-change backup (default: data/backups)')
    intake = commands.add_parser('alert-intake', help='Offline vendor-alert candidate report (no database writes)')
    intake.add_argument('--alerts', default=str(core.ROOT / 'fixtures' / 'email_alerts_2026-11-12.json'))
    intake.add_argument('--out', default=None, help='Optional JSON output, ideally in ignored data/')
    audit = commands.add_parser('email-audit', help='Read-only alert-to-SQLite coverage audit')
    audit.add_argument('--alerts', default=None, help='Sanitized alert JSON (default: CI-06 fixture)')
    audit.add_argument('--out', default=None, help='Optional output JSON report path')
    serve = commands.add_parser('serve', help='Serve the dashboard on loopback')
    serve.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == 'alert-intake':
            from . import alert_intake
            report = alert_intake.run_file(args.alerts)
            output = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
            if args.out:
                out = Path(args.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(output, encoding='utf-8')
                print('Candidate report written:', out)
            else:
                print(output, end='')
            return 0
        if args.command == 'email-audit':
            from . import email_audit
            # Read-only: avoid core.connect() schema-creating side effects.
            uri = Path(args.db).resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True)) as con:
                con.row_factory = sqlite3.Row
                alerts = email_audit.load_alerts(args.alerts) if args.alerts else email_audit.load_alerts()
                result = email_audit.audit_alerts(con, alerts)
            output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
            if args.out:
                Path(args.out).write_text(output, encoding='utf-8')
                print('Audit written:', args.out)
            else:
                print(output, end='')
            return 0
        if args.command == 'serve':
            from .server import serve as run_server
            run_server(Path(args.db), args.port)
            return 0

        if args.command == 'backfill-source-urls' and args.apply:
            from .backup import copy_database
            stamp = time.strftime('%Y%m%dT%H%M%S')
            backup_dir = Path(args.backup_dir) if args.backup_dir else Path(args.db).resolve().parent / 'backups'
            backup = backup_dir / f'{Path(args.db).stem}-before-source-url-backfill-{stamp}.sqlite3'
            copy_database(Path(args.db), backup)
            print('Verified backup:', backup, flush=True)

        with closing(core.connect(args.db)) as con:
            if args.command in ('refresh', 'watch'):
                feeds = tuple(core.OFFICIAL) if args.feed == 'all' else (args.feed,)
                if args.command == 'refresh':
                    return refresh(con, feeds)
                interval = args.interval_hours
                if not math.isfinite(interval) or not 1 <= interval <= 168:
                    raise ValueError('Refresh interval must be between 1 and 168 hours')
                print(f'Refresh every {interval:g} hours while this process runs. Ctrl+C to stop.', flush=True)
                while True:
                    refresh(con, feeds)
                    time.sleep(interval * 3600)
            if args.command == 'init':
                print('Initialized database:', args.db)
            elif args.command == 'seed-known':
                print(core.import_manual(con, core.ROOT / 'fixtures' / 'known_research.json'))
            elif args.command == 'demo':
                sample = core.ROOT / 'fixtures' / 'synthetic_canadabuys.csv'
                print(core.import_csv_text(con, sample.read_text(encoding='utf-8'), 'demo'))
                print(core.import_manual(con, core.ROOT / 'fixtures' / 'known_research.json'))
            elif args.command == 'backfill-source-urls':
                result = core.backfill_source_urls(con, apply=args.apply)
                print(result if args.apply else dict(result, note='dry run; pass --apply to write'))
            elif args.command == 'import-csv':
                print(core.import_csv_text(con, Path(args.file).read_text(encoding='utf-8'), args.feed))
        return 0
    except KeyboardInterrupt:
        print('\nStopped')
        return 0
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
