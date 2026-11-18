"""Replay the frozen CI-02 sample against a database containing the same rows."""
import argparse
import csv
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ci import core


def read_rows(path, **options):
    with path.open(encoding='utf-8', newline='') as stream:
        return list(csv.DictReader(stream, **options))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=core.DB_PATH)
    args = parser.parse_args()
    folder = Path(__file__).parent
    sample = read_rows(folder / 'ci02-sample.csv')
    labels = {row['id']: row for row in read_rows(folder / 'ci02-labels.psv', delimiter='|')}
    cutoff = datetime.fromisoformat(sample[0]['cutoff_utc'])
    with closing(core.connect(args.db)) as con:
        ranking = {}
        offset = 0
        while True:
            page = core.get_notices(con, view='recommended', limit=250, offset=offset, now=cutoff)
            ranking.update({n['id']: offset + i for i, n in enumerate(page['notices'], 1)})
            offset += page['returned']
            if offset >= page['total'] or not page['returned']:
                break
        print('id,expected_fit,expected_route,shortlist_rank,eligibility,budget,profit')
        for item in sample:
            ref = item['source_key']
            notice_id = 'canadabuys:' + ref
            row = core.get_detail(con, notice_id)
            if not row or row['row_hash'] != item['row_hash']:
                parser.error(f'Frozen revision unavailable for {ref}. Use the original snapshot.')
            print(ref, labels[ref]['fit'], labels[ref]['route'], ranking.get(notice_id, ''),
                  row['eligibility'], row['fiscal_budget'], row['estimated_profit'], sep=',')


if __name__ == '__main__':
    main()
