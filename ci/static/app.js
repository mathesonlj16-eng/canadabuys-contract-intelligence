const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
const number = value => Number(value ?? 0).toLocaleString('en-CA');
const money = value => value == null ? 'Unknown' : Number(value).toLocaleString('en-CA', {
  style: 'currency', currency: 'CAD', currencyDisplay: 'code'
});
const eastern = new Intl.DateTimeFormat('en-CA', {
  dateStyle: 'medium', timeStyle: 'short', timeZone: 'America/Toronto'
});
const timestamp = value => value ? eastern.format(new Date(value)) + ' ET' : 'Unknown';
const sourceNames = {
  canadabuys: 'CanadaBuys', manual: 'Prior research', demo: 'Fictional demo',
  'canadabuys-open': 'CanadaBuys Open', 'canadabuys-new': 'CanadaBuys New'
};
const stateNames = {active: 'Active in feed', historical: 'Historical', unverified: 'Needs source recheck', demo: 'Fictional demo'};
const stageNames = {'': 'Outside pipeline', saved: 'Saved', evaluating: 'Evaluating', bid: 'Bid', no_bid: 'No bid'};
const gateNames = {unknown: 'Unknown', met: 'Met', not_met: 'Not met', partner_possible: 'Partner possible', not_applicable: 'Not applicable'};
const viewLabels = {
  all: ['All Contracts', 'Search Canadian procurement notices and your prior research.'],
  recommended: ['Recommended', 'Active notices ranked by service fit, reviewed eligibility, and time to respond.'],
  pipeline: ['My Pipeline', 'Open a contract to save it, record a bid decision, or update your review.'],
  analytics: ['Research & Analytics', 'Explore buyers, categories, industries and budgets in the collected notices.'],
  health: ['Data Health', 'Check source coverage, refresh history, and ingestion errors.']
};
const pageSize = 40;
let view = 'all';
let offset = 0;
let requestId = 0;
let detailId = 0;
let searchTimer;
let gateLabels = {};

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function showError(error) {
  $('feedback').textContent = error.message;
}

function renderDirectory(data) {
  $('results').innerHTML = data.notices.map(n => `
    <tr>
      <td><button class="title-link" data-notice="${esc(n.id)}">${esc(n.title)}</button>
          <small>${esc(n.buyer || 'Buyer unavailable')} · ${esc(sourceNames[n.source_id])}${n.categories.length ? ' · ' + esc(n.categories.join(', ')) : ''}</small></td>
      <td><span class="badge ${esc(n.record_state)}">${esc(stateNames[n.record_state])}</span>
          ${view === 'pipeline' ? `<small>${esc(stageNames[n.user_stage] || n.user_stage)}</small>` : ''}</td>
      <td>${esc(n.closing_utc ? timestamp(n.closing_utc) : (n.close_raw || 'Unknown'))}</td>
      <td><span class="score">${n.score}</span><small>${esc(n.recommendation)}</small></td>
    </tr>`).join('') || `<tr><td colspan="4">${view === 'pipeline' ? 'No matching saved opportunities.' : 'No contracts match these filters.'}</td></tr>`;
  $('page-count').textContent = data.returned ?
    `${number(offset + 1)}–${number(offset + data.returned)} of ${number(data.total)}` : '0 results';
  $('previous').disabled = offset === 0;
  $('next').disabled = offset + pageSize >= data.total;
}

function renderHealth(data) {
  const metrics = [
    ['Collected records', data.total_notices], ['Active in feed', data.states.active],
    ['Historical records', data.states.historical], ['Observed changes', data.amendments_seen]
  ];
  $('metrics').innerHTML = metrics.map(([label, value]) => `<div class="metric">${label}<strong>${number(value)}</strong></div>`).join('');
  $('success').innerHTML = data.last_success.map(r => `
    <div class="run"><strong>${esc(sourceNames[r.source_id])}</strong>
    <small>Downloaded ${esc(timestamp(r.finished_at))} · ${number(r.rows_total)} rows</small>
    <small>Source published ${esc(timestamp(r.published_utc))}</small></div>`).join('') || '<p>No official refresh recorded. Run <code>.\\refresh.ps1</code> in PowerShell.</p>';
  $('runs').innerHTML = data.runs.map(r => `
    <div class="run"><strong>${esc(sourceNames[r.source_id] || r.source_id)}</strong> · <span class="${r.status === 'failed' ? 'warning' : ''}">${esc(r.status)}</span>
      <small>${esc(timestamp(r.finished_at || r.started_at))} · ${number(r.added)} added · ${number(r.changed)} changed · ${number(r.unchanged)} unchanged · ${number(r.skipped)} skipped</small>
      ${r.message ? `<small>${esc(r.message)}</small>` : ''}</div>`).join('') || '<p>No imports recorded.</p>';
}

