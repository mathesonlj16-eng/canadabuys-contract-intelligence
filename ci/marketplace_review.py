"""Transaction-backed marketplace review writes in the existing CI database."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from ci.marketplace_intake import HANDOFF_SCHEMA, selected_handoff

KEY = re.compile(r'marketplace:(?:upwork:[0-9]{1,120}|(?:contra|charityvillage|indeed|linkedin):[A-Za-z0-9_.-]{4,120})')
FINGERPRINT = re.compile(r'[a-f0-9]{64}')
OPEN_STATES = {'open', 'manual_review', 'needs_review'}

DDL = """
CREATE TABLE IF NOT EXISTS marketplace_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL UNIQUE,
    version_order INTEGER NOT NULL CHECK(version_order > 0),
    event_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    automation_blocked INTEGER NOT NULL DEFAULT 0 CHECK(automation_blocked IN (0, 1)),
    owner_notes TEXT,
    github_issue TEXT
)
"""


def reconcile(db: Path, envelope: dict) -> dict:
    """Commit one prepare-only selection and return the current durable review."""
    selected = envelope.get('selection') if isinstance(envelope, dict) else None
    if (not isinstance(selected, dict) or envelope.get('schema') != HANDOFF_SCHEMA
            or envelope.get('run_mode') != 'dry_run'):
        raise ValueError('expected one dry-run marketplace selection')
    key = selected.get('key')
    task_key = 'marketplace:' + key if isinstance(key, str) else ''
    revision = selected.get('revision')
    fingerprint = selected.get('fingerprint')
    if (not KEY.fullmatch(task_key) or type(revision) is not int or not 1 <= revision < 2**53
            or not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint)
            or selected.get('verification') != 'REVIEW_REQUIRED'
            or selected.get('eligibility') != 'UNKNOWN'
            or selected.get('application_state') != 'PREPARE_ONLY'
            or selected.get('spend_gate') not in ('OWNER_APPROVAL_REQUIRED', 'CHECK_BEFORE_APPLYING')):
        raise ValueError('invalid or non-prepare-only marketplace selection')
    event_key = f'{task_key}:{revision}:{fingerprint}'

    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=rw', uri=True, timeout=15)) as con:
        con.row_factory = sqlite3.Row
        with con:
            con.execute('BEGIN IMMEDIATE')
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notices'").fetchone():
                raise ValueError('expected an existing Contract Intelligence database')
            con.execute(DDL)
            row = con.execute('SELECT * FROM marketplace_reviews WHERE task_key=?', (task_key,)).fetchone()
            if row is None:
                con.execute(
                    'INSERT INTO marketplace_reviews(task_key, version_order, event_key) VALUES (?, ?, ?)',
                    (task_key, revision, event_key),
                )
                action, reason = 'create', 'marketplace_new_review'
            elif row['state'] not in OPEN_STATES or row['automation_blocked']:
                action, reason = 'hold', 'review_closed_or_blocked'
            elif row['event_key'] == event_key:
                action, reason = 'none', 'already_reviewed'
            elif revision < row['version_order']:
                action, reason = 'hold', 'stale_marketplace_event'
            elif revision == row['version_order']:
                action, reason = 'hold', 'conflicting_marketplace_revision'
            else:
                con.execute(
                    'UPDATE marketplace_reviews SET version_order=?, event_key=? WHERE id=?',
                    (revision, event_key, row['id']),
                )
                action, reason = 'update', 'newer_marketplace_revision'

            saved = con.execute(
                'SELECT id, task_key, version_order, event_key, state, automation_blocked '
                'FROM marketplace_reviews WHERE task_key=?', (task_key,),
            ).fetchone()

        # Return only after commit. A lost response can be retried with the same event.
        record = dict(saved)
        record['review_record_id'] = f"sqlite:{record.pop('id')}"
        record['automation_blocked'] = bool(record['automation_blocked'])
        return {
            'operation': 'no_op' if action == 'none' else 'manual_review',
            'review_action': action,
            'reason': reason,
            'record': record,
            'allow_execution': False,
            'application_state': 'PREPARE_ONLY',
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('selection', nargs='?', type=Path, help='Existing sanitized CI selection JSON')
    source.add_argument('--select', metavar='KEY', help='Select an existing marketplace listing from this database')
    parser.add_argument('--db', type=Path, required=True, help='Existing Contract Intelligence SQLite database')
    args = parser.parse_args()
    try:
        if args.select is not None:
            payload = selected_handoff(args.db, args.select)
        else:
            payload = json.loads(args.selection.read_text(encoding='utf-8-sig'))
        result = reconcile(args.db, payload)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
