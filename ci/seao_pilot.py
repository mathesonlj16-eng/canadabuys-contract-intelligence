"""Import official SEAO release packages into a standalone pilot database."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from itertools import groupby
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit


TENDER_TAGS = {'tender', 'tenderUpdate', 'tenderCancellation'}
SCHEMA = """
CREATE TABLE pilot_releases (
    ocid TEXT NOT NULL, release_id TEXT NOT NULL, released_utc TEXT NOT NULL,
    fingerprint TEXT NOT NULL, patch TEXT NOT NULL,
    PRIMARY KEY (ocid, release_id)
);
CREATE TABLE pilot_tenders (
    ocid TEXT PRIMARY KEY, updated_utc TEXT NOT NULL, fields TEXT NOT NULL
);
CREATE TABLE pilot_runs (
    run_id INTEGER PRIMARY KEY, source_uri TEXT NOT NULL, licence TEXT NOT NULL,
    published_utc TEXT NOT NULL, sha256 TEXT NOT NULL, counts TEXT NOT NULL
);
PRAGMA user_version=1;
"""


def connect_pilot(path: Path):
    con = sqlite3.connect(path, timeout=20)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not tables:
            con.executescript(SCHEMA)
        elif tables != {'pilot_releases', 'pilot_tenders', 'pilot_runs'} or (
            con.execute('PRAGMA user_version').fetchone()[0] != 1
        ):
            raise ValueError('Choose a new file or an existing SEAO pilot database')
        con.row_factory = sqlite3.Row
        return con
    except BaseException:
        con.close()
        raise


def utc(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f'SEAO timestamp requires an explicit offset: {value}')
    return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')


def required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing SEAO identity or provenance')
    return value


def notice_links(documents):
    links = []
    for document in documents:
        url = document.get('url', '')
        parts = urlsplit(url)
        if parts.scheme == 'https' and parts.hostname == 'seao.gouv.qc.ca' and not parts.username:
            links.append(url)
    return list(dict.fromkeys(links))


def tender_patch(release):
    tender = release.get('tender') or {}
    patch = {name: tender[name] for name in ('id', 'title', 'status', 'mainProcurementCategory')
             if tender.get(name) not in (None, '')}
    if 'tenderCancellation' in release['tag']:
        patch['status'] = 'cancelled'
        patch['_cancellation_event'] = True
    buyer = (release.get('buyer') or {}).get('name')
    if buyer:
        patch['buyer'] = buyer
    period = tender.get('tenderPeriod') or {}
    for source, target in (('startDate', 'opened'), ('endDate', 'closing')):
        if value := period.get(source):
            patch[target + '_raw'] = value
            patch[target + '_utc'] = utc(value)
    if links := notice_links(tender.get('documents') or []):
        patch['notice_links'] = links
    return patch


def rebuild(con, ocid):
    fields = {}
    conflicts = set()
    rows = con.execute(
        'SELECT released_utc,patch FROM pilot_releases WHERE ocid=? ORDER BY released_utc,release_id', (ocid,)
    )
    for last_time, batch in groupby(rows, key=lambda row: row['released_utc']):
        merged = {}
        tied = set()
        cancellation = False
        for row in batch:
            patch = json.loads(row['patch'])
            cancellation |= patch.pop('_cancellation_event', False)
            tied.update(key for key in patch.keys() & merged.keys() if patch[key] != merged[key])
            merged.update(patch)
        if cancellation:
            # An explicit cancellation event controls a tied snapshot status.
            merged['status'] = 'cancelled'
            tied.discard('status')
        conflicts.difference_update(merged)
        conflicts.update(tied)
        fields.update(merged)
        for key in tied:
            fields.pop(key, None)
    if conflicts:
        fields['conflicted_fields'] = sorted(conflicts)
    con.execute('''
        INSERT INTO pilot_tenders VALUES (?,?,?)
        ON CONFLICT(ocid) DO UPDATE SET updated_utc=excluded.updated_utc, fields=excluded.fields
    ''', (ocid, last_time, json.dumps(fields, ensure_ascii=False, sort_keys=True)))
    return bool(conflicts)


def import_package(con, payload: bytes):
    package = json.loads(payload)
    source = required_text(package['uri'])
    licence = required_text(package['license'])
    published = utc(package['publishedDate'])
    counts = dict(inserted=0, replayed=0, skipped=0, conflicted=0)
    affected = set()
    with con:
        con.execute('BEGIN IMMEDIATE')
        for release in package['releases']:
            if not TENDER_TAGS.intersection(release['tag']):
                counts['skipped'] += 1
                continue
            ocid = required_text(release['ocid'])
            release_id = required_text(release['id'])
            released = utc(release['date'])
            patch = json.dumps(tender_patch(release), ensure_ascii=False, sort_keys=True)
            # Fingerprint tender fields used by the pilot, without retaining parties or contacts.
            fingerprint = hashlib.sha256(json.dumps(
                [released, sorted(release['tag']), patch], ensure_ascii=False).encode('utf-8')).hexdigest()
            previous = con.execute('SELECT fingerprint FROM pilot_releases WHERE ocid=? AND release_id=?',
                                   (ocid, release_id)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError(f'SEAO release identity collision: {ocid} / {release_id}')
                counts['replayed'] += 1
                continue
            con.execute('INSERT INTO pilot_releases VALUES (?,?,?,?,?)',
                        (ocid, release_id, released, fingerprint, patch))
            affected.add(ocid)
            counts['inserted'] += 1
        for ocid in sorted(affected):
            counts['conflicted'] += rebuild(con, ocid)
        con.execute('INSERT INTO pilot_runs (source_uri,licence,published_utc,sha256,counts) VALUES (?,?,?,?,?)',
                    (source, licence, published, hashlib.sha256(payload).hexdigest(), json.dumps(counts)))
    return dict(counts, rebuilt=len(affected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('packages', type=Path, nargs='+')
    args = parser.parse_args()
    try:
        with closing(connect_pilot(args.db)) as con:
            for path in args.packages:
                print(path.name, json.dumps(import_package(con, path.read_bytes())))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f'SEAO pilot import failed: {exc}\n')


if __name__ == '__main__':
    main()
