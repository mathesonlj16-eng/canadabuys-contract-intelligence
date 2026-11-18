"""Freeze a stratified CI-03 sample from the isolated SEAO pilot."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

CUTOFF = '2026-11-11T05:00:00+00:00'
TECH = re.compile(r'site (?:web|internet)|logiciel|application web|accessibilit[eé] num[eé]rique|'
                  r'cybers[eé]curit[eé]|infonuagique|donn[eé]es', re.I)


def sample(con):
    rows = [(key, json.loads(fields)) for key, fields in con.execute('SELECT ocid, fields FROM pilot_tenders')]
    rows.sort(key=lambda row: hashlib.sha256(row[0].encode()).hexdigest())
    eligible = [(key, fields) for key, fields in rows if fields.get('notice_links')]

    def current(fields):
        return fields.get('status') == 'active' and fields.get('closing_utc', '') > CUTOFF

    strata = [
        ('disputed deadline', 6, lambda f: 'closing_utc' in f.get('conflicted_fields', [])),
        ('other conflict', 3, lambda f: f.get('conflicted_fields') and 'closing_utc' not in f['conflicted_fields']),
        ('control', 3, lambda f: current(f) and not f.get('conflicted_fields') and not TECH.search(f.get('title', ''))),
        ('technology title', 6, lambda f: current(f) and not f.get('conflicted_fields') and TECH.search(f.get('title', ''))),
    ]
    result = []
    for label, count, accept in strata:
        pool = [(key, fields) for key, fields in eligible if accept(fields)]
        if len(pool) < count:
            raise ValueError(f'Insufficient notices in {label}')
        for key, fields in pool[:count]:
            versions = []
            for release_id, date, patch in con.execute(
                'SELECT release_id, released_utc, patch FROM pilot_releases WHERE ocid=? ORDER BY released_utc,release_id',
                (key,),
            ):
                p = json.loads(patch)
                versions.append(dict(id=release_id, date=date, closing=p.get('closing_raw'), status=p.get('status'),
                                     buyer=p.get('buyer'), category=p.get('mainProcurementCategory'),
                                     notice_links=p.get('notice_links', [])))
            result.append(dict(stratum=label, pool_size=len(pool), ocid=key, projected=fields, releases=versions))
    return dict(cutoff=CUTOFF, notices=result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('db', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    with closing(sqlite3.connect(args.db.resolve(strict=True).as_uri() + '?mode=ro', uri=True)) as connection:
        args.output.write_text(json.dumps(sample(connection), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
