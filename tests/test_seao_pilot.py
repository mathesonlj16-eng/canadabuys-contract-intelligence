"""Focused regressions for the isolated SEAO import."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from ci import core
from ci.seao_pilot import connect_pilot, import_package, utc


def release(ocid, release_id, date, tag, **tender):
    return dict(ocid=ocid, id=release_id, date=date, tag=[tag], tender=tender)


def package(*releases):
    return json.dumps(dict(uri='https://example.test/package', license='https://example.test/cc-by',
                           publishedDate='2026-11-10T10:00:00-05:00', releases=releases)).encode()


class PilotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.con = connect_pilot(self.directory / 'pilot.sqlite3')
        self.addCleanup(self.con.close)

    def fields(self, ocid='one'):
        return json.loads(self.con.execute('SELECT fields FROM pilot_tenders WHERE ocid=?', (ocid,)).fetchone()[0])

    def test_composite_identity_and_replay(self):
        one = release('one', 'shared', '2026-10-29T11:00:00-04:00', 'tender', id='same', title='One')
        two = release('two', 'shared', '2026-10-29T11:00:00-04:00', 'tender', id='same', title='Two')
        data = package(one, two)
        self.assertEqual(import_package(self.con, data)['inserted'], 2)
        self.assertEqual(import_package(self.con, data), dict(inserted=0, replayed=2, skipped=0, conflicted=0, rebuilt=0))
        self.assertEqual(self.fields('one')['title'], 'One')
        self.assertEqual(self.fields('two')['title'], 'Two')

    def test_reverse_arrival_merges_sparse_amendments_and_cancellation(self):
        original = release('one', 'a', '2026-10-29T09:00:00-04:00', 'tender',
                           title='Original', status='active',
                           tenderPeriod={'endDate': '2026-11-15T13:00:00-05:00'},
                           documents=[{'url': 'https://seao.gouv.qc.ca/avis?id=1'}])
        amendment = release('one', 'b', '2026-11-03T09:00:00-05:00', 'tenderUpdate',
                            title='Amended', tenderPeriod={'endDate': '2026-11-20T14:00:00-05:00'})
        sparse = release('one', 'c', '2026-11-04T09:00:00-05:00', 'tenderUpdate', mainProcurementCategory='services')
        cancelled = release('one', 'd', '2026-11-05T09:00:00-05:00', 'tenderCancellation')
        import_package(self.con, package(cancelled, sparse))
        import_package(self.con, package(amendment))
        import_package(self.con, package(original, amendment))
        fields = self.fields()
        self.assertEqual(fields['status'], 'cancelled')
        self.assertEqual(fields['title'], 'Amended')
        self.assertEqual(fields['closing_utc'], '2026-11-20T19:00:00.000000+00:00')
        self.assertEqual(fields['notice_links'], ['https://seao.gouv.qc.ca/avis?id=1'])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM pilot_releases').fetchone()[0], 4)

    def test_award_and_absence_do_not_close_a_notice(self):
        active = release('one', 'a', '2026-11-01T09:00:00-05:00', 'tender', status='active')
        award = release('one', 'b', '2026-11-05T09:00:00-05:00', 'awardUpdate', status='complete')
        import_package(self.con, package(active))
        self.assertEqual(import_package(self.con, package(award))['skipped'], 1)
        self.assertEqual(self.fields()['status'], 'active')

    def test_identity_collision_rolls_back_the_whole_package(self):
        one = release('one', 'a', '2026-11-01T09:00:00-05:00', 'tender', title='Original')
        import_package(self.con, package(one))
        conflict = dict(one, tender={'title': 'Conflict'})
        two = dict(one, ocid='two')
        with self.assertRaisesRegex(ValueError, 'identity collision'):
            import_package(self.con, package(two, conflict))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM pilot_releases').fetchone()[0], 1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM pilot_runs').fetchone()[0], 1)

    def test_offsets_around_dst_and_missing_offset(self):
        self.assertEqual(utc('2026-11-01T01:30:00-04:00'), '2026-11-01T05:30:00.000000+00:00')
        self.assertEqual(utc('2026-11-01T01:30:00-05:00'), '2026-11-01T06:30:00.000000+00:00')
        for invalid in ('2026-11-01', '2026-11-01T01:30:00'):
            with self.assertRaisesRegex(ValueError, 'explicit offset'):
                utc(invalid)

    def test_rejects_directory_database(self):
        path = self.directory / 'directory.sqlite3'
        with closing(core.connect(path)):
            pass
        with self.assertRaisesRegex(ValueError, 'pilot database'):
            connect_pilot(path)

    def test_conflicting_tied_fields_abstain_until_newer_value(self):
        date = '2026-11-05T09:00:00-05:00'
        active = release('one', 'a', date, 'tender', status='active', title='Same tender')
        closed = release('one', 'b', date, 'tenderUpdate', status='complete')
        import_package(self.con, package(active))
        self.assertEqual(import_package(self.con, package(closed))['conflicted'], 1)
        self.assertEqual(self.fields(), {'title': 'Same tender', 'conflicted_fields': ['status']})
        later = release('one', 'c', '2026-11-06T09:00:00-05:00', 'tenderCancellation')
        import_package(self.con, package(later))
        self.assertEqual(self.fields(), {'title': 'Same tender', 'status': 'cancelled'})

    def test_explicit_cancellation_controls_tied_active_status(self):
        date = '2026-11-05T09:00:00-05:00'
        cancelled = release('one', 'a', date, 'tenderCancellation')
        active = release('one', 'b', date, 'tender', status='active')
        import_package(self.con, package(active, cancelled))
        self.assertEqual(self.fields()['status'], 'cancelled')
