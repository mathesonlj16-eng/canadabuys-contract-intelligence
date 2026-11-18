"""Render the frozen CI-03 evidence as a standalone, read-only HTML preview."""
import argparse
from html import escape
import json
from pathlib import Path
from urllib.parse import urlsplit


def official_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'seao.gouv.qc.ca':
        raise ValueError('Expected an official SEAO link')
    return escape(url)


def render(sample, observations):
    rows = []
    for item in sample['notices']:
        fields = item['projected']
        observation = observations[item['ocid']]
        title = escape(fields.get('title', 'Untitled notice'))
        buyer = escape(fields.get('buyer', 'Unknown buyer'))
        ocid = escape(item['ocid'])
        stratum = escape(item['stratum'])
        observed = escape(observation.get('observed_utc') or 'Unknown')
        page_date = escape(observation.get('deadline') or 'Unknown')
        page_status = escape(observation.get('status') or 'Unknown')
        qualification = escape(observation.get('fit', 'Eligibility unreviewed'))
        url = official_url(observation['url'])
        history = ''.join(
            '<li><code>' + escape(release['id']) + '</code> · ' + escape(release['date']) + ' · '
            + escape(release.get('closing') or 'Deadline omitted') + ' · '
            + escape(release.get('status') or 'Status omitted') + '</li>'
            for release in item['releases']
        )
        conflict = ', '.join(fields.get('conflicted_fields', []))
        source_date = escape(fields.get('closing_raw') or ('Disputed' if 'closing_utc' in conflict else 'Unknown'))
        rows.append(f"""
<tr data-stratum="{stratum}">
  <td><strong>{title}</strong><small>{buyer} · Québec SEAO</small><small>{ocid} · {stratum}</small><small>{qualification}</small></td>
  <td>{source_date}<small>{escape(fields.get('status') or 'Unknown status')}</small></td>
  <td>{page_date}<small>{page_status} · {observed}</small></td>
  <td><a href="{url}" target="_blank" rel="noreferrer">Official notice</a>
    <details><summary>Release evidence</summary><ul>{history}</ul>
    <small>Conflicting fields: {escape(conflict or 'None in pilot')}</small></details></td>
</tr>""")
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CI-03 · Québec SEAO review</title>
<style>
* { box-sizing: border-box } body { background: #f3f5f7; color: #1b2e3e; font: 15px/1.5 system-ui,sans-serif; margin: 0 }
main { max-width: 1370px; margin: auto; padding: 32px clamp(14px,4vw,52px) } h1 { margin: 0; letter-spacing: -.04em; font-size: clamp(28px,4vw,40px) }
p { color: #40576a } a { color: #075e59 } label { display: inline-grid; gap: 4px; margin: 0 12px 12px 0 }
input,select { padding: 8px 11px; border: 1px solid #aebdc7; border-radius: 6px; font: inherit }
.wrap { overflow: auto; border: 1px solid #ced9e0; border-radius: 10px; background: #fff }
table { width: 100%; min-width: 720px; border-collapse: collapse; text-align: left }
th,td { vertical-align: top; padding: 13px; border-bottom: 1px solid #dbe3e8 } th { background: #e9f0f5 }
td:first-child { width: 32% } small { display: block; color: #465e70; margin-top: 5px } details { margin-top: 6px }
summary { cursor: pointer } ul { padding-left: 18px } [hidden] { display: none }
</style></head>
<body><main><h1>All Contracts · Québec SEAO preview</h1>
<p>Isolated review of the November 11 cohort. Buyer observations are dated checks. Eligibility requires a separate tender review.</p>
<label>Search notices <input type="search" id="search" placeholder="Title, buyer, or OCID"></label>
<label>Sample group <select id="group"><option value="">All groups</option><option>disputed deadline</option><option>other conflict</option><option>control</option><option>technology title</option></select></label>
<p id="count" role="status"></p>
<div class="wrap" tabindex="0" aria-label="Contract evidence"><table><thead><tr>
<th scope="col">Opportunity</th><th scope="col">Feed projection</th><th scope="col">Buyer page observation</th><th scope="col">Source and release history</th>
</tr></thead><tbody>""" + ''.join(rows) + """</tbody></table></div>
<p>Source: Secrétariat du Conseil du trésor, <a href="https://www.donneesquebec.ca/recherche/dataset/systeme-electronique-dappel-doffres-seao">Données Québec SEAO</a> · <a href="https://www.donneesquebec.ca/licence/#cc-by">CC BY 4.0</a>.</p>
</main><script>
const search = document.querySelector('#search'), group = document.querySelector('#group');
function filter() {
  const query = search.value.trim().toLocaleLowerCase();
  let shown = 0;
  for (const row of document.querySelectorAll('tbody tr')) {
    row.hidden = (group.value !== '' && group.value !== row.dataset.stratum) ||
      !row.cells[0].textContent.toLocaleLowerCase().includes(query);
    if (!row.hidden) shown++;
  }
  document.querySelector('#count').textContent = shown + (shown === 1 ? ' notice in view' : ' notices in view');
}
search.addEventListener('input', filter);
group.addEventListener('change', filter);
filter();
</script></body></html>"""


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    folder = Path(__file__).parent
    sample = json.loads((folder / 'ci03-sample.json').read_text(encoding='utf-8'))
    observations = json.loads((folder / 'ci03-observations.json').read_text(encoding='utf-8'))
    args.output.write_text(render(sample, {row['ocid']: row for row in observations}), encoding='utf-8')
