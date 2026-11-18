"""Offline tests: no network, no paid APIs, all tenders below are fictional test fixtures."""
import copy
import json
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from ci import core

FIXTURE=(core.ROOT/'fixtures'/'synthetic_canadabuys.csv').read_text(encoding='utf8')

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/'test.sqlite3'
        self.con=core.connect(self.db)
    def tearDown(self):
        self.con.close();self.temp.cleanup()

    def test_seed_is_idempotent(self):
        a=core.import_csv_text(self.con,FIXTURE,'demo')
        b=core.import_csv_text(self.con,FIXTURE,'demo')
        self.assertEqual(a['added'],4)
        self.assertEqual(b['added'],0)
        self.assertEqual(b['unchanged'],4)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM notices').fetchone()[0],4)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM notice_versions').fetchone()[0],4)

    def test_amendment_is_versioned_and_deduplicated(self):
        core.import_csv_text(self.con,FIXTURE,'demo')
        changed=FIXTURE.replace('2026-12-20 14:00:00','2026-12-28 14:00:00')
        r=core.import_csv_text(self.con,changed,'demo')
        self.assertEqual(r['changed'],1)
        self.assertEqual(r['unchanged'],3)
        n=self.con.execute("SELECT close_raw FROM notices WHERE id='demo:DEMO-001'").fetchone()
        self.assertEqual(n['close_raw'],'2026-12-28 14:00:00')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM notice_versions WHERE notice_id='demo:DEMO-001'").fetchone()[0],2)
        self.assertEqual(core.import_csv_text(self.con,changed,'demo')['changed'],0)

    def test_schema_drift_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'missing required columns'):
            core.import_csv_text(self.con,'foo,bar\na,b','demo')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM notices').fetchone()[0],0)

    def test_date_timezone_is_fixed_minus_five_not_toronto_dst(self):
        self.assertEqual(core.closing_to_utc('2026-07-01 12:00:00'),'2026-07-01T17:00:00+00:00')
        self.assertEqual(core.closing_to_utc('2026-11-20 12:00:00'),'2026-11-20T17:00:00+00:00')
        self.assertIsNone(core.closing_to_utc('2026-11-20'))
        self.assertIsNone(core.closing_to_utc('not a timestamp'))

    def test_hard_gate_overrides_text_score(self):
        core.import_csv_text(self.con,FIXTURE,'demo')
        n=dict(self.con.execute("SELECT * FROM notices WHERE id='demo:DEMO-001'").fetchone())
        n['source_id']='canadabuys';n['eligibility']='not_met'
        r=core.score_notice(n,now=datetime(2026,11,11,tzinfo=timezone.utc))
        self.assertGreater(r['score'],10)
        self.assertEqual(r['recommendation'],'Not eligible as solo bidder')

    def test_manual_prior_research_never_claimed_open(self):
        r=core.import_manual(self.con,core.ROOT/'fixtures'/'known_research.json')
        self.assertEqual(r['added'],4)
        row=self.con.execute("SELECT status,source_id,closing_utc,close_raw FROM notices WHERE id='manual:O04'").fetchone()
        self.assertEqual(row['status'],'needs source recheck')
        self.assertEqual(row['source_id'],'manual')
        self.assertIsNone(row['closing_utc'])
        self.assertEqual(row['close_raw'],'2026-11-30')

    def test_filtered_query_and_stats(self):
        core.import_csv_text(self.con,FIXTURE,'demo')
        core.import_manual(self.con,core.ROOT/'fixtures'/'known_research.json')
        r=core.get_notices(self.con,q='WordPress')
        self.assertGreaterEqual(r['total'],1)
        self.assertEqual(core.summary(self.con)['total_notices'],8)
        self.assertEqual(core.get_notices(self.con,source='manual')['total'],4)
        self.assertTrue(all(item['source_id']=='demo' for item in core.get_notices(self.con,source='demo')['notices']))

    def test_missing_notice_does_not_infer_cancelled(self):
        core.import_csv_text(self.con,FIXTURE,'demo')
        one='\n'.join(FIXTURE.splitlines()[:2])+'\n'
        core.import_csv_text(self.con,one,'demo')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM notices').fetchone()[0],4)
        self.assertEqual(self.con.execute("SELECT status FROM notices WHERE id='demo:DEMO-004'").fetchone()['status'],'open')

if __name__=='__main__':unittest.main()
