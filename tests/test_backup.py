from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ci import core
from ci.backup import copy_database


class BackupTests(unittest.TestCase):
    def test_restore_captures_saved_review_while_source_is_open(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'live.sqlite3'
            backup = Path(folder) / 'backup.sqlite3'
            restored = Path(folder) / 'restore.sqlite3'
            with closing(core.connect(source)) as con:
                core.import_manual(con, core.ROOT / 'fixtures' / 'known_research.json')
                notice = core.get_detail(con, 'manual:O01')
                gates = {key: {'state': 'unknown', 'evidence': ''} for key in core.GATES}
                core.update_review(con, notice['id'], {
                    'revision': notice['row_hash'], 'review_updated_at': None,
                    'stage': 'saved', 'notes': 'Sample recovery note', 'gates': gates,
                    'complexity': 'unknown', 'budget': None, 'revenue': None, 'cost': None,
                })
                self.assertTrue(source.with_suffix('.sqlite3-wal').exists())
                copy_database(source, backup)
                with self.assertRaises(FileExistsError):
                    copy_database(source, backup)
                with con:
                    con.execute("UPDATE notices SET user_notes='' WHERE id=?", (notice['id'],))
            copy_database(backup, restored)
            with closing(core.connect(restored)) as con:
                record = core.get_detail(con, notice['id'])
                self.assertEqual(record['user_stage'], 'saved')
                self.assertEqual(record['user_notes'], 'Sample recovery note')
                self.assertEqual(record['review']['gates'], gates)

    def test_missing_source_cannot_create_an_empty_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'missing.sqlite3'
            backup = Path(folder) / 'backup.sqlite3'
            with self.assertRaises(FileNotFoundError):
                copy_database(source, backup)
            self.assertFalse(backup.exists())
