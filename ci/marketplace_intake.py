"""Bounded, offline marketplace opportunity intake. No scraping or sending.

Input is a human-reviewed or authorized feed's *sanitized* JSON record, never
raw message bodies or applicant data. This module does not read Gmail or n8n.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SCHEMA = 'ci.marketplace_intake.v1'
HANDOFF_SCHEMA = 'ci.marketplace_selection.v1'
HOSTS = {
    'upwork': ('upwork.com',),
    'contra': ('contra.com',),
    'charityvillage': ('charityvillage.com',),
    'indeed': ('indeed.com',),
    'linkedin': ('linkedin.com',),
}
# The URL must identify a listing, not an account or search page.
LISTING_PATHS = {
    'upwork': re.compile(r'^/jobs/(?:[^/]*_)?~\d+/?$'),
    'contra': re.compile(r'^/(?:opportunities|jobs)/[^/]+/?$'),
    'charityvillage': re.compile(r'^/(?:jobs|job|job-board)/[^/]+/?$'),
    'indeed': re.compile(r'^/(?:viewjob|rc/clk)/?$'),
    'linkedin': re.compile(r'^/jobs/view/\d+/?$'),
}
VALID_METHODS = {'user_opened_listing', 'authorized_api', 'platform_alert'}
# A temporary employee contract is JOB, not proof of independent contracting.
# Never infer a business relationship from a title containing "contract".
OPPORTUNITY_TYPES = {'JOB', 'INDIVIDUAL_CONTRACT', 'BUSINESS_PROJECT', 'UNKNOWN'}


def _is_host(host: str, domain: str) -> bool:
    return host == domain or host.endswith('.' + domain)


def _clean_url(platform: str, raw: object) -> str:
    if not isinstance(raw, str) or len(raw) > 1200:
        raise ValueError('missing or oversized direct listing URL')
    parts = urlsplit(raw.strip())
    host = (parts.hostname or '').lower()
    if parts.scheme != 'https' or not host or parts.username or parts.password or parts.port:
        raise ValueError('requires HTTPS listing URL without credentials or port')
    if not any(_is_host(host, d) for d in HOSTS[platform]):
        raise ValueError('listing URL is not on the claimed platform')
    if not LISTING_PATHS[platform].fullmatch(parts.path):
        raise ValueError('URL must point to an individual listing')
    # Never persist platform referral, email click-tracking or session parameters.
    # A few providers identify a listing with a query ID rather than a path ID.
    if platform == 'indeed':
        from urllib.parse import parse_qs, urlencode
        params = parse_qs(parts.query)
        key = (params.get('jk') or params.get('vjk') or [''])[0]
        if not re.fullmatch(r'[a-zA-Z0-9]{8,40}', key):
            raise ValueError('Indeed listing needs a stable job key')
        return urlunsplit(('https', host, '/viewjob', urlencode({'jk': key}), ''))
    return urlunsplit(('https', host, parts.path.rstrip('/'), '', ''))


def _clean_date(raw: object) -> str:
    if not isinstance(raw, str) or len(raw) > 40:
        raise ValueError('observed_at is required')
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('observation timestamp must include a timezone')
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    except ValueError as exc:
        raise ValueError('observed_at must be timezone-aware ISO 8601') from exc


def normalize(record: dict) -> dict:
    if not isinstance(record, dict) or record.get('kind') != 'opportunity':
        raise ValueError('not a classified opportunity (onboarding and alerts skipped)')
    platform = str(record.get('platform', '')).strip().lower()
    if platform not in HOSTS:
        raise ValueError('unapproved marketplace platform')
    method = record.get('method')
    if not isinstance(method, str) or method not in VALID_METHODS:
        raise ValueError('unapproved intake method')
    if method == 'platform_alert':
        sender_domain = str(record.get('sender_domain', '')).lower().strip()
        if not any(_is_host(sender_domain, d) for d in HOSTS[platform]):
            raise ValueError('alert sender domain must match platform (still unverified)')
    title = record.get('title')
    if not isinstance(title, str) or not 6 <= len(title.strip()) <= 240:
        raise ValueError('missing or invalid listing title')
    url = _clean_url(platform, record.get('source_url'))
    observed_at = _clean_date(record.get('observed_at'))
    if platform == 'upwork':
        external_id = re.search(r'~(\d+)$', url).group(1)
    else:
        external_id = str(record.get('external_id', '')).strip()
        if not re.fullmatch(r'[\w.-]{4,120}', external_id):
            raise ValueError('stable external_id required for this platform')
    price = record.get('budget_amount')
    if price is not None:
        if type(price) not in (int, float) or not 0 <= price <= 1_000_000_000:
            raise ValueError('budget_amount must be an unsigned numeric value')
    currency = str(record.get('currency', 'unverified')).upper().strip()
    if currency not in {'USD', 'CAD', 'UNVERIFIED'}:
        raise ValueError('currency must be USD, CAD or unverified')
    connects = record.get('connects_required')
    if connects is not None and (type(connects) is not int or not 0 <= connects <= 1000):
        raise ValueError('connects_required must be nonnegative integer')
    competition = str(record.get('competition', 'unknown'))[:80]
    location = str(record.get('location', 'unknown'))[:120]
    opportunity_type = record.get('opportunity_type', 'UNKNOWN')
    if not isinstance(opportunity_type, str) or opportunity_type not in OPPORTUNITY_TYPES:
        raise ValueError('opportunity_type must be JOB, INDIVIDUAL_CONTRACT, BUSINESS_PROJECT or UNKNOWN')
    item = {
        'key': f'{platform}:{external_id}',
        'platform': platform,
        'external_id': external_id,
        'title': title.strip(),
        'source_url': url,
        'observed_at': observed_at,
        'method': method,
        'budget_amount': price,
        'currency': currency,
        'connects_required': connects,
        'competition': competition,
        'location': location,
        'opportunity_type': opportunity_type,
        # A platform alert is evidence for discovery, NOT source, eligibility or budget verification.
        'verification': 'REVIEW_REQUIRED',
        'eligibility': 'UNKNOWN',
        'application_state': 'PREPARE_ONLY',
        'spend_gate': 'OWNER_APPROVAL_REQUIRED' if connects else 'CHECK_BEFORE_APPLYING',
    }
    # Preserve v1 fingerprints when type is not known; avoid spurious revision
    # of pre-existing, unclassified marketplace records during migration.
    material = {k: v for k, v in item.items()
                if k not in ('observed_at', 'method')
                and not (k == 'opportunity_type' and v == 'UNKNOWN')}
    item['fingerprint'] = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return item


def screen_batch(records: list) -> tuple[list, list]:
    accepted, skipped = [], []
    for i, raw in enumerate(records):
        try:
            accepted.append(normalize(raw))
        except (ValueError, TypeError) as exc:
            skipped.append({'index': i, 'reason': str(exc)})
    return accepted, skipped


def ensure_schema(con: sqlite3.Connection) -> None:
    ddl = '''
    CREATE TABLE IF NOT EXISTS marketplace_opportunities (
        key TEXT PRIMARY KEY, platform TEXT NOT NULL, external_id TEXT NOT NULL,
        title TEXT NOT NULL, source_url TEXT NOT NULL, observed_at TEXT NOT NULL,
        method TEXT NOT NULL, budget_amount REAL, currency TEXT NOT NULL,
        connects_required INTEGER, competition TEXT NOT NULL, location TEXT NOT NULL,
        verification TEXT NOT NULL, eligibility TEXT NOT NULL,
        application_state TEXT NOT NULL CHECK(application_state='PREPARE_ONLY'),
        spend_gate TEXT NOT NULL, fingerprint TEXT NOT NULL,
        opportunity_type TEXT NOT NULL DEFAULT 'UNKNOWN'
            CHECK(opportunity_type IN ('JOB', 'INDIVIDUAL_CONTRACT', 'BUSINESS_PROJECT', 'UNKNOWN')),
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS marketplace_observations (
        observation_key TEXT PRIMARY KEY, opportunity_key TEXT NOT NULL,
        observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
        FOREIGN KEY(opportunity_key) REFERENCES marketplace_opportunities(key)
    );
    '''
    for statement in ddl.strip().rstrip(';').split(';'):
        con.execute(statement)
    # Existing local CI databases may already contain v1 marketplace rows.
    # Add only the new column; never rebuild the table or touch procurement.
    columns = {row[1] for row in con.execute('PRAGMA table_info(marketplace_opportunities)')}
    if 'opportunity_type' not in columns:
        con.execute(
            "ALTER TABLE marketplace_opportunities ADD COLUMN opportunity_type TEXT NOT NULL "
            "DEFAULT 'UNKNOWN' CHECK(opportunity_type IN "
            "('JOB', 'INDIVIDUAL_CONTRACT', 'BUSINESS_PROJECT', 'UNKNOWN'))"
        )


def import_records(con: sqlite3.Connection, items: list[dict]) -> dict:
    with con:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        if not items:
            return {'added': 0, 'revised': 0, 'observed': 0, 'replayed': 0}
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notices'").fetchone():
            raise ValueError('expected an existing Contract Intelligence database with a notices table')
        ensure_schema(con)
        return _import_records(con, items)


def _import_records(con: sqlite3.Connection, items: list[dict]) -> dict:
    totals = {'added': 0, 'revised': 0, 'observed': 0, 'replayed': 0}
    lookup = con.cursor()
    lookup.row_factory = sqlite3.Row
    for item in items:
        obs = hashlib.sha256((item['key'] + '\n' + item['observed_at'] + '\n' + item['fingerprint']).encode()).hexdigest()
        if con.execute('SELECT 1 FROM marketplace_observations WHERE observation_key=?', (obs,)).fetchone():
            totals['replayed'] += 1
            continue
        exists = lookup.execute('SELECT fingerprint, revision, last_seen FROM marketplace_opportunities WHERE key=?', (item['key'],)).fetchone()
        values = [item[k] for k in ('key','platform','external_id','title','source_url','observed_at','method','budget_amount','currency','connects_required','competition','location','verification','eligibility','application_state','spend_gate','fingerprint','opportunity_type')]
        if exists is None:
            con.execute('INSERT INTO marketplace_opportunities (key,platform,external_id,title,source_url,observed_at,method,budget_amount,currency,connects_required,competition,location,verification,eligibility,application_state,spend_gate,fingerprint,opportunity_type,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values + [item['observed_at'], item['observed_at']])
            totals['added'] += 1
        elif exists['fingerprint'] != item['fingerprint'] and item['observed_at'] >= exists['last_seen']:
            con.execute('UPDATE marketplace_opportunities SET title=?,source_url=?,observed_at=?,method=?,budget_amount=?,currency=?,connects_required=?,competition=?,location=?,spend_gate=?,fingerprint=?,opportunity_type=?,last_seen=?,revision=? WHERE key=?', (item['title'],item['source_url'],item['observed_at'],item['method'],item['budget_amount'],item['currency'],item['connects_required'],item['competition'],item['location'],item['spend_gate'],item['fingerprint'],item['opportunity_type'],item['observed_at'],exists['revision']+1,item['key']))
            totals['revised'] += 1
        else:
            con.execute('UPDATE marketplace_opportunities SET last_seen=MAX(last_seen,?) WHERE key=?', (item['observed_at'], item['key']))
            totals['observed'] += 1
        con.execute('INSERT INTO marketplace_observations (observation_key,opportunity_key,observed_at,fingerprint) VALUES (?,?,?,?)', (obs,item['key'],item['observed_at'],item['fingerprint']))
    return totals


def selected_handoff(db: Path, key: str) -> dict:
    """Read one manually selected current revision from the existing database."""
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        row = con.execute(
            'SELECT key, revision, fingerprint, verification, eligibility, application_state, spend_gate '
            'FROM marketplace_opportunities WHERE key=?', (key,)
        ).fetchone()
    if row is None:
        raise ValueError('selected marketplace key was not found')
    fields = ('key', 'revision', 'fingerprint', 'verification', 'eligibility', 'application_state', 'spend_gate')
    return {'schema': HANDOFF_SCHEMA, 'run_mode': 'dry_run', 'selection': dict(zip(fields, row))}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Manual/authorized marketplace alerts → existing CI SQLite (NO sending)')
    p.add_argument('input_json', nargs='?', type=Path, help='Sanitized records; no raw emails, credentials or applicant data')
    p.add_argument('--db', type=Path, default=Path('data/contracts.sqlite3'))
    p.add_argument('--apply', action='store_true', help='Explicitly add screened records to EXISTING local DB')
    p.add_argument('--select', metavar='PLATFORM:ID', help='Export one stored lead for manual review')
    args = p.parse_args(argv)
    if args.select is not None:
        if args.input_json or args.apply:
            p.error('--select is read-only and cannot be combined with input_json or --apply')
        try:
            report = selected_handoff(args.db, args.select)
        except (sqlite3.Error, ValueError) as exc:
            p.error(str(exc))
        print(json.dumps(report, indent=2))
        return 0
    if args.input_json is None:
        p.error('provide input_json or --select')
    payload = json.loads(args.input_json.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or payload.get('schema') != SCHEMA or not isinstance(payload.get('records'), list):
        p.error('expected ci.marketplace_intake.v1 with records array')
    items, skipped = screen_batch(payload['records'])
    report = {'mode': 'apply' if args.apply else 'dry_run', 'accepted': len(items), 'skipped': skipped,
              'keys': [x['key'] for x in items], 'application_state': 'PREPARE_ONLY'}
    if args.apply:
        if not args.db.is_file():
            p.error('DB does not exist: refusing to create or replace the existing Contract Intelligence database')
        try:
            with closing(sqlite3.connect(args.db.resolve().as_uri() + '?mode=rw', uri=True)) as con:
                report.update(import_records(con, items))
        except (sqlite3.Error, ValueError) as exc:
            p.error(str(exc))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
