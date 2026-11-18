"""Source-preserving contract ingestion and explainable, non-predictive screening."""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from functools import lru_cache
from statistics import median
import hashlib
import io
import json
import os
import re
import sqlite3
import urllib.request
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode, urlsplit
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get('CI_DB_PATH', ROOT / 'data' / 'contracts.sqlite3'))
OFFICIAL = {
    'canadabuys-open': 'https://canadabuys.canada.ca/opendata/pub/openTenderNotice-ouvertAvisAppelOffres.csv',
    'canadabuys-new': 'https://canadabuys.canada.ca/opendata/pub/newTenderNotice-nouvelAvisAppelOffres.csv',
}
SOURCE_LABELS = {
    'canadabuys-open': 'CanadaBuys federal — open notices',
    'canadabuys-new': 'CanadaBuys federal — new notices',
    'manual': 'Curated research — unverified since last review',
    'demo': 'DEMONSTRATION — fictional notices',
}
CANADABUYS_NOTICE_PAGE = 'https://canadabuys.canada.ca/en/tender-opportunities/tender-notice/'
# Only reference formats whose public page path is the reference itself (observed on canadabuys.canada.ca).
# PW-/SSC- legacy references are not derivable reliably and stay without a link.
CANADABUYS_PAGE_REF = re.compile(r'cb-\d+-\d+|WS\d+-Doc\d+')
MAX_DOWNLOAD = 30 * 1024 * 1024
FIXED_CANADABUYS_TZ = timezone(timedelta(hours=-5))  # Per PSPC published dataset, not DST-adjusted local time
NOW = lambda: datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript('''
    CREATE TABLE IF NOT EXISTS notices (
        id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_key TEXT NOT NULL,
        title TEXT NOT NULL, buyer TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '', solicitation TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'unknown', category TEXT NOT NULL DEFAULT '',
        published_at TEXT NOT NULL DEFAULT '', close_raw TEXT NOT NULL DEFAULT '',
        closing_utc TEXT, source_url TEXT NOT NULL DEFAULT '',
        provenance TEXT NOT NULL DEFAULT '', fiscal_budget REAL, currency TEXT NOT NULL DEFAULT '',
        budget_basis TEXT NOT NULL DEFAULT '', eligibility TEXT NOT NULL DEFAULT 'unreviewed',
        user_stage TEXT NOT NULL DEFAULT '', user_notes TEXT NOT NULL DEFAULT '',
        linked_issue TEXT NOT NULL DEFAULT '', raw_json TEXT NOT NULL DEFAULT '{}',
        row_hash TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        UNIQUE(source_id, source_key)
    );
    CREATE TABLE IF NOT EXISTS notice_versions (
        version_id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id TEXT NOT NULL,
        observed_at TEXT NOT NULL, row_hash TEXT NOT NULL, change_type TEXT NOT NULL,
        old_value TEXT, new_value TEXT,
        FOREIGN KEY(notice_id) REFERENCES notices(id)
    );
    CREATE INDEX IF NOT EXISTS idx_notice_source ON notices(source_id);
    CREATE INDEX IF NOT EXISTS idx_notice_close ON notices(closing_utc);
    CREATE INDEX IF NOT EXISTS idx_notice_status ON notices(status);
    CREATE INDEX IF NOT EXISTS idx_notice_version ON notice_versions(notice_id);
    CREATE TABLE IF NOT EXISTS ingest_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
        started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
        added INTEGER NOT NULL DEFAULT 0, changed INTEGER NOT NULL DEFAULT 0,
        unchanged INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
        rows_total INTEGER NOT NULL DEFAULT 0, bytes_total INTEGER NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT ''
    );
    ''')
    con.executescript('''
    CREATE TABLE IF NOT EXISTS feed_snapshots (
        run_id INTEGER PRIMARY KEY REFERENCES ingest_runs(run_id),
        published_utc TEXT, sha256 TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS feed_presence (
        feed TEXT NOT NULL, notice_id TEXT NOT NULL REFERENCES notices(id),
        run_id INTEGER NOT NULL REFERENCES feed_snapshots(run_id),
        PRIMARY KEY(feed, notice_id)
    );
    CREATE TABLE IF NOT EXISTS notice_reviews (
        notice_id TEXT PRIMARY KEY REFERENCES notices(id),
        revision TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    ''')
    return con


def read_field(row: dict, *aliases: str) -> str:
    """Resolve exact bilingual names or safe known prefix variants, tolerating documentation spelling changes."""
    for name in aliases:
        if name in row and str(row[name] or '').strip():
            return str(row[name]).strip()
    for name in aliases:
        for k, v in row.items():
            if k and k.lower().startswith(name.lower()) and str(v or '').strip():
                return str(v).strip()
    return ''