const auditStatuses = {
  FOUND_IN_FEDERAL_FEED: 'Found in federal feed',
  FOUND_IN_OTHER_SOURCE: 'Found in other source',
  POSSIBLE_MATCH_REVIEW: 'Possible match — review',
  NOT_FOUND_IN_LOCAL_FEDERAL: 'Not in local federal snapshot',
  OUTSIDE_AUTOMATED_SCOPE: 'Outside automated federal scope'
};
function renderEmailAudit(data) {
  const source = data.last_successful_federal_import ? timestamp(data.last_successful_federal_import) : 'No successful federal import';
  const rows = data.records.map(r => {
    const match = r.matched_id ? `<small>Local ID: ${esc(r.matched_id)} · In Recommended: ${esc(r.in_recommended == null ? 'unknown' : r.in_recommended ? 'yes' : 'no')}</small>` : '';
    return `<tr><td><strong>${esc(r.title)}</strong><small>${esc(r.reference)}</small></td><td>${esc(r.platform)}</td><td>${esc(auditStatuses[r.status] || r.status)}${match}</td><td>${esc(r.deadline_reported || 'Unknown')}</td></tr>`;
  }).join('');
  $('email-audit').innerHTML = `<p><strong>${number(data.total_alerts)} supplier-alert records</strong> · ${number(data.federal_exact_matches)} exact federal matches out of ${number(data.federal_sample_size)} federal candidates · ${number(data.local_total_notices)} local records</p>` +
    `<p class='caption'>Latest successful federal import: ${esc(source)}. Alert deadlines are unverified; this sample does not measure nationwide recall.</p>` +
    `<div class='table-scroll' tabindex='0' aria-label='Email coverage comparison'><table><thead><tr><th>Notice</th><th>Platform</th><th>Comparison</th><th>Deadline in email</th></tr></thead><tbody>${rows}</tbody></table></div>` +
    `<p class='caption'>Exact, possible and out-of-scope matches are distinct. No email notice is imported as an official contract.</p>`;
}
function chart(title, data) {
  const maximum = Math.max(1, ...data.map(([, count]) => count));
  return `<section class="panel card"><h2>${title}</h2>` + (data.map(([label, count]) => `
    <div class="bar-row"><span>${esc(label)}</span><meter aria-label="${esc(label)}" value="${count}" max="${maximum}"></meter><strong>${number(count)}</strong></div>`
  ).join('') || '<p>No records with this information.</p>') + '</section>';
}

function renderAnalytics(data) {
  $('analytics-scope').textContent = `${number(data.total)} records, including ${number(data.historical)} historical records. Counts reflect collected records only. Fictional data is excluded.`;
  const budgets = data.budgets.map(b => {
    const format = b.label.includes(' CAD ') ? money : number;
    return `<div class="run"><strong>${esc(b.label)}</strong><small>${number(b.count)} known · ${esc(format(b.minimum))} to ${esc(format(b.maximum))} · median ${esc(format(b.median))}</small></div>`;
  }).join('');
  $('analytics-cards').innerHTML = chart('Published notices by month', data.months) + chart('Procurement categories', data.categories) +
    chart('Top buyers', data.buyers) + chart('Top industries and commodities', data.industries) +
    `<section class="panel card"><h2>Known budgets</h2>${budgets || '<p>No budgets recorded for these records.</p>'}<p>The tender CSV has no budget field. Coverage comes from curated or owner-entered amounts.</p></section>`;
}

async function loadView() {
  const seq = ++requestId;
  try {
    if (view === 'health') {
      const [summary, audit] = await Promise.all([api('/api/summary'), api('/api/email-audit')]);
      if (seq === requestId) { renderHealth(summary); renderEmailAudit(audit); }
    } else if (view === 'analytics') {
      const data = await api('/api/analytics?' + new URLSearchParams({state: $('analytics-state').value}));
      if (seq === requestId) renderAnalytics(data);
    } else {
      const params = new URLSearchParams(new FormData($('filters')));
      params.set('limit', pageSize);
      params.set('offset', offset);
      params.set('view', view);
      const data = await api('/api/notices?' + params);
      if (seq !== requestId) return;
      if (offset && offset >= data.total) {
        offset = Math.max(0, Math.floor((data.total - 1) / pageSize) * pageSize);
        return loadView();
      }
      renderDirectory(data);
    }
  } catch (error) {
    if (seq === requestId) showError(error);
  }
}

