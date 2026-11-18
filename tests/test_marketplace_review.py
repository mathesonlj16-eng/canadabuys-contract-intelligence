from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ci.marketplace_intake import import_records, normalize
from ci.marketplace_review import reconcile


class ReviewWriterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'contracts.sqlite3'
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE notices (id TEXT PRIMARY KEY)')

    @staticmethod
    def selection(revision, fingerprint):
        return {
            'schema': 'ci.marketplace_selection.v1',
            'run_mode': 'dry_run',
            'selection': {
                'key': 'upwork:1234567890',
                'revision': revision,
                'fingerprint': fingerprint * 64,
                'verification': 'REVIEW_REQUIRED',
                'eligibility': 'UNKNOWN',
                'application_state': 'PREPARE_ONLY',
                'spend_gate': 'OWNER_APPROVAL_REQUIRED',
            },
        }

    def invoke(self, *events):
        # Hold the write lock until every subprocess is ready to contend for it.
        command = (
            "import json,sys;from pathlib import Path;"
            "from ci.marketplace_review import reconcile;"
            "print('ready',flush=True);"
            "print(json.dumps(reconcile(Path(sys.argv[1]),json.loads(input()))))"
        )
        workers = []
        try:
            with closing(sqlite3.connect(self.db)) as lock, lock:
                lock.execute('BEGIN IMMEDIATE')
                for event in events:
                    worker = subprocess.Popen(
                        [sys.executable, '-c', command, str(self.db)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                    )
                    workers.append(worker)
                    self.assertEqual(worker.stdout.readline().strip(), 'ready')
                    worker.stdin.write(json.dumps(event) + '\n')
                    worker.stdin.flush()
            results = []
            for worker in workers:
                output, errors = worker.communicate(timeout=20)
                self.assertEqual(worker.returncode, 0, errors)
                results.append(json.loads(output))
            return results
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate()

    def check_one(self, revision):
        with closing(sqlite3.connect(self.db)) as con, con:
            rows = con.execute('SELECT id, version_order FROM marketplace_reviews').fetchall()
        self.assertEqual(rows, [(1, revision)])

    def test_simultaneous_create_then_restart_and_retry(self):
        event = self.selection(1, 'a')
        results = self.invoke(event, event)
        self.assertCountEqual([r['review_action'] for r in results], ['create', 'none'])
        self.assertEqual({r['record']['review_record_id'] for r in results}, {'sqlite:1'})
        self.assertTrue(all(r['allow_execution'] is False for r in results))
        self.assertEqual(self.invoke(event)[0]['operation'], 'no_op')
        self.check_one(1)

    def test_overlapping_revisions_and_conflict(self):
        old, new = self.selection(1, 'a'), self.selection(2, 'b')
        results = self.invoke(old, new)
        self.assertEqual(sum(r['review_action'] == 'create' for r in results), 1)
        self.assertEqual({r['record']['review_record_id'] for r in results}, {'sqlite:1'})
        self.check_one(2)
        self.assertEqual(self.invoke(old)[0]['reason'], 'stale_marketplace_event')
        self.assertEqual(self.invoke(self.selection(2, 'c'))[0]['reason'], 'conflicting_marketplace_revision')
        self.check_one(2)

    def test_local_selection_command_reuses_review_after_restart(self):
        item = normalize({
            'kind': 'opportunity', 'platform': 'upwork', 'method': 'user_opened_listing',
            'title': 'Synthetic test listing',
            'source_url': 'https://www.upwork.com/jobs/~1234567890',
            'observed_at': '2026-11-12T20:00:00Z',
        })
        command = [sys.executable, '-m', 'ci.marketplace_review', '--db',
                   str(self.db), '--select', item['key']]
        with closing(sqlite3.connect(self.db)) as con:
            import_records(con, [item])
        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as first, \
                subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as second:
            receipts = []
            for worker in (first, second):
                output, _ = worker.communicate(timeout=20)
                self.assertEqual(worker.returncode, 0)
                receipts.append(json.loads(output))
        self.assertCountEqual([r['review_action'] for r in receipts], ['create', 'none'])
        self.assertEqual({r['record']['review_record_id'] for r in receipts}, {'sqlite:1'})
        # Retry the same selection without supplying a previous receipt.
        replay = subprocess.run(command, capture_output=True, text=True, check=True, timeout=20)
        result = json.loads(replay.stdout)
        self.assertEqual(result['operation'], 'no_op')
        self.assertEqual(result['record']['review_record_id'], 'sqlite:1')
        self.assertEqual(result['application_state'], 'PREPARE_ONLY')
        self.check_one(1)

    def test_killed_writer_rolls_back_uncommitted_update(self):
        event = self.selection(1, 'a')
        reconcile(self.db, event)
        with subprocess.Popen(
            [sys.executable, '-c',
             "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE');"
             "c.execute('UPDATE marketplace_reviews SET version_order=99');print('written',flush=True);input()",
             str(self.db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        ) as worker:
            self.assertEqual(worker.stdout.readline().strip(), 'written')
            worker.kill()
            worker.communicate(timeout=10)
        self.check_one(1)
        result = self.invoke(self.selection(2, 'b'))[0]
        self.assertEqual(result['review_action'], 'update')
        self.assertEqual(result['record']['review_record_id'], 'sqlite:1')
        self.check_one(2)

    def test_owner_disposition_survives_updates_and_blocks_closed_rows(self):
        reconcile(self.db, self.selection(1, 'a'))
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE marketplace_reviews SET state='needs_review', owner_notes='keep', github_issue='example'")
        self.assertEqual(reconcile(self.db, self.selection(2, 'b'))['review_action'], 'update')
        with closing(sqlite3.connect(self.db)) as con, con:
            self.assertEqual(con.execute(
                'SELECT state, owner_notes, github_issue FROM marketplace_reviews'
            ).fetchone(), ('needs_review', 'keep', 'example'))
            con.execute('UPDATE marketplace_reviews SET automation_blocked=1')
        self.assertEqual(reconcile(self.db, self.selection(3, 'c'))['reason'], 'review_closed_or_blocked')
        self.check_one(2)

    def test_approved_input_rejected_before_write(self):
        event = self.selection(1, 'a')
        event['selection']['application_state'] = 'APPROVED'
        with self.assertRaises(ValueError):
            reconcile(self.db, event)
        with closing(sqlite3.connect(self.db)) as con, con:
            self.assertEqual(con.execute('SELECT name FROM sqlite_master').fetchall(),
                             [('notices',), ('sqlite_autoindex_notices_1',)])
