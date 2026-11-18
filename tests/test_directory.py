"""Regressions for official snapshots, revision-aware reviews, and complete ranking."""
import csv
import io
from datetime import datetime, timedelta, timezone

from ci import core
from pathlib import Path
import tempfile
import unittest


def official_sample(*, number='002', ref='TEST-1', close=None, title='WordPress website redesign'):
    close = close or datetime.now(timezone.utc) + timedelta(days=10)
    row = {
        'title-titre-eng': title,
        'referenceNumber-numeroReference': ref,
        'solicitationNumber-numeroSollicitation': 'S-1',
        'publicationDate-datePublication': '2026-01-01',
        'amendmentNumber-numeroModification': number,
        'amendmentDate-dateModification': '',
        'tenderClosingDate-appelOffresDateCloture': close.isoformat(),
        'tenderStatus-appelOffresStatut-eng': 'Open',
        'attachment-piecesJointes-eng': 'https://example.com/notice.pdf,https://example.com/addendum.pdf',
        'contractingEntityName-nomEntitContractante-eng': 'Test Buyer',
        'noticeURL-URLavis-eng': '',
        'tenderDescription-descriptionAppelOffres-eng': 'Rebuild a CMS.',
    }
    return row


def as_csv(*rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.con = core.connect(Path(workspace.name) / 'test.sqlite3')
        self.addCleanup(self.con.close)

    def snapshot(self, *rows, age=0, feed='canadabuys-open'):
        published = (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat()
        return core.import_csv_text(self.con, as_csv(*rows), feed, verified=True, published_utc=published)

    def test_missing_and_expired_are_not_active(self):
        self.snapshot(official_sample(ref='TEST-1'),
                      official_sample(ref='TEST-2', close=datetime.now(timezone.utc) - timedelta(hours=1)))
        self.assertEqual(core.get_notices(self.con, state='active')['total'], 1)
        self.snapshot(official_sample(ref='TEST-3'))
        self.assertEqual(core.get_detail(self.con, 'canadabuys:TEST-1')['record_state'], 'unverified')
        detail = core.get_detail(self.con, 'canadabuys:TEST-2')
        self.assertEqual((detail['record_state'], detail['status']), ('historical', 'open'))

    def test_offline_open_and_old_official_open_require_recheck(self):
        core.import_csv_text(self.con, as_csv(official_sample()))
        self.assertEqual(core.get_notices(self.con, state='active')['total'], 0)
        self.snapshot(official_sample(), age=49)
        self.assertEqual(core.get_notices(self.con, view='recommended')['total'], 0)

    def test_new_feed_amendment_cannot_be_reverted_by_older_open(self):
        self.snapshot(official_sample(), age=5)
        self.snapshot(official_sample(number='003'), age=1, feed='canadabuys-new')
        replay = self.snapshot(official_sample(), age=3)
        detail = core.get_detail(self.con, 'canadabuys:TEST-1')
        self.assertEqual((replay['skipped'], detail['amendment'], len(detail['versions'])), (1, '003', 2))
        self.assertEqual(len(detail['documents']), 2)

    def test_pipeline_gate_and_amendment_invalidation(self):
        self.snapshot(official_sample(), age=2)
        notice_id = 'canadabuys:TEST-1'
        detail = core.get_detail(self.con, notice_id)
        gates = {key: {'state': 'unknown', 'evidence': ''} for key in core.GATES}
        gates['insurance'] = {'state': 'not_met', 'evidence': 'Section 4 requires higher coverage'}
        payload = dict(stage='saved', notes='Verify insurance', gates=gates, complexity='unknown',
                       revision=detail['row_hash'], review_updated_at=None, budget=None, cost=500, revenue=1500)
        core.update_review(self.con, notice_id, payload)
        self.assertEqual(core.get_notices(self.con, view='recommended')['total'], 0)
        detail = core.get_detail(self.con, notice_id)
        self.assertEqual((detail['eligibility'], detail['estimated_profit']), ('not_met', 1000))
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            core.update_review(self.con, notice_id, payload)
        self.snapshot(official_sample(number='003'))
        detail = core.get_detail(self.con, notice_id)
        self.assertEqual((detail['eligibility'], detail['estimated_profit'], detail['user_stage']),
                         ('unknown', None, 'saved'))

    def test_pagination_happens_after_ranking(self):
        weak = official_sample(ref='A', title='Contract')
        weak['tenderDescription-descriptionAppelOffres-eng'] = 'WordPress'
        self.snapshot(weak, official_sample(ref='B'))
        page = core.get_notices(self.con, view='recommended', limit=1)
        self.assertEqual((page['total'], page['notices'][0]['source_key']), (2, 'B'))

    def test_failed_snapshot_is_atomic_and_audited(self):
        self.snapshot(official_sample())
        row = official_sample(ref='TEST-2')
        with self.assertRaisesRegex(ValueError, 'Duplicate source reference'):
            self.snapshot(row, row)
        summary = core.summary(self.con)
        self.assertEqual(summary['total_notices'], 1)
        self.assertEqual(summary['runs'][0]['status'], 'failed')
        self.assertEqual(summary['states']['active'], 1)

    def test_restricted_route_requires_review_even_if_csv_type_is_rfp(self):
        row = official_sample()
        row['noticeType-avisType-eng'] = 'Request for Proposal'
        row['tenderDescription-descriptionAppelOffres-eng'] = (
            'This requirement is only open to the below list of pre-qualified suppliers.')
        self.snapshot(row)
        notice_id = 'canadabuys:TEST-1'
        detail = core.get_detail(self.con, notice_id)
        self.assertTrue(detail['restricted_route'])
        self.assertEqual(detail['eligibility'], 'unreviewed')
        self.assertEqual(core.get_notices(self.con, view='recommended')['total'], 0)
        gates = {key: {'state': 'met', 'evidence': 'Synthetic documentary evidence'} for key in core.GATES}
        gates['references'] = {'state': 'unknown', 'evidence': ''}
        gates['insurance'] = {'state': 'unknown', 'evidence': ''}
        payload = dict(stage='saved', notes='Sample', gates=gates, complexity='moderate',
                       revision=detail['row_hash'], review_updated_at=None,
                       budget=3000, cost=None, revenue=2000)
        core.update_review(self.con, notice_id, payload)
        detail = core.get_detail(self.con, notice_id)
        self.assertIsNone(detail['estimated_profit'])
        self.assertIsNone(detail['components']['Economics'])
        self.assertEqual(core.get_notices(self.con, view='recommended')['total'], 0)
        gates['insurance'] = gates['references'] = {'state': 'met', 'evidence': 'Verified for test'}
        core.update_review(self.con, notice_id, dict(payload, review_updated_at=detail['review_updated_at']))
        self.assertEqual(core.get_notices(self.con, view='recommended')['total'], 1)
