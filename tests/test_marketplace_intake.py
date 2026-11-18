import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from ci import marketplace_intake as m

REAL_REVIEWED = {
    'kind':'opportunity','platform':'upwork','method':'user_opened_listing',
    'source_url':'https://www.upwork.com/jobs/Research-Workflow-Automation_~022120652634932329378?utm_source=chatgpt.com',
    'title':'Research Workflow AI Automation',
    'observed_at':'2026-11-12T18:14:00-05:00',
    'budget_amount':750, 'currency':'UNVERIFIED',
    'connects_required':23,'competition':'50+ proposals','location':'Worldwide',
}
REAL_NOT_LEADS = [
    {'kind':'account_notification','platform':'upwork','method':'platform_alert','sender_domain':'t.upwork.com','title':'Your Profile is Approved and Published.','observed_at':'2026-11-12T23:03:19Z'},
    {'kind':'account_notification','platform':'charityvillage','method':'platform_alert','sender_domain':'charityvillage.com','title':'Activate your saved search at CharityVillage','observed_at':'2026-11-12T22:52:27Z'},
]

class TestMarketplaceIntake(unittest.TestCase):
    def test_real_curated_single_listing(self):
        x=m.normalize(REAL_REVIEWED)
        self.assertEqual(x['key'],'upwork:022120652634932329378')
        self.assertEqual(x['source_url'],'https://www.upwork.com/jobs/Research-Workflow-Automation_~022120652634932329378')
        self.assertEqual(x['application_state'],'PREPARE_ONLY')
        self.assertEqual(x['verification'],'REVIEW_REQUIRED')
        self.assertEqual(x['spend_gate'],'OWNER_APPROVAL_REQUIRED')
    def test_real_account_notifications_are_not_leads(self):
        ok, skipped=m.screen_batch(REAL_NOT_LEADS)
        self.assertEqual(len(ok),0)
        self.assertEqual(len(skipped),2)
    def test_reject_invalid_or_tracking_link(self):
        for url in ('https://evil.example/jobs/~022120652634932329378','http://www.upwork.com/jobs/~022120652634932329378','https://www.upwork.com/nx/find-work','https://track.example/click?id=1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.normalize(dict(REAL_REVIEWED,source_url=url))
    def test_alert_requires_sender_domain(self):
        with self.assertRaises(ValueError):
            m.normalize(dict(REAL_REVIEWED, method='platform_alert',sender_domain='bad.example'))
    def test_no_raw_email_or_applicant_data_persisted(self):
        rec=dict(REAL_REVIEWED,email_address='private@example.com',raw_email_body='secret',resume='private')
        x=m.normalize(rec)
        self.assertNotIn('email_address', x)
        self.assertNotIn('raw_email_body', x)
        self.assertNotIn('resume', x)
    def test_exact_replay_and_changed_listing(self):
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE notices(id TEXT PRIMARY KEY, title TEXT)')
            x=m.normalize(REAL_REVIEWED)
            self.assertEqual(m.import_records(con,[x])['added'],1)
            self.assertEqual(m.import_records(con,[x])['replayed'],1)
            y=m.normalize(dict(REAL_REVIEWED,competition='10-15 proposals',observed_at='2026-11-12T19:14:00-05:00'))
            self.assertEqual(m.import_records(con,[y])['revised'],1)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM marketplace_opportunities').fetchone()[0],1)
            self.assertEqual(con.execute('SELECT revision FROM marketplace_opportunities').fetchone()[0],2)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM marketplace_observations').fetchone()[0],2)
    def test_different_urls_same_job_id_dedup(self):
        alt=dict(REAL_REVIEWED,source_url='https://www.upwork.com/jobs/~022120652634932329378',observed_at='2026-11-12T20:14:00-05:00')
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE notices(id TEXT PRIMARY KEY, title TEXT)')
            m.import_records(con,[m.normalize(REAL_REVIEWED),m.normalize(alt)])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM marketplace_opportunities').fetchone()[0],1)
    def test_dry_run_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'alerts.json'; db=Path(tmp)/'nonexistent.sqlite3'
            src.write_text(json.dumps({'schema':m.SCHEMA,'records':[REAL_REVIEWED]+REAL_NOT_LEADS}))
            self.assertEqual(m.main([str(src),'--db',str(db)]),0)
            self.assertFalse(db.exists())
            with self.assertRaises(SystemExit):
                m.main([str(src),'--db',str(db),'--apply'])
            self.assertFalse(db.exists())
    def test_selection_exports_one_revision_and_only_review_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'existing.sqlite3'
            with closing(sqlite3.connect(db)) as con:
                con.execute('CREATE TABLE notices(id TEXT PRIMARY KEY)')
                item = m.normalize(REAL_REVIEWED)
                m.import_records(con, [item])
            result = m.selected_handoff(db, item['key'])
            self.assertEqual(result['run_mode'], 'dry_run')
            self.assertEqual(result['selection'], {
                'key': item['key'], 'revision': 1, 'fingerprint': item['fingerprint'],
                'verification': 'REVIEW_REQUIRED', 'eligibility': 'UNKNOWN',
                'application_state': 'PREPARE_ONLY', 'spend_gate': 'OWNER_APPROVAL_REQUIRED',
            })
            with self.assertRaises(ValueError):
                m.selected_handoff(db, 'upwork:missing')

    def test_opportunity_type_defaults_to_unknown(self):
        # A title containing "contract" does not establish contractor terms.
        unclassified = m.normalize(dict(REAL_REVIEWED, title='Contract web developer'))
        self.assertEqual(unclassified['opportunity_type'], 'UNKNOWN')
        self.assertEqual(unclassified['fingerprint'], m.normalize(dict(REAL_REVIEWED, title='Contract web developer', opportunity_type='UNKNOWN'))['fingerprint'])
        for invalid in ('FREELANCER', 'EMPLOYEE', 'business_project', '', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                m.normalize(dict(REAL_REVIEWED, opportunity_type=invalid))

    def test_job_individual_contract_and_business_project_share_db(self):
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE notices(id TEXT PRIMARY KEY)')
            types = ['JOB', 'INDIVIDUAL_CONTRACT', 'BUSINESS_PROJECT']
            items = [
                m.normalize(dict(
                    REAL_REVIEWED, opportunity_type=type_name,
                    source_url=f'https://www.upwork.com/jobs/~02212065263493232937{index}',
                ))
                for index, type_name in enumerate(types)
            ]
            self.assertEqual(m.import_records(con, items)['added'], 3)
            self.assertEqual(
                con.execute('SELECT opportunity_type FROM marketplace_opportunities ORDER BY key').fetchall(),
                [(name,) for name in types],
            )
            self.assertEqual(con.execute('SELECT COUNT(*) FROM notices').fetchone()[0], 0)
            self.assertEqual(m.import_records(con, items)['replayed'], 3)

    def test_classification_changed_only_when_record_revised(self):
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE notices(id TEXT PRIMARY KEY)')
            original = m.normalize(REAL_REVIEWED)
            self.assertEqual(m.import_records(con, [original])['added'], 1)
            self.assertEqual(con.execute('SELECT opportunity_type FROM marketplace_opportunities').fetchone()[0], 'UNKNOWN')
            typed = m.normalize(dict(REAL_REVIEWED, opportunity_type='BUSINESS_PROJECT',
                                     observed_at='2026-11-12T20:14:00-05:00'))
            self.assertEqual(m.import_records(con, [typed])['revised'], 1)
            self.assertEqual(con.execute('SELECT opportunity_type,revision FROM marketplace_opportunities').fetchone(),
                             ('BUSINESS_PROJECT', 2))
            # An older contradictory observation may be retained, but must not overwrite the current type.
            stale = m.normalize(dict(REAL_REVIEWED, opportunity_type='JOB',
                                     observed_at='2026-11-12T18:30:00-05:00'))
            self.assertEqual(m.import_records(con, [stale])['observed'], 1)
            self.assertEqual(con.execute('SELECT opportunity_type,revision FROM marketplace_opportunities').fetchone(),
                             ('BUSINESS_PROJECT', 2))

    def test_old_marketplace_table_migrates_without_dropping_prior_record(self):
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE notices(id TEXT PRIMARY KEY)')
            con.execute('''CREATE TABLE marketplace_opportunities (
                key TEXT PRIMARY KEY, platform TEXT NOT NULL, external_id TEXT NOT NULL,
                title TEXT NOT NULL, source_url TEXT NOT NULL, observed_at TEXT NOT NULL,
                method TEXT NOT NULL, budget_amount REAL, currency TEXT NOT NULL,
                connects_required INTEGER, competition TEXT NOT NULL, location TEXT NOT NULL,
                verification TEXT NOT NULL, eligibility TEXT NOT NULL,
                application_state TEXT NOT NULL, spend_gate TEXT NOT NULL,
                fingerprint TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1
            )''')
            x = m.normalize(REAL_REVIEWED)
            cols = ('key','platform','external_id','title','source_url','observed_at','method',
                    'budget_amount','currency','connects_required','competition','location',
                    'verification','eligibility','application_state','spend_gate','fingerprint')
            con.execute('INSERT INTO marketplace_opportunities (' + ','.join(cols) +
                        ',first_seen,last_seen) VALUES (' + ','.join('?' for _ in range(len(cols)+2)) + ')',
                        [x[k] for k in cols] + [x['observed_at'], x['observed_at']])
            self.assertEqual(m.import_records(con, [x])['observed'], 1)
            self.assertEqual(con.execute(
                'SELECT opportunity_type,fingerprint,revision FROM marketplace_opportunities'
            ).fetchone(), ('UNKNOWN', x['fingerprint'], 1))
            self.assertEqual(con.execute('SELECT COUNT(*) FROM marketplace_opportunities').fetchone()[0], 1)

    def test_apply_refuses_unrelated_database(self):
        with sqlite3.connect(':memory:') as con:
            with self.assertRaisesRegex(ValueError, 'existing Contract Intelligence database'):
                m.import_records(con, [m.normalize(REAL_REVIEWED)])
            self.assertFalse(con.in_transaction)
            self.assertEqual(con.execute('SELECT name FROM sqlite_master').fetchall(), [])

    def test_no_change_to_existing_procurement_table(self):
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE notices(id TEXT PRIMARY KEY, title TEXT)')
            con.execute("INSERT INTO notices VALUES('canadabuys:1','Original')")
            m.import_records(con,[m.normalize(REAL_REVIEWED)])
            self.assertEqual(con.execute("SELECT title FROM notices WHERE id='canadabuys:1'").fetchone()[0],'Original')

if __name__=='__main__':
    unittest.main()