def closing_to_utc(raw: str) -> str | None:
    """CanadaBuys data dictionary specifies UTC-05:00 regardless of DST. Never invent a time for a date-only value."""
    raw = (raw or '').strip()
    if not raw or raw in ('-', 'N/A'):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            if not ('T' in raw or ' ' in raw):
                return None
            dt = dt.replace(tzinfo=FIXED_CANADABUYS_TZ)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M', '%m/%d/%Y %H:%M:%S'):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=FIXED_CANADABUYS_TZ).astimezone(timezone.utc).isoformat(timespec='seconds')
        except ValueError:
            continue
    return None


def canadabuys_notice_url(ref: str) -> str:
    """Public CanadaBuys tender-notice page for a reference, or '' when the format is not known to map."""
    ref = (ref or '').strip()
    return CANADABUYS_NOTICE_PAGE + quote(ref) if CANADABUYS_PAGE_REF.fullmatch(ref) else ''


def normalized_from_csv(row: dict, source: str) -> dict | None:
    ref = read_field(row, 'referenceNumber-numeroReference', 'referenceNumber')
    title = read_field(row, 'title-titre-eng', 'title-titre-fra')
    if not ref or not title:
        return None
    closing = read_field(row, 'tenderClosingDate-appelOffresDateCloture', 'tenderClosingDate-appelOffresdateCloture', 'tenderClosingDate')
    status = read_field(row, 'tenderStatus-tenderStatut-eng', 'tenderStatus-appelOffresStatut-eng', 'tenderStatus')
    notice = {
        'id': f'{"demo" if source == "demo" else "canadabuys"}:{ref}', 'source_id': 'demo' if source == 'demo' else 'canadabuys', 'source_key': ref,
        'title': title, 'buyer': read_field(row, 'contractingEntityName-nomEntitContractante-eng', 'contractingEntityName-nomEntitContractante-fra'),
        'description': read_field(row, 'tenderDescription-descriptionAppelOffres-eng', 'tenderDescription-descriptionAppelOffres-fra'),
        'solicitation': read_field(row, 'solicitationNumber-numeroSollicitation', 'solicitationNumber'),
        'status': status.lower() if status else 'unknown',
        'category': read_field(row, 'procurementCategory-categorieApprovisionnement'),
        'published_at': read_field(row, 'publicationDate-datePublication'),
        'close_raw': closing, 'closing_utc': closing_to_utc(closing),
        # The CSV leaves noticeURL empty for notices hosted on CanadaBuys itself; link their public page instead.
        'source_url': read_field(row, 'noticeURL-URLavis-eng', 'noticeURL-URLavis-fra')
                      or ('' if source == 'demo' else canadabuys_notice_url(ref)),
        'provenance': 'FICTIONAL FIXTURE — not a live contract' if source == 'demo' else 'CanadaBuys federal CSV; buyer notice and amendments control',
        'fiscal_budget': None, 'currency': '', 'budget_basis': '',
        'eligibility': 'unreviewed', 'user_stage': '', 'user_notes': '', 'linked_issue': '',
        'raw_json': json.dumps(row, ensure_ascii=False, sort_keys=True),
    }
    return notice


SOURCE_FIELDS = ('title', 'buyer', 'description', 'solicitation', 'status', 'category',
                 'published_at', 'close_raw', 'closing_utc', 'source_url', 'provenance', 'raw_json')


def backfill_source_urls(con: sqlite3.Connection, apply: bool = False) -> dict:
    """Fill empty CanadaBuys source_url values that can be derived from the reference alone.

    Never overwrites a stored URL, never touches raw_json/row_hash, and creates no notice_versions,
    so downstream change detection sees no new events. Dry run unless apply=True."""
    counts = dict(missing=0, derivable=0, updated=0, not_derivable=0)
    rows = con.execute("SELECT id, source_key, raw_json FROM notices WHERE source_id='canadabuys' AND source_url=''").fetchall()
    updates = []
    for row in rows:
        counts['missing'] += 1
        raw = json.loads(row['raw_json'] or '{}')
        url = read_field(raw, 'noticeURL-URLavis-eng', 'noticeURL-URLavis-fra') or canadabuys_notice_url(row['source_key'])
        if safe_url(url):
            counts['derivable'] += 1
            updates.append((url, row['id']))
        else:
            counts['not_derivable'] += 1
    if apply and updates:
        with con:
            counts['updated'] = sum(con.execute("UPDATE notices SET source_url=? WHERE id=? AND source_url=''", u).rowcount
                                    for u in updates)
    return counts


