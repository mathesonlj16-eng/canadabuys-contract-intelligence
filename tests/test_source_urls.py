"""Offline tests for CanadaBuys source-link derivation and backfill. All references below are fictional."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from ci import cli, core

PAGE = 'https://canadabuys.canada.ca/en/tender-opportunities/tender-notice/'


def row(ref, url=''):
    return {'referenceNumber-numeroReference': ref, 'title-titre-eng': 'SYNTHETIC notice',
            'publicationDate-datePublication': '2026-11-10', 'noticeURL-URLavis-eng': url}


class DeriveTests(unittest.TestCase):
    def test_known_reference_formats_map_to_public_page(self):
        self.assertEqual(core.canadabuys_notice_url('cb-335-48912487'), PAGE + 'cb-335-48912487')
        self.assertEqual(core.canadabuys_notice_url('cb-1-2'), PAGE + 'cb-1-2')
        self.assertEqual(core.canadabuys_notice_url('WS4789309300-Doc4789317450'), PAGE + 'WS4789309300-Doc4789317450')

    def test_unreliable_formats_get_no_link(self):
        for ref in ('PW-_MER-005-29423', 'SSC-26-00035398:T', 'PW-23-01027079', 'MX-243100787103',
                    'cb-12', 'cb-1-2/../x', ' ', '', 'DEMO-001'):
            self.assertEqual(core.canadabuys_notice_url(ref), '', ref)

    def test_feed_url_wins_and_missing_url_is_derived(self):
        buyer = 'https://www.merx.com/public/solicitations/1/abstract'
        self.assertEqual(core.normalized_from_csv(row('cb-1-2', buyer), 'canadabuys-open')['source_url'], buyer)
        self.assertEqual(core.normalized_from_csv(row('cb-1-2'), 'canadabuys-open')['source_url'], PAGE + 'cb-1-2')
        self.assertEqual(core.normalized_from_csv(row('SSC-1-2:T'), 'canadabuys-open')['source_url'], '')

    def test_demo_fixtures_never_get_live_links(self):
        self.assertEqual(core.normalized_from_csv(row('cb-1-2'), 'demo')['source_url'], '')

    def test_derivation_does_not_change_row_hash(self):
        # Change detection keys on raw_json only, so derived links cannot create spurious versions.
        notice = core.normalized_from_csv(row('cb-1-2'), 'canadabuys-open')
        self.assertEqual(core.hash_snapshot(notice), core.hash_snapshot(dict(notice, source_url='')))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'test.sqlite3'
        self.con = core.connect(self.db)
        for ref, url in (('cb-1-2', ''), ('WS1-Doc2', ''), ('SSC-1-2:T', ''),
                         ('MX-1', 'https://www.merx.com/public/solicitations/9/abstract')):
            notice = core.normalized_from_csv(row(ref, url), 'canadabuys-open')
            core.upsert(self.con, dict(notice, source_url=url))  # simulate rows stored before the fix
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def urls(self):
        return dict(self.con.execute('SELECT source_key, source_url FROM notices'))

    def test_dry_run_writes_nothing(self):
        before = self.urls()
        result = core.backfill_source_urls(self.con)
        self.assertEqual(result, dict(missing=3, derivable=2, updated=0, not_derivable=1))
        self.assertEqual(self.urls(), before)

    def test_apply_fills_only_derivable_and_is_idempotent(self):
        versions = self.con.execute('SELECT COUNT(*), MAX(version_id) FROM notice_versions').fetchone()
        hashes = dict(self.con.execute('SELECT id, row_hash FROM notices'))
        first = core.backfill_source_urls(self.con, apply=True)
        self.assertEqual(first['updated'], 2)
        urls = self.urls()
        self.assertEqual(urls['cb-1-2'], PAGE + 'cb-1-2')
        self.assertEqual(urls['WS1-Doc2'], PAGE + 'WS1-Doc2')
        self.assertEqual(urls['SSC-1-2:T'], '')
        self.assertEqual(urls['MX-1'], 'https://www.merx.com/public/solicitations/9/abstract')
        self.assertEqual(core.backfill_source_urls(self.con, apply=True),
                         dict(missing=1, derivable=0, updated=0, not_derivable=1))
        self.assertEqual(self.con.execute('SELECT COUNT(*), MAX(version_id) FROM notice_versions').fetchone(), versions)
        self.assertEqual(dict(self.con.execute('SELECT id, row_hash FROM notices')), hashes)

    def test_cli_apply_takes_verified_backup_first(self):
        self.con.close()
        backups = Path(self.temp.name) / 'backups'
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['--db', str(self.db), 'backfill-source-urls', '--apply',
                                       '--backup-dir', str(backups)]), 0)
        copies = list(backups.glob('*.sqlite3'))
        self.assertEqual(len(copies), 1, out.getvalue())
        with closing(sqlite3.connect(copies[0])) as old:
            self.assertEqual(old.execute("SELECT source_url FROM notices WHERE source_key='cb-1-2'").fetchone()[0], '')
        self.con = core.connect(self.db)
        self.assertEqual(self.urls()['cb-1-2'], PAGE + 'cb-1-2')

    def test_cli_default_is_dry_run(self):
        self.con.close()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['--db', str(self.db), 'backfill-source-urls']), 0)
        self.assertIn('dry run', out.getvalue())
        self.con = core.connect(self.db)
        self.assertEqual(self.urls()['cb-1-2'], '')