function selectView(next) {
  view = next;
  offset = 0;
  $('filters').reset();
  $('feedback').textContent = '';
  $('page-title').textContent = viewLabels[view][0];
  $('page-description').textContent = viewLabels[view][1];
  $('analytics').hidden = view !== 'analytics';
  $('health').hidden = view !== 'health';
  $('directory').hidden = ['analytics', 'health'].includes(view);
  $('filters').elements.state.disabled = view === 'recommended';
  $('results').innerHTML = '<tr><td colspan="4">Loading…</td></tr>';
  $('page-count').textContent = '';
  document.querySelectorAll('[data-view]').forEach(button => {
    if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  loadView();
}

const link = (url, label) => /^https:\/\//i.test(url || '') ?
  `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>` : '';
const options = (choices, selected) => Object.entries(choices).map(([value, label]) =>
  `<option value="${esc(value)}"${value === selected ? ' selected' : ''}>${esc(label)}</option>`).join('');

function reviewForm(n) {
  const review = n.review;
  const stale = n.review_revision && n.review_revision !== n.row_hash;
  const gates = Object.entries(gateLabels).map(([key, label]) => {
    const gate = review.gates?.[key];
    return `<div class="gate"><label>${esc(label)}<select name="${key}">${options(gateNames, stale ? 'unknown' : gate?.state || 'unknown')}</select></label>
      <label>Evidence for ${esc(label.toLowerCase())}<input name="${key}-evidence" maxlength="800" value="${esc(gate?.evidence)}" placeholder="Tender clause, document or explanation"></label></div>`;
  }).join('');
  return `<details ${n.user_stage ? 'open' : ''}><summary>Pipeline and eligibility review</summary>
    ${stale ? '<p class="warning">The source changed after your review. Recheck the requirements before confirming each criterion again.</p>' : ''}
    <form id="review">
      <div class="review-grid">
        <label>Pipeline decision<select name="stage">
          ${n.user_stage && !(n.user_stage in stageNames) ? options({[n.user_stage]: 'Legacy decision'}, n.user_stage) : ''}
          ${options(stageNames, n.user_stage)}</select></label>
        <label>Delivery complexity<select name="complexity">${options({unknown: 'Unknown', low: 'Low', moderate: 'Moderate', high: 'High'}, stale ? 'unknown' : review.complexity || 'unknown')}</select></label>
        ${[['budget', 'Documented budget ceiling (CAD)'], ['revenue', 'Estimated revenue (CAD)'], ['cost', 'Estimated delivery cost (CAD)']].map(([key, label]) =>
          `<label>${label}<input type="number" min="0" max="1000000000000" step="0.01" name="${key}" value="${esc(stale ? '' : review[key])}"></label>`
        ).join('')}
      </div>
      <label>Decision notes<textarea name="notes" maxlength="5000" rows="3">${esc(n.user_notes)}</textarea></label>
      <p class="caption">Mark a requirement only after reviewing the tender. Add a source or explanation for each decision.</p>
      ${gates}
      <button type="submit" class="primary">Save review</button><span id="save-result" role="status"></span>
    </form></details>`;
}

function renderDetail(n) {
  $('detail-title').textContent = n.title;
  const budget = n.review_revision === n.row_hash && n.review.budget != null ? money(n.review.budget) + ' · owner-entered' :
    n.fiscal_budget != null ? `${n.currency} ${number(n.fiscal_budget)} · ${n.budget_basis}` : 'Undisclosed in the feed';
  const fields = [
    ['Buyer', n.buyer], ['Record status', stateNames[n.record_state]], ['Feed status', n.status],
    ['Solicitation number', n.solicitation], ['Source reference', n.source_key], ['Notice type', n.notice_type],
    ['Deadline (Eastern)', n.closing_utc ? timestamp(n.closing_utc) : n.close_raw],
    ['Deadline as published', n.close_raw && n.source_id === 'canadabuys' && n.closing_utc ? `${n.close_raw} (feed UTC−05:00)` : n.close_raw],
    ['Deadline (UTC)', n.closing_utc], ['Delivery regions', n.delivery_region],
    ['Budget', budget], ['Estimated profit', money(n.estimated_profit)],
    ['Amendment', n.amendment], ['Amendment date', n.amendment_date],
    ['Source snapshot', n.snapshot_at ? timestamp(n.snapshot_at) : 'Unknown']
  ];
  const sources = [link(n.notice_url, 'CanadaBuys notice'), link(n.source_url, 'Buyer portal'), link(n.search_url, 'Search CanadaBuys by solicitation'), link(n.linked_issue, 'Management issue')].filter(Boolean);
  const docs = n.documents.map((url, i) => {
    let name;
    try { name = decodeURIComponent(new URL(url).pathname.split('/').pop()); } catch { name = ''; }
    return `<li>${link(url, name?.includes('.') ? name : `Official attachment ${i + 1}`)}</li>`;
  }).join('');
  const history = n.versions.map(v => `<li>${esc(timestamp(v.observed_at))} · ${esc(v.change_type)}${v.amendment ? ' · amendment ' + esc(v.amendment) : ''}${v.changed_fields.length ? ' · ' + esc(v.changed_fields.join(', ')) : ''}</li>`).join('');
  $('detail-body').innerHTML = `
    <span class="badge ${esc(n.record_state)}">${esc(stateNames[n.record_state])}</span>
    <dl class="detail-grid">${fields.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value || 'Unknown')}</dd></div>`).join('')}</dl>
    <h3>Official notice and documents</h3>
    <p>${sources.join(' · ') || 'No source link supplied.'}</p>
    ${docs ? '<ul>' + docs + '</ul>' : '<p class="caption">Check the buyer portal for documents and addenda.</p>'}
    <details open><summary>Scope and requirements</summary><p class="scope">${esc(n.description || 'No description supplied.')}</p></details>
    <details><summary>Fit and eligibility · ${n.score} / 100</summary>
      <p>${esc(n.recommendation)}. Verify the official notice before making a bid decision.</p>
      <dl class="detail-grid">${Object.entries(n.components).map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${value ?? 'Unknown'}</dd></div>`).join('')}</dl>
      <p>${esc(n.economics_note)}.</p>
      <p>${Object.entries(n.matches).map(([label, values]) => `${esc(label)} (${values.map(esc).join(', ')})`).join(' · ') || 'No service keywords matched.'}</p></details>
    ${reviewForm(n)}
    <details><summary>Observed revisions (${n.versions.length})</summary><ul>${history}</ul></details>`;
  $('review').addEventListener('submit', event => saveReview(event, n));
}

async function saveReview(event, n) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = new FormData(form);
  const payload = Object.fromEntries(['stage', 'notes', 'complexity'].map(key => [key, values.get(key)]));
  const button = form.querySelector('button[type="submit"]');
  payload.revision = n.row_hash;
  payload.review_updated_at = n.review_updated_at;
  payload.gates = Object.fromEntries(Object.keys(gateLabels).map(key =>
    [key, {state: values.get(key), evidence: values.get(`${key}-evidence`)}]));
  for (const key of ['budget', 'revenue', 'cost']) {
    const value = values.get(key);
    payload[key] = value === '' ? null : Number(value);
  }
  button.disabled = true;
  try {
    await api(`/api/notice/${encodeURIComponent(n.id)}/review`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    if ($('detail').open && $('review') === form) $('detail').close();
    $('feedback').textContent = 'Review saved.';
    await loadView();
  } catch (error) {
    const message = form.querySelector('#save-result');
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function showDetail(id) {
  const seq = ++detailId;
  try {
    const [n, config] = await Promise.all([api('/api/notice/' + encodeURIComponent(id)), api('/api/config')]);
    if (seq !== detailId) return;
    gateLabels = config.gates;
    renderDetail(n);
    if (!$('detail').open) $('detail').showModal();
    $('detail').scrollTop = 0;
  } catch (error) {
    showError(error);
  }
}

document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => selectView(button.dataset.view)));
$('filters').addEventListener('submit', event => event.preventDefault());
$('filters').addEventListener('input', () => {
  offset = 0;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadView, 175);
});
$('previous').addEventListener('click', () => { offset -= pageSize; loadView(); });
$('next').addEventListener('click', () => { offset += pageSize; loadView(); });
$('results').addEventListener('click', event => {
  const button = event.target.closest('[data-notice]');
  if (button) showDetail(button.dataset.notice);
});
$('close').addEventListener('click', () => $('detail').close());
$('analytics-state').addEventListener('change', loadView);
$('reload').addEventListener('click', async () => {
  $('feedback').textContent = '';
  try { renderHealth(await api('/api/summary')); } catch (error) { showError(error); }
  loadView();
});
api('/api/summary').then(renderHealth).catch(showError);
api('/api/analytics').then(data => {
  $('category').insertAdjacentHTML('beforeend', options(Object.fromEntries(data.category_options.map(c => [c, c]))));
}).catch(showError);
loadView();