def hash_snapshot(notice: dict) -> str:
    return hashlib.sha256(notice['raw_json'].encode('utf8')).hexdigest()


def amendment(raw: dict) -> str:
    return read_field(raw, 'amendmentNumber-numeroModification')


def earlier_revision(incoming: dict, previous: dict) -> bool:
    new_number, old_number = amendment(incoming), amendment(previous)
    if new_number.isdecimal() and old_number.isdecimal() and int(new_number) != int(old_number):
        return int(new_number) < int(old_number)
    new_date = read_field(incoming, 'amendmentDate-dateModification')
    old_date = read_field(previous, 'amendmentDate-dateModification')
    return bool(new_date and old_date and new_date < old_date)


def upsert(con: sqlite3.Connection, notice: dict, source_time=None, verified=False) -> str:
    clock = NOW()
    n = dict(notice, row_hash=hash_snapshot(notice), last_seen=clock)
    previous = con.execute('SELECT * FROM notices WHERE id=?', (n['id'],)).fetchone()
    if previous and previous['row_hash'] == n['row_hash']:
        con.execute('UPDATE notices SET last_seen=? WHERE id=?', (clock, n['id']))
        return 'unchanged'
    if previous:
        old_raw, new_raw = json.loads(previous['raw_json']), json.loads(n['raw_json'])
        previous_snapshot = con.execute('''
            SELECT MAX(s.published_utc) AS published, COUNT(*) AS count
            FROM feed_presence p JOIN feed_snapshots s USING(run_id) WHERE notice_id=?
        ''', (n['id'],)).fetchone()
        # A local file must not replace verified data, or revive an older amendment.
        if previous_snapshot['count'] and not verified:
            return 'skipped'
        published = previous_snapshot['published']
        if earlier_revision(new_raw, old_raw) or (
            source_time and published and source_time < published
            and not earlier_revision(old_raw, new_raw)
        ):
            return 'skipped'
        fields = SOURCE_FIELDS + ('row_hash', 'last_seen')
        con.execute('UPDATE notices SET ' + ', '.join(f'{field}=?' for field in fields) + ' WHERE id=?',
                    tuple(n[field] for field in fields) + (n['id'],))
    else:
        n['first_seen'] = clock
        fields = tuple(n)
        con.execute('INSERT INTO notices (' + ','.join(fields) + ') VALUES (' +
                    ','.join('?' for _ in fields) + ')', tuple(n.values()))
    con.execute('''
        INSERT INTO notice_versions(notice_id,observed_at,row_hash,change_type,old_value,new_value)
        VALUES(?,?,?,?,?,?)
    ''', (n['id'], clock, n['row_hash'], 'changed' if previous else 'created',
          previous['raw_json'] if previous else None, n['raw_json']))
    return 'changed' if previous else 'added'


def import_csv_text(con: sqlite3.Connection, text: str, feed: str = 'canadabuys-open',
                    *, verified: bool = False, published_utc: str | None = None) -> dict:
    if feed not in OFFICIAL and feed != 'demo':
        raise ValueError('Unsupported feed')
    if verified and feed not in OFFICIAL:
        raise ValueError('Only official feeds can record verified snapshots')
    body = text.encode('utf-8')
    counts = dict(added=0, changed=0, unchanged=0, skipped=0, rows_total=0)
    started = NOW()
    try:
        csv.field_size_limit(8 * 1024 * 1024)
        reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff'), newline=''), strict=True)
        required = {'referenceNumber-numeroReference', 'title-titre-eng', 'publicationDate-datePublication'}
        if verified:
            required.update(('amendmentNumber-numeroModification', 'amendmentDate-dateModification',
                             'tenderClosingDate-appelOffresDateCloture',
                             'tenderStatus-appelOffresStatut-eng', 'attachment-piecesJointes-eng',
                             'contractingEntityName-nomEntitContractante-eng',
                             'solicitationNumber-numeroSollicitation', 'noticeURL-URLavis-eng',
                             'tenderDescription-descriptionAppelOffres-eng'))
        headers = reader.fieldnames or []
        if missing := required.difference(headers):
            raise ValueError('Unrecognized CSV schema: missing required columns ' + ', '.join(sorted(missing)))
        if len(set(headers)) != len(headers):
            raise ValueError('Duplicate CSV columns')
        with con:
            con.execute('BEGIN IMMEDIATE')
            run = con.execute(
                'INSERT INTO ingest_runs(source_id,started_at,status,bytes_total) VALUES(?,?,?,?)',
                (feed, started, 'running', len(body))).lastrowid
            if verified:
                latest = con.execute('''
                    SELECT MAX(published_utc) FROM feed_snapshots JOIN ingest_runs USING(run_id)
                    WHERE source_id=? AND status='success'
                ''', (feed,)).fetchone()[0]
                if latest and published_utc and published_utc < latest:
                    raise ValueError('Source published an older snapshot than the last successful refresh')
                con.execute('INSERT INTO feed_snapshots VALUES (?,?,?)',
                            (run, published_utc, hashlib.sha256(body).hexdigest()))
            seen = set()
            for row in reader:
                counts['rows_total'] += 1
                if None in row or None in row.values():
                    raise ValueError(f'CSV column count mismatch at line {reader.line_num}')
                notice = normalized_from_csv(row, feed)
                if not notice:
                    if verified:
                        raise ValueError(f'Missing reference or title at line {reader.line_num}')
                    counts['skipped'] += 1
                    continue
                if verified and notice['id'] in seen:
                    raise ValueError(f'Duplicate source reference at line {reader.line_num}')
                seen.add(notice['id'])
                outcome = upsert(con, notice, published_utc, verified)
                counts[outcome] += 1
                if verified:
                    con.execute('''
                        INSERT INTO feed_presence VALUES(?,?,?)
                        ON CONFLICT(feed,notice_id) DO UPDATE SET run_id=excluded.run_id
                        WHERE excluded.run_id > feed_presence.run_id
                    ''', (feed, notice['id'], run))
            if verified and feed == 'canadabuys-open' and not seen:
                raise ValueError('The open snapshot is empty')
            con.execute('''
                UPDATE ingest_runs SET finished_at=?,status='success',added=?,changed=?,unchanged=?,
                    skipped=?,rows_total=?,message=? WHERE run_id=?
            ''', (NOW(), *counts.values(),
                  'Official HTTPS snapshot' if verified else 'Local file; current status requires verification', run))
    except Exception as exc:
        with con:
            con.execute('''
                INSERT INTO ingest_runs(source_id,started_at,finished_at,status,bytes_total,message)
                VALUES(?,?,?,'failed',?,?)
            ''', (feed, started, NOW(), len(body), str(exc)[:1000]))
        raise
    return dict(counts, run_id=run, source=feed)


def fetch_feed(feed: str, timeout: int = 45) -> tuple[str, str | None]:
    req = urllib.request.Request(OFFICIAL[feed], headers={
        'User-Agent': 'ContractIntelligence/0.2', 'Accept': 'text/csv,*/*',
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise ValueError('Download exceeds the 30 MiB feed limit')
        modified = response.headers.get('Last-Modified')
        published = parsedate_to_datetime(modified).astimezone(timezone.utc).isoformat() if modified else None
    return data.decode('utf-8'), published


def refresh_feed(con, feed):
    started = NOW()
    try:
        body, published = fetch_feed(feed)
    except Exception as exc:
        with con:
            con.execute('''
                INSERT INTO ingest_runs(source_id,started_at,finished_at,status,message)
                VALUES(?,?,?,'failed',?)
            ''', (feed, started, NOW(), str(exc)[:1000]))
        raise
    return import_csv_text(con, body, feed, verified=True, published_utc=published)


def import_manual(con: sqlite3.Connection, path: Path) -> dict:
    records=json.loads(path.read_text(encoding='utf-8'))
    added=changed=unchanged=0
    for item in records:
        deadline=item.get('closing_local','')
        n=dict(
            id='manual:'+item['id'], source_id='manual', source_key=item['id'], title=item['title'], buyer=item['buyer'],
            description=item.get('description',''), solicitation=item.get('solicitation',''),status='needs source recheck',
            category=item.get('category',''),published_at='',close_raw=deadline,closing_utc=(datetime.fromisoformat(deadline).astimezone(timezone.utc).isoformat(timespec='seconds') if deadline and ('+' in deadline[10:] or '-' in deadline[10:]) else None),
            source_url=item.get('buyer_url',''),provenance='Prior user-reviewed contract research; live status NOT checked by this app',
            fiscal_budget=item.get('budget'),currency=item.get('currency','CAD'),budget_basis=item.get('budget_basis',''),
            eligibility=item.get('eligibility','unknown'),user_stage=item.get('stage',''),user_notes=item.get('notes',''),
            linked_issue=item.get('issue_url',''),raw_json=json.dumps(item,sort_keys=True,ensure_ascii=False))
        with con:
            outcome=upsert(con,n)
            if outcome=='added':added+=1
            elif outcome=='changed':changed+=1
            else:unchanged+=1
    return {'added':added,'changed':changed,'unchanged':unchanged,'source':'manual'}


SERVICE = {
    'website / CMS': ('wordpress', 'squarespace', 'website redesign', 'website redevelopment',
                      'web development', 'web design', 'content management', 'cms migration',
                      'digital portal', 'membership portal', 'web application'),
    'integrations / workflows': ('api integration', 'systems integration', 'software integration',
                                 'workflow automation', 'process automation', 'business automation',
                                 'application development', 'software development', 'full stack',
                                 'full-stack', 'data migration', 'business analysis', 'it professional services'),
    'data / analytics': ('data analysis', 'data analytics', 'data platform', 'dashboard', 'power bi',
                         'business intelligence', 'data reporting', 'data engineering',
                         'data visualization', 'data architecture'),
    'accessibility / UX': ('digital accessibility', 'accessibility remediation', 'wcag',
                             'user experience', 'ux design', 'usability testing', 'user research'),
    'AI governance': ('ai governance','artificial intelligence governance','model risk','ai assurance','machine learning governance','ai decision-making','algorithmic impact')
}


GATES = {
    'qualifications': 'Mandatory bidder qualifications',
    'experience': 'Required experience',
    'references': 'Client references',
    'insurance': 'Insurance',
    'security': 'Security clearance',
    'remote': 'Remote work and delivery geography',
    'capacity': 'Delivery capacity and mandatory scope',
}
GATE_STATES = {'unknown', 'met', 'not_met', 'partner_possible', 'not_applicable'}
STAGES = {'', 'saved', 'evaluating', 'bid', 'no_bid'}
COMPLEXITIES = {'unknown', 'low', 'moderate', 'high'}
FRESHNESS = timedelta(hours=48)


def eligibility_state(n):
    legacy = n.get('eligibility') or 'unreviewed'
    review = n.get('review', {})
    if not review:
        return legacy
    if n['review_revision'] != n['row_hash']:
        return 'unknown'
    states = {item['state'] for item in review['gates'].values()}
    if 'not_met' in states:
        return 'not_met'
    if 'partner_possible' in states:
        return 'partner_possible'
    if states <= {'met', 'not_applicable'}:
        return 'met'
    return 'unknown'


def opportunity_state(n, now):
    if n['source_id'] == 'demo':
        return 'demo'
    if n['source_id'] == 'manual':
        return 'historical'
    closing = datetime.fromisoformat(n['closing_utc']) if n['closing_utc'] else None
    if n['status'] in {'expired', 'cancelled', 'canceled', 'closed'} or (closing and closing <= now):
        return 'historical'
    snapshot = n.get('snapshot_at')
    if (n['status'] == 'open' and closing and snapshot
            and timedelta(0) <= now - datetime.fromisoformat(snapshot) <= FRESHNESS):
        return 'active'
    return 'unverified'


SERVICE_PATTERNS = {
    group: {word: re.compile(r'(?<!\w)' + re.escape(word) + r'(?!\w)', re.I) for word in words}
    for group, words in SERVICE.items()
}


@lru_cache(maxsize=2048)
def service_matches(title, description):
    matches, points = {}, 0
    for group, patterns in SERVICE_PATTERNS.items():
        title_hits = [word for word, pattern in patterns.items() if pattern.search(title)]
        scope_hits = [word for word, pattern in patterns.items()
                      if word not in title_hits and pattern.search(description)]
        if title_hits or scope_hits:
            matches[group] = title_hits + scope_hits
            points += min(24, 18 * bool(title_hits) + 6 * bool(scope_hits) + 3 * len(title_hits))
    return matches, min(65, points)


RESTRICTED_INVITATION = re.compile(
    r"only (?:open to (?:the below list of pre-qualified suppliers|those.{0,100}supply arrangement holders)|"
    r"(?:selected|indigenous) [a-z ]{0,45}(?:sa|supply arrangement) holders)|"
    r"(?:open only to|open to only)\s+.{0,100}(?:supply arrangement|sa holders)",
    re.I,
)


def restricted_route(n):
    return n.get('source_id') == 'canadabuys' and (
        n.get('notice_type') == 'RFP against Supply Arrangement'
        or bool(RESTRICTED_INVITATION.search(n.get('description', '')))
    )


def score_notice(n: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    state = opportunity_state(n, now)
    eligibility = eligibility_state(n)
    restricted = restricted_route(n)
    matches, technical = service_matches(n.get('title', ''), n.get('description', ''))
    category_fit = not (set(n.get('categories', [])) <= {'Construction', 'Goods'} and n.get('categories'))
    if not category_fit and technical < 18:
        matches, technical = {}, 0
    closing = n.get('closing_utc')
    days = (datetime.fromisoformat(closing) - now).total_seconds() / 86400 if closing else None
    window = 15 if days is not None and days >= 14 else 8 if days is not None and days >= 7 else 0
    review = n.get('review', {}) if n.get('review_revision') == n.get('row_hash') else {}
    revenue, cost = review.get('revenue'), review.get('cost')
    profit = round(revenue - cost, 2) if revenue is not None and cost is not None else None
    capacity = review.get('gates', {}).get('capacity', {}).get('state')
    complexity = review.get('complexity')
    delivery = {'low': 10, 'moderate': 6, 'high': 2}.get(complexity) if capacity in {'met', 'not_applicable'} else None
    ceiling = review.get('budget')
    if profit is None:
        economics, economics_note = None, 'Estimated revenue and delivery cost are required'
    elif ceiling is not None and revenue > ceiling:
        economics, economics_note = 0, 'Estimated revenue exceeds the recorded ceiling'
    elif profit <= 0:
        economics, economics_note = 0, 'Estimated profit is zero or negative'
    elif ceiling is None:
        economics, economics_note = 6, 'Positive estimated profit with an unknown ceiling'
    else:
        economics, economics_note = 10, 'Positive estimated profit within the recorded ceiling'
    components = {'Service fit': technical, 'Response window': window if days is not None else None,
                  'Delivery': delivery, 'Economics': economics}
    score = sum(points for points in components.values() if points is not None)

    if eligibility == 'not_met':
        recommendation = 'Not eligible as solo bidder'
    elif state == 'demo':
        recommendation = 'Fictional demonstration'
    elif state == 'historical':
        recommendation = 'Historical record'
    elif state != 'active':
        recommendation = 'Current status needs verification'
    elif restricted and eligibility != 'met':
        recommendation = 'Restricted bid route — verify prequalification'
    elif eligibility == 'partner_possible':
        recommendation = 'Partner route needs review'
    elif eligibility != 'met':
        recommendation = 'Needs mandatory eligibility review'
    else:
        recommendation = 'Mandatory review complete'

    return dict(score=score, components=components, matches=matches, recommendation=recommendation,
                days_left=round(days, 1) if days is not None else None, record_state=state,
                eligibility=eligibility, restricted_route=restricted, estimated_profit=profit, economics_note=economics_note,
                provisional=eligibility != 'met' or state != 'active')


DIRECTORY_SQL = """
    WITH latest AS (
        SELECT source_id, MAX(run_id) AS run_id
        FROM feed_snapshots JOIN ingest_runs USING(run_id) GROUP BY source_id
    )
    SELECT n.*, r.revision AS review_revision, r.payload AS review_json, r.updated_at AS review_updated_at,
        MAX(
            COALESCE(CASE WHEN p.run_id = lo.run_id THEN os.published_utc END, ''),
            COALESCE(CASE WHEN np.run_id = ln.run_id AND
                (os.published_utc IS NULL OR ns.published_utc > os.published_utc)
                THEN ns.published_utc END, '')
        ) AS snapshot_at
    FROM notices n
    LEFT JOIN notice_reviews r ON r.notice_id=n.id
    LEFT JOIN latest lo ON lo.source_id='canadabuys-open'
    LEFT JOIN latest ln ON ln.source_id='canadabuys-new'
    LEFT JOIN feed_presence p ON p.notice_id=n.id AND p.feed=lo.source_id
    LEFT JOIN feed_presence np ON np.notice_id=n.id AND np.feed=ln.source_id
    LEFT JOIN feed_snapshots os ON os.run_id=lo.run_id
    LEFT JOIN feed_snapshots ns ON ns.run_id=ln.run_id
"""
CATEGORIES = {'GD': 'Goods', 'SRV': 'Services', 'CNST': 'Construction', 'SVRTGD': 'Services related to goods',
              'SRVTGD': 'Services related to goods'}


def category_names(value):
    codes = value.replace('*', '').splitlines()
    return list(dict.fromkeys(CATEGORIES.get(code.strip(), code.strip()) for code in codes if code.strip()))


def safe_url(url):
    try:
        parts = urlsplit(url)
        return url if parts.scheme == 'https' and parts.hostname and not parts.username else ''
    except ValueError:
        return ''


def serialize(row: sqlite3.Row, detail=False, now=None) -> dict:
    n = dict(row)
    raw = json.loads(n.pop('raw_json', '{}'))
    n['review'] = json.loads(n.pop('review_json', None) or '{}')
    n['categories'] = category_names(n['category'])
    n['notice_type'] = read_field(raw, 'noticeType-avisType-eng', 'noticeType-avisType-fra')
    n['industry'] = (read_field(raw, 'unspscDescription-eng', 'unspscDescription-fra',
                                'gsinDescription-nibsDescription-eng').splitlines() or [''])[0].lstrip('* ').strip()
    n.update(score_notice(n, now))
    if not detail:
        for key in ('description', 'review', 'user_notes'):
            n.pop(key, None)
        return n

    n['source_url'] = safe_url(n['source_url'])
    if n['source_id'] == 'canadabuys':
        ref = n['source_key']
        n['notice_url'] = canadabuys_notice_url(ref)
        n['search_url'] = 'https://canadabuys.canada.ca/en/tender-opportunities?' + urlencode(
            {'words': n['solicitation'] or ref})
    n['documents'] = list(dict.fromkeys(filter(None, (
        safe_url(part.strip()) for field in ('attachment-piecesJointes-eng', 'attachment-piecesJointes-fra')
        for part in re.split(r',(?=\s*https://)|\r?\n', raw.get(field) or '')
    ))))
    n['amendment'] = amendment(raw)
    n['amendment_date'] = read_field(raw, 'amendmentDate-dateModification')
    n['delivery_region'] = read_field(raw, 'regionsOfDelivery-regionsLivraison-eng', 'regionsOfDelivery-regionsLivraison-fra')
    return n


def get_notices(con, q='', source='', state='', min_score=0, limit=50, offset=0,
                view='all', category='', now=None):
    state = {'open': 'active', 'archived': 'historical'}.get(state, state)
    conditions, args = [], []
    if q:
        conditions.append('(' + ' OR '.join(
            f"instr(lower(n.{field}), ?) > 0" for field in
            ('title', 'buyer', 'description', 'solicitation', 'source_key', 'raw_json')
        ) + ')')
        args.extend([q[:120].lower()] * 6)
    if source:
        conditions.append('n.source_id=?')
        args.append(source)
    if view == 'pipeline':
        conditions.append("n.user_stage != ''")
    sql = DIRECTORY_SQL + (' WHERE ' + ' AND '.join(conditions) if conditions else '')
    now = now or datetime.now(timezone.utc)
    notices = []
    for row in con.execute(sql, args):
        n = serialize(row, now=now)
        if category and category not in n['categories']:
            continue
        if state == 'needs-review':
            if n['eligibility'] == 'met':
                continue
        elif state and state != n['record_state']:
            continue
        if n['score'] < min_score:
            continue
        if view == 'recommended' and (
            n['record_state'] != 'active' or n['eligibility'] == 'not_met'
            or n['user_stage'] == 'no_bid' or not n['matches']
            or (n['restricted_route'] and n['eligibility'] != 'met')
            or n['notice_type'] in {'Request for Information', 'Advance Contract Award Notice', 'Directed Contract'}
        ):
            continue
        notices.append(n)
    if view == 'recommended':
        notices.sort(key=lambda n: (
            n['eligibility'] != 'met', n['eligibility'] == 'partner_possible',
            -n['score'], n['closing_utc'], n['id']))
    else:
        notices.sort(key=lambda n: (n['record_state'] != 'active', n['closing_utc'] or '~', n['id']))
    offset = max(offset, 0)
    page = notices[offset:offset + min(max(limit, 1), 1000)]
    return {'total': len(notices), 'returned': len(page), 'notices': page}


def get_detail(con, notice_id):
    row = con.execute(DIRECTORY_SQL + ' WHERE n.id=?', (notice_id,)).fetchone()
    if not row:
        return None
    n = serialize(row, detail=True)
    n['versions'] = []
    for version in con.execute('''
        SELECT observed_at, change_type, old_value, new_value FROM notice_versions
        WHERE notice_id=? ORDER BY version_id DESC
    ''', (notice_id,)):
        before = json.loads(version['old_value'] or '{}')
        after = json.loads(version['new_value'] or '{}')
        n['versions'].append(dict(observed_at=version['observed_at'], change_type=version['change_type'],
                                  amendment=amendment(after), changed_fields=sorted(
                                      key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
                                  ) if before else []))
    return n


def update_review(con, notice_id, data):
    if not isinstance(data, dict):
        raise ValueError('Expected an object')
    stage, notes = data.get('stage'), data.get('notes')
    if stage not in STAGES or not isinstance(notes, str) or len(notes) > 5000:
        raise ValueError('Invalid pipeline decision or notes')
    gates = data.get('gates')
    if not isinstance(gates, dict) or gates.keys() != GATES.keys():
        raise ValueError('A review must include all mandatory criteria')
    for gate in gates.values():
        if (not isinstance(gate, dict) or gate.get('state') not in GATE_STATES
                or not isinstance(gate.get('evidence'), str) or len(gate['evidence']) > 800):
            raise ValueError('Invalid eligibility review')
        if gate['state'] != 'unknown' and not gate['evidence'].strip():
            raise ValueError('Add evidence for each reviewed requirement')
    review = {key: data.get(key) for key in ('gates', 'complexity', 'budget', 'revenue', 'cost')}
    if review['complexity'] not in COMPLEXITIES:
        raise ValueError('Invalid delivery complexity')
    for key in ('budget', 'revenue', 'cost'):
        amount = review[key]
        if amount is not None and (type(amount) not in (int, float) or not 0 <= amount <= 1e12):
            raise ValueError('Use a non-negative CAD amount up to one trillion')
    with con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT row_hash FROM notices WHERE id=?', (notice_id,)).fetchone()
        if not row:
            raise LookupError('Notice not found')
        current = con.execute('SELECT updated_at FROM notice_reviews WHERE notice_id=?', (notice_id,)).fetchone()
        if data.get('revision') != row['row_hash'] or data.get('review_updated_at') != (current[0] if current else None):
            raise RuntimeError('The notice or review changed. Reopen it before saving.')
        con.execute('UPDATE notices SET user_stage=?, user_notes=? WHERE id=?', (stage, notes, notice_id))
        con.execute('''
            INSERT INTO notice_reviews VALUES (?,?,?,?)
            ON CONFLICT(notice_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,
                updated_at=excluded.updated_at
        ''', (notice_id, row['row_hash'], json.dumps(review), datetime.now(timezone.utc).isoformat()))


def summary(con):
    rows = [serialize(row) for row in con.execute(DIRECTORY_SQL)]
    groups = Counter(n['source_id'] for n in rows)
    states = Counter(n['record_state'] for n in rows)
    runs = [dict(x) for x in con.execute('''
        SELECT r.*, s.published_utc, s.sha256 FROM ingest_runs r
        LEFT JOIN feed_snapshots s USING(run_id) ORDER BY r.run_id DESC LIMIT 15
    ''')]
    last_success = [dict(x) for x in con.execute('''
        SELECT r.source_id, r.finished_at, s.published_utc, r.rows_total
        FROM ingest_runs r JOIN feed_snapshots s USING(run_id)
        WHERE r.run_id IN (
            SELECT MAX(run_id) FROM ingest_runs JOIN feed_snapshots USING(run_id) GROUP BY source_id
        ) ORDER BY r.source_id
    ''')]
    amendments = con.execute("SELECT COUNT(*) FROM notice_versions WHERE change_type='changed'").fetchone()[0]
    return dict(total_notices=len(rows), by_source=dict(groups), states=dict(states), runs=runs,
                last_success=last_success, amendments_seen=amendments)


def analytics(con, state=''):
    rows = [serialize(row) for row in con.execute(DIRECTORY_SQL + " WHERE n.source_id != 'demo'")]
    categories = sorted({category for n in rows for category in n['categories']})
    if state:
        rows = [n for n in rows if n['record_state'] == state]
    published = Counter(n['published_at'][:7] for n in rows if re.fullmatch(r'\d{4}-\d{2}-\d{2}', n['published_at']))
    budgets = defaultdict(list)
    reviewed = con.execute('SELECT n.id, r.payload FROM notices n JOIN notice_reviews r ON n.id=r.notice_id '
                           'WHERE n.row_hash=r.revision')
    entered = {row['id']: json.loads(row['payload']).get('budget') for row in reviewed}
    for n in rows:
        manual = entered.get(n['id'])
        if manual is not None:
            budgets['Owner-entered CAD budgets'].append(manual)
        elif n['fiscal_budget'] is not None:
            budgets[f"Curated {n['currency'] or 'unspecified currency'} budgets"].append(n['fiscal_budget'])
    return dict(total=len(rows), category_options=categories,
                buyers=Counter(n['buyer'] or 'Unknown' for n in rows).most_common(12),
                industries=Counter(n['industry'] or 'Unknown' for n in rows).most_common(12),
                categories=Counter(c for n in rows for c in n['categories']).most_common(),
                months=sorted(published.items())[-18:],
                budgets=[dict(label=label, count=len(values), minimum=min(values),
                              median=median(values), maximum=max(values)) for label, values in sorted(budgets.items())],
                historical=sum(n['record_state'] == 'historical' for n in rows))
