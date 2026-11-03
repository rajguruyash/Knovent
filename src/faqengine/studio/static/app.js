'use strict';
/* FAQ Studio. Everything on the page is built with DOM calls, never innerHTML, so text from a FAQ can not inject markup. */

// ---------- small helpers
const SVG_NS = 'http://www.w3.org/2000/svg';
function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function make(create, tag, attrs, kids) {
  const el = create(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.setAttribute('class', v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  return append(el, kids);
}
const h = (tag, attrs, ...kids) => make(t => document.createElement(t), tag, attrs, kids);
const svg = (tag, attrs, ...kids) => make(t => document.createElementNS(SVG_NS, t), tag, attrs, kids);
const pct = (x, d = 0) => (x == null ? 'n/a' : (100 * x).toFixed(d) + '%');
const num = (x, d = 2) => (x == null ? 'n/a' : Number(x).toFixed(d));
const secs = s => (s == null ? '…' : s < 90 ? Math.round(s) + ' s' : Math.floor(s / 60) + ' min ' + Math.round(s % 60) + ' s');
const gb = x => (x == null ? 'unknown' : x.toFixed(1) + ' GB');
const clock = ts => new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

async function api(path, body, method) {
  const res = await fetch(path, {
    method: method || (body !== undefined ? 'POST' : 'GET'),
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new Error(data.error || 'Something went wrong (' + res.status + ').');
  return data;
}

// ---------- state and routing
const S = { cols: null, overview: null, pid: null, project: null, tab: 'faq', timer: null, test: null, notice: null, pending: null, flash: null };
const TABS = [['faq', '1 · Your FAQ'], ['train', '2 · Train'], ['results', '3 · Results'], ['test', '4 · Test']];
const BANDS = {
  direct: { label: 'Answer directly', sub: 'Confident enough to reply without an LLM.', cls: 'good' },
  llm: { label: 'Needs a closer look', sub: 'Related, but not certain. This is where your LLM would take over.', cls: 'warn' },
  none: { label: 'Not in the FAQ', sub: 'Nothing is close enough to trust.', cls: 'muted' },
};
const STAGES = [['reading', 'Reading your FAQ'], ['loading_model', 'Loading the base model'], ['starting', 'Training'], ['checking', 'Checking the result'], ['saving', 'Saving your engine']];
const SOURCES = {
  'held-out phrasing': 'Another way of asking that you wrote, kept out of training and the index for this check.',
  'typo or shortened': 'Your questions with typos, or cut short, in ways the model did not practise.',
  'answer words': 'Keywords taken from each answer: does the answer\'s own wording lead back to its entry?',
};
const GLOSSARY = [
  ['Right first (Hit@1, P@1, R@1)', 'How often the correct entry was the engine\'s first choice. The number to watch.'],
  ['R@3, R@5 (recall)', 'How often the correct entry was somewhere in the top 3 (or 5). Matters if you show several suggestions or hand a few to an LLM.'],
  ['P@3, P@5 (precision)', 'Of the top 3 (or 5) shown, what share is correct. With one right entry per question this can never exceed 1/3 (or 1/5), so it is shown for completeness.'],
  ['MRR@10', 'Average of 1 ÷ position of the correct entry (1 if first, ½ if second…). Rewards putting the right entry near the top.'],
  ['nDCG@10', 'A 0 to 1 ranking-quality score that discounts the correct entry the further down it appears.'],
  ['Loss', 'What training tries to reduce. Lower is better. Compare train, dev and test: a test loss far above the train loss means it memorised instead of learning.'],
];

function go(tab) {
  const target = '#/p/' + S.pid + '/' + tab;
  if (location.hash === target) route(); else location.hash = target;
}
function defaultTab(p) { return p.status.state === 'trained' ? 'results' : p.status.state === 'training' ? 'train' : 'faq'; }

async function route() {
  stopPolling();
  const m = location.hash.match(/^#\/p\/([0-9a-f]{12})(?:\/(faq|train|results|test))?$/);
  try {
    S.overview = await api('/api/overview');
    if (!m) { S.pid = null; S.project = null; S.test = null; S.pending = null; return render(); }
    if (S.pid !== m[1]) { S.test = null; S.pending = null; S.flash = null; }
    S.pid = m[1];
    await loadProject();
  } catch (e) {
    if (m) { location.hash = '#/'; return; }
    return fatal(e);
  }
  S.tab = m[2] || defaultTab(S.project);
  if (['results', 'test'].includes(S.tab) && S.project.status.state !== 'trained') S.tab = defaultTab(S.project);
  render();
  maybePoll();
}
async function loadProject() { S.project = await api('/api/projects/' + S.pid); if (!S.providers) { try { S.providers = (await api('/api/llm/providers')).providers; } catch (e) { S.providers = []; } } }
function fatal(e) {
  document.getElementById('app').replaceChildren(h('p', { class: 'boot' }, 'Could not reach the studio: ' + e.message + ' Is it still running?'));
}

// ---------- polling while a job runs
function stopPolling() { if (S.timer) { clearInterval(S.timer); S.timer = null; } }
function maybePoll() {
  const p = S.project;
  if (p && (p.status.state === 'training' || p.export.state === 'running') && !S.timer) S.timer = setInterval(poll, 600);
}
async function poll() {
  try {
    const was = S.project.status.state, wasExport = S.project.export.state;
    const r = await api('/api/projects/' + S.pid + '?light=1');
    Object.assign(S.project, r);
    if (was === 'training' && r.status.state !== 'training') {
      stopPolling();
      await loadProject();
      S.overview = await api('/api/overview');
      if (r.status.state === 'trained') go('results'); else render();
    } else if (wasExport === 'running' && r.export.state !== 'running') {
      stopPolling();
      await loadProject();
      render();
    } else if (S.tab === 'train') {
      const live = document.getElementById('live');
      if (live) fillLive(live);
    }
  } catch (e) { stopPolling(); }
}

// ---------- notices
function notify(msg, kind) {
  S.notice = { msg, kind: kind || 'bad' };
  const box = document.getElementById('notice');
  if (box) box.replaceChildren(h('div', { class: 'banner ' + S.notice.kind, role: 'alert' }, msg));
  clearTimeout(notify.t);
  notify.t = setTimeout(() => { S.notice = null; const b = document.getElementById('notice'); if (b) b.replaceChildren(); }, 9000);
}

// ---------- charts
function lineChart(points, opts) {
  const o = Object.assign({ w: 360, hgt: 130, color: 'var(--accent)', label: '', yFmt: v => v.toFixed(2), yMin: null, yMax: null }, opts);
  if (points.length < 2) return h('p', { class: 'muted small' }, 'Not enough points to draw.');
  const xs = points.map(p => p.x), ys = points.map(p => p.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = o.yMin ?? Math.min(...ys), y1 = o.yMax ?? Math.max(...ys);
  const sx = x => 30 + ((x - x0) / (x1 - x0 || 1)) * (o.w - 40);
  const sy = y => o.hgt - 18 - ((y - y0) / (y1 - y0 || 1)) * (o.hgt - 30);
  const d = points.map((p, i) => (i ? 'L' : 'M') + sx(p.x).toFixed(1) + ',' + sy(p.y).toFixed(1)).join(' ');
  return svg('svg', { viewBox: '0 0 ' + o.w + ' ' + o.hgt, class: 'chart', role: 'img', 'aria-label': o.label },
    svg('line', { x1: 30, y1: o.hgt - 18, x2: o.w - 10, y2: o.hgt - 18, stroke: 'var(--line)' }),
    svg('path', { d, fill: 'none', stroke: o.color, 'stroke-width': 2, 'stroke-linejoin': 'round' }),
    svg('text', { x: 2, y: 12 }, o.yFmt(y1)), svg('text', { x: 2, y: o.hgt - 20 }, o.yFmt(y0)),
    svg('text', { x: 30, y: o.hgt - 4 }, 'step ' + x0), svg('text', { x: o.w - 10, y: o.hgt - 4, 'text-anchor': 'end' }, 'step ' + x1));
}
function entryMap(map, opts) {
  const o = Object.assign({ highlight: [], point: null }, opts);
  const w = 440, hgt = 320, pad = 26;
  const sx = x => pad + ((x + 1) / 2) * (w - 2 * pad), sy = y => hgt - pad - ((y + 1) / 2) * (hgt - 2 * pad);
  const few = map.points.length <= 14;
  const dots = map.points.map(p => {
    const on = o.highlight.includes(p.id);
    return svg('g', {},
      svg('circle', { cx: sx(p.x), cy: sy(p.y), r: on ? 7 : 5, fill: on ? 'var(--warn)' : 'var(--accent)', opacity: on ? 1 : 0.7 }, svg('title', {}, p.question)),
      (on || few) ? svg('text', { x: sx(p.x) + (p.x > 0.2 ? -9 : 9), y: sy(p.y) + 4, 'text-anchor': p.x > 0.2 ? 'end' : 'start', style: 'font-size:10px' }, clip(p.question, 26)) : null);
  });
  const star = o.point ? svg('g', {},
    svg('path', { d: 'M0,-9 L3,-3 L9,0 L3,3 L0,9 L-3,3 L-9,0 L-3,-3 Z', transform: 'translate(' + sx(o.point[0]) + ',' + sy(o.point[1]) + ')', fill: 'var(--bad)' }),
    svg('text', { x: sx(o.point[0]) + 12, y: sy(o.point[1]) - 6, style: 'font-size:11px;font-weight:600;fill:var(--bad)' }, 'your question')) : null;
  return svg('svg', { viewBox: '0 0 ' + w + ' ' + hgt, class: 'chart', role: 'img', 'aria-label': 'Map of your FAQ entries: entries about similar things sit close together' },
    svg('rect', { x: 1, y: 1, width: w - 2, height: hgt - 2, rx: 10, fill: 'none', stroke: 'var(--line)' }), dots, star);
}

// ---------- building blocks
const card = (title, sub, ...kids) => h('section', { class: 'card' }, title ? h('h2', {}, title) : null, sub ? h('p', { class: 'muted small' }, sub) : null, ...kids);
const stat = (value, label, hero) => h('div', { class: 'stat' + (hero ? ' hero' : '') }, h('b', {}, value), h('span', {}, label));
const facts = rows => h('dl', { class: 'facts' }, rows.filter(Boolean).map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
const bar = (frac, cls) => h('div', { class: 'bar ' + (cls || ''), role: 'img', 'aria-label': pct(frac) }, h('i', { style: 'width:' + Math.max(0, Math.min(1, frac || 0)) * 100 + '%' }));
function statePill(state) {
  const m = { new: ['Not trained', 'muted'], training: ['Training…', 'warn'], trained: ['Trained', 'good'], failed: ['Failed', 'bad'] }[state] || [state, 'muted'];
  return h('span', { class: 'pill ' + m[1] }, m[0]);
}

function topBar() {
  const hw = S.overview && S.overview.hardware;
  return h('header', { class: 'top' },
    h('a', { class: 'brand', href: '#/' },
      svg('svg', { viewBox: '0 0 32 32' }, svg('rect', { width: 32, height: 32, rx: 8, fill: 'var(--accent)' }), svg('path', { d: 'M9 11h14M9 16h14M9 21h8', stroke: 'var(--accent-text)', 'stroke-width': 2.5, 'stroke-linecap': 'round' })),
      'FAQ Studio'),
    h('span', { class: 'spacer' }),
    hw ? h('span', { class: 'pill muted', title: hw.cpu + ' · ' + gb(hw.ram_gb) + ' memory' }, hw.accelerator) : null);
}
function sidebar() {
  const list = (S.overview && S.overview.projects) || [];
  return h('nav', { class: 'side', 'aria-label': 'Projects' },
    h('a', { class: 'btn primary', href: '#/', style: 'width:100%;justify-content:center' }, '+ New project'),
    h('h3', {}, 'Your projects'),
    list.length ? list.map(p => h('button', { class: 'proj', type: 'button', 'aria-current': String(p.id === S.pid), onclick: () => { location.hash = '#/p/' + p.id; } },
      h('span', { class: 'dot ' + p.state }), h('span', { class: 'name' }, p.name))) : h('p', { class: 'muted small' }, 'Nothing yet. Upload a FAQ to start.'));
}

// ---------- welcome / upload
const TEXT_FILE = /\.(csv|tsv|json|jsonl|txt|md|markdown|html?)$/i;
const ACCEPT = '.csv,.tsv,.xlsx,.json,.jsonl,.txt,.md,.markdown,.html,.htm,.pdf,.docx';
function toBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
async function readFiles(list) {
  const out = [];
  for (const f of Array.from(list)) {
    if (f.size > 15 * 1024 * 1024) throw new Error(f.name + ' is larger than 15 MB.');
    out.push(TEXT_FILE.test(f.name) ? { filename: f.name, text: await f.text() } : { filename: f.name, base64: toBase64(await f.arrayBuffer()) });
  }
  return out;
}
const isWeb = src => /^https?:\/\//.test(src || '');
const fromLabel = src => (isWeb(src) ? 'Web page: ' : 'Document: ') + src;
function webRow(onGo, compact) {
  const url = h('input', { type: 'url', placeholder: 'https://your-site.com/help', autocomplete: 'off', maxlength: 2000, 'aria-label': 'Address of a web page', style: 'flex:1;min-width:220px' });
  const pages = h('select', { 'aria-label': 'How many pages to read', style: 'max-width:250px' },
    [[1, 'Just this page'], [5, 'This page and up to 4 it links to'], [15, 'Up to 15 pages of this site'], [30, 'Up to 30 pages of this site']].map(([v, t]) => h('option', { value: v }, t)));
  const go = h('button', { class: 'btn', type: 'button', onclick: async () => {
    if (!url.value.trim()) return notify('Paste the address of a web page.');
    const label = go.textContent;
    go.disabled = true; go.textContent = 'Reading…';
    try { await onGo(url.value.trim(), Number(pages.value)); } catch (e) { notify(e.message); } finally { go.disabled = false; go.textContent = label; }
  } }, compact ? '+ Add web page' : 'Read this web page');
  url.addEventListener('keydown', e => { if (e.key === 'Enter') go.click(); });
  return h('div', { class: 'row', style: compact ? 'margin-top:10px' : 'justify-content:center' }, url, pages, go);
}
function welcome() {
  const input = h('input', { type: 'file', multiple: true, accept: ACCEPT, hidden: true, onchange: e => e.target.files.length && uploadFiles(e.target.files) });
  const drop = h('div', { class: 'drop', tabindex: '0', role: 'button', 'aria-label': 'Choose your files',
    onclick: () => input.click(), onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } },
    ondragover: e => { e.preventDefault(); drop.classList.add('over'); }, ondragleave: () => drop.classList.remove('over'),
    ondrop: e => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); } },
    h('b', {}, 'Drop your FAQ here, plus any documents'), h('span', { class: 'muted' }, 'Choose several files at once · FAQ tables (.csv .tsv .xlsx .json .jsonl) · documents (.txt .md .html .pdf .docx)'));
  return h('div', {},
    h('div', { id: 'notice' }),
    h('section', { class: 'card hero-card' },
      h('h1', {}, 'Turn your FAQ and documents into an engine you can test'),
      h('p', { class: 'muted' }, 'Upload your questions and answers, and any handbooks, policies or pages that add context. The studio trains a small model on this computer, shows you how it performs, and lets you try it and approve or reject what it says.'),
      drop, input,
      h('p', { class: 'muted small', style: 'margin:14px 0 6px' }, 'Or give it a web page: it reads the page (and, if you ask, the pages it links to on the same site) and keeps the text in the knowledge base.'),
      webRow(async (u, n) => {
        const r = await api('/api/projects', { web: { url: u, pages: n } });
        S.flash = 'Read ' + (r.notes.length ? 'the pages it could. Skipped: ' + r.notes.slice(0, 3).join('; ') : 'the page' + (n > 1 ? 's' : '')) + '.';
        location.hash = '#/p/' + r.id + '/faq';
      }, false),
      h('div', { class: 'row', style: 'justify-content:center;margin-top:14px' },
        h('button', { class: 'btn', type: 'button', onclick: useExample }, 'Try the bike-shop example'),
        h('a', { class: 'btn', href: '/api/template.csv', download: '' }, 'Download a FAQ template')),
      h('p', { class: 'muted small', style: 'margin-top:14px' }, 'A FAQ table needs a question column (Question, Q or FAQ all work) and, ideally, an answer column; id, category and alt_questions (other ways of asking it, separated by |) are optional. Documents, web pages and tables of values are cut into passages that are embedded and searched together with the FAQ; answers can be built from them as evidence, quoting their own sentences with a reference.'),
      h('div', { class: 'how' },
        h('div', {}, h('b', {}, '1 · Upload'), 'Your files are checked and kept only on this computer.'),
        h('div', {}, h('b', {}, '2 · Train'), 'A small model learns your wording. Watch it live.'),
        h('div', {}, h('b', {}, '3 · Read the report'), 'Scores, comparisons, and where your FAQ is confusing.'),
        h('div', {}, h('b', {}, '4 · Test it'), 'Ask anything. Approve or reject, with a comment.'))));
}
async function uploadFiles(list) {
  try {
    const files = await readFiles(list);
    const stem = files[0].filename.replace(/\.[^.]+$/, '');
    const r = await api('/api/projects', { name: files.length > 1 ? stem + ' + ' + (files.length - 1) + ' more' : stem, files });
    location.hash = '#/p/' + r.id + '/faq';
  } catch (e) { notify(e.message); }
}
async function useExample() {
  try { const r = await api('/api/projects/example', {}); location.hash = '#/p/' + r.id + '/faq'; } catch (e) { notify(e.message); }
}

// ---------- project frame
function projectView() {
  const P = S.project, trained = P.status.state === 'trained';
  const content = { faq: faqTab, train: trainTab, results: resultsTab, test: testTab }[S.tab]();
  return h('div', {},
    h('div', { id: 'notice' }),
    h('div', { class: 'head' },
      h('div', { class: 'grow' }, h('h1', {}, P.name), h('p', { class: 'muted small' }, projectLine(P))),
      statePill(P.status.state),
      h('button', { class: 'btn link', type: 'button', onclick: deleteProject }, 'Delete')),
    h('div', { class: 'tabs', role: 'tablist' }, TABS.map(([id, label]) => h('button', {
      class: 'tab', role: 'tab', type: 'button', 'aria-selected': String(S.tab === id), disabled: ['results', 'test'].includes(id) && !trained,
      onclick: () => go(id) }, label))),
    content);
}
function projectLine(P) {
  const f = P.faq, bits = [f.sources.length + (f.sources.length === 1 ? ' source' : ' sources')];
  if (f.entries) bits.push(f.entries + (f.entries === 1 ? ' FAQ entry' : ' FAQ entries'));
  if (f.passages) bits.push(f.passages + ' document passages');
  return bits.join(' · ');
}
async function deleteProject() {
  if (!confirm('Delete this project, its model and all your tests? This cannot be undone.')) return;
  try { await api('/api/projects/' + S.pid, undefined, 'DELETE'); location.hash = '#/'; } catch (e) { notify(e.message); }
}

// ---------- tab 1: the files
function staleBanner() {
  const trained = S.project.status.state === 'trained';
  return h('div', { class: 'banner warn' }, h('b', {}, 'The files changed since this model was trained. '),
    'Search and tests still use the old files until you update the index (seconds; the model is not changed) or train again (minutes; the model also learns the new wording). ',
    trained ? h('button', { class: 'btn', type: 'button', style: 'margin-left:8px', onclick: reindex }, 'Update the search index') : null,
    trained ? h('button', { class: 'btn', type: 'button', style: 'margin-left:8px', onclick: () => startTrain(false) }, 'Train again') : null);
}
function reindexedBanner() {
  return h('div', { class: 'banner info' }, 'The search index is up to date with the current files. The model itself was trained on earlier files, so wording that is only in the new files is searched by the model as it was. ',
    h('button', { class: 'btn', type: 'button', style: 'margin-left:8px', onclick: () => startTrain(false) }, 'Train again'));
}
async function reindex() {
  try {
    const r = await api('/api/projects/' + S.pid + '/reindex', {});
    S.flash = 'Search index updated: ' + r.embedded + ' new or changed pieces embedded, ' + r.kept + ' kept as they were' + (r.dropped ? ', ' + r.dropped + ' dropped' : '') + '. No training was needed.';
    await loadProject();
    go('faq');  // where the message is shown, so it cannot linger and appear on another project
  } catch (e) { notify(e.message); }
}
function faqTab() {
  const P = S.project, f = P.faq, state = P.status.state;
  const singles = f.preview.filter(r => r.kind === 'faq' && r.phrasings === 1).length;
  const hasDocs = f.passages > 0;
  return h('div', {},
    h('div', { id: 'notice' }),
    P.stale ? staleBanner() : P.reindexed ? reindexedBanner() : null,
    (P.notices || []).map(n => h('div', { class: 'banner warn' }, n)),
    S.flash ? h('div', { class: 'banner info' }, (() => { const t = S.flash; S.flash = null; return t; })()) : null,
    f.warnings.length ? h('div', { class: 'banner warn' }, h('b', {}, 'Worth a look: '), h('ul', { class: 'plain' }, f.warnings.map(w => h('li', {}, w)))) : null,
    f.entries === 0 ? h('div', { class: 'banner info' }, 'There are only documents here, no FAQ question and answer pairs. They will be searched with the base model, and there is nothing to train. Add a FAQ file to train an engine.') : null,
    f.entries && singles > f.entries / 2 ? h('div', { class: 'banner info' }, 'Most FAQ entries have one way of asking. That works for search, but training can only learn from wording variety: add alternates (the alt_questions column), or test the engine and let your approved questions become extra wording.') : null,
    P.llm && !P.llm.onboarded ? llmCard(true) : null,
    P.faq.language.advice ? modelCard() : null,
    S.cols ? columnsCard() : null, sourcesCard(), heldoutCard(), regressionCard(), P.faq.language.advice ? null : modelCard(), P.llm && P.llm.onboarded ? llmCard(false) : null,
    card('What was read', 'The FAQ entries and document passages the engine will search.',
      h('div', { class: 'grid3', style: 'margin:10px 0 14px' }, stat(f.entries, 'FAQ entries'), stat(hasDocs ? f.passages : f.phrasings, hasDocs ? 'document passages' : 'ways of asking'), stat(f.warnings.length, 'things to check')),
      h('div', { class: 'scroll' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, 'ID'), h('th', {}, 'Question or heading'), h('th', {}, 'Answer or text'), h('th', {}, 'From'), h('th', { class: 'num' }, 'Phrasings'))),
        h('tbody', {}, f.preview.map(r => h('tr', {}, h('td', { class: 'muted' }, r.id), h('td', {}, r.question), h('td', { class: 'trunc' }, r.answer || '(none)'),
          h('td', { class: 'small' }, r.kind === 'passage' ? h('span', { class: 'pill muted' }, isWeb(r.source) ? 'web page' : 'document') : null, ' ', r.source), h('td', { class: 'num' }, r.kind === 'passage' ? '–' : r.phrasings)))))),
      f.entries + f.passages > f.preview.length ? h('p', { class: 'muted small' }, 'Showing the first ' + f.preview.length + ' of ' + (f.entries + f.passages) + '.') : null),
    h('div', { class: 'row' },
      state === 'training' ? h('button', { class: 'btn primary', type: 'button', onclick: () => go('train') }, 'See training')
        : h('button', { class: 'btn primary', type: 'button', onclick: () => startTrain(false) }, state === 'trained' ? 'Train again' : 'Train my engine'),
      h('span', { class: 'muted small' }, 'Takes seconds to a few minutes. Nothing leaves this computer.')));
}
function modelCard() {
  const P = S.project, cur = P.settings.base, lang = P.faq.language, spec = P.bases.find(b => b.key === cur) || {};
  const pick = h('select', { id: 'base', 'aria-label': 'Model', style: 'max-width:340px', disabled: P.status.state === 'training', onchange: async e => {
    try { await api('/api/projects/' + S.pid + '/settings', { base: e.target.value }); await loadProject(); render(); notify('Model changed. Train again to use it.', 'good'); }
    catch (err) { notify(err.message); e.target.value = cur; }
  } }, P.bases.map(b => h('option', { value: b.key, selected: b.key === cur }, b.hf_id.split('/').pop() + (b.languages === 'multilingual' ? ' (multilingual)' : ''))));
  return card('Model', 'The small model that turns questions and text into numbers, so that similar meanings end up close together. It is the same model whatever the subject; changing it means training again.',
    lang.advice ? h('div', { class: 'banner warn' }, lang.advice) : null,
    h('div', { class: 'row' }, pick, h('span', { class: 'muted small' }, spec.label || '')),
    h('label', { class: 'small', style: 'display:flex;gap:8px;align-items:center;margin-top:8px' },
      h('input', { type: 'checkbox', checked: P.settings.finetune, disabled: P.status.state === 'training', onchange: async e => {
        try { await api('/api/projects/' + S.pid + '/settings', { finetune: e.target.checked }); await loadProject(); render(); notify('Changed. Train again to use it.', 'good'); } catch (err) { notify(err.message); e.target.checked = !e.target.checked; } } }),
      'Fine-tune the model on my FAQ (switch off to use the pretrained model exactly as it is: the control to compare against)'),
    h('p', { class: 'muted small' }, 'The text looks like: ' + lang.language + (lang.sure ? '' : ' (not sure: ' + lang.evidence + ')') + '.'));
}

// ---------- optional language model (the onboarding question and the settings)
const LLM_FORM = { choice: null, provider: '', model: '', base_url: '', key: '', models: [], msg: null, busy: false, caps: null, pid: null };
function llmFormFor(L) {
  if (LLM_FORM.pid !== S.pid) Object.assign(LLM_FORM, { pid: S.pid, choice: null, provider: L.provider || '', model: L.model || '', base_url: L.base_url || '', key: '', models: [], msg: null, busy: false, caps: null });
  return LLM_FORM;
}
function llmCard(onboarding) {
  const P = S.project, L = P.llm, F = llmFormFor(L), providers = S.providers || [];
  const spec = providers.find(p => p.key === F.provider) || {};
  const caps = F.caps || { polish: L.polish !== false, grounded: L.grounded !== false, assist: L.assist !== false };
  const redraw = () => { if (onboarding && S.project.llm.onboarded) return render(); const old = document.getElementById('llmCard'); if (old) old.replaceWith(llmCard(onboarding)); else render(); };  // once the question is answered the whole tab shows the settings card instead
  const body = () => ({ provider: F.provider, model: F.model, base_url: spec.needs_address ? F.base_url : '', ...(F.key ? { api_key: F.key } : {}) });
  const run = async fn => { F.busy = true; F.msg = null; redraw(); try { await fn(); } catch (e) { F.msg = { bad: true, text: e.message }; } F.busy = false; redraw(); };
  const validate = () => run(async () => {
    const r = await api('/api/projects/' + S.pid + '/llm/validate', body());
    F.models = r.models || [];
    F.msg = { bad: !r.ok, text: r.message + (r.ok ? '' : ' You can try again, or continue in local-only mode.') };
    if (r.ok && !F.model && F.models.length) F.model = F.models[0];
  });
  const save = enable => run(async () => {
    P.llm = await api('/api/projects/' + S.pid + '/llm', { ...body(), enabled: enable, polish: caps.polish, grounded: caps.grounded, assist: caps.assist });
    F.key = ''; F.choice = null; F.caps = null;
    F.msg = { bad: false, text: enable ? 'Saved. Questions in the Test tab can now use this model.' : 'Saved.' };
    notify(F.msg.text, 'good');
  });
  const skip = () => run(async () => { P.llm = await api('/api/projects/' + S.pid + '/llm/skip', {}); F.choice = null; notify('Local-only mode. You can add a language model later in the settings.', 'good'); });
  const toggle = () => run(async () => { P.llm = await api('/api/projects/' + S.pid + '/llm', { enabled: !L.enabled }); });
  const removeAll = () => { if (!confirm('Forget this business\'s language-model settings and key?')) return; run(async () => { P.llm = await api('/api/projects/' + S.pid + '/llm/remove', {}); Object.assign(F, { provider: '', model: '', base_url: '', key: '', models: [], choice: null }); }); };
  const field = (label, el) => h('label', { class: 'field' }, label, el);
  const form = h('div', { style: 'margin-top:10px' },
    field('Provider', h('select', { 'aria-label': 'Provider', disabled: F.busy, onchange: e => { F.provider = e.target.value; F.model = ''; F.models = []; F.msg = null; redraw(); } },
      h('option', { value: '' }, 'Choose a provider…'), providers.map(p => h('option', { value: p.key, selected: p.key === F.provider }, p.label)))),
    spec.needs_address ? field('Server address', h('input', { type: 'text', value: F.base_url, placeholder: 'http://localhost:11434/v1', autocomplete: 'off', 'aria-label': 'Server address', oninput: e => { F.base_url = e.target.value; } })) : null,
    F.provider ? field(spec.needs_key === false ? 'API key (optional for your own server)' : 'API key', h('input', { type: 'password', value: F.key, autocomplete: 'new-password', spellcheck: 'false', 'aria-label': 'API key',
      placeholder: L.has_key && L.provider === F.provider ? 'A key is saved. Type a new one to replace it.' : 'Paste the key here', oninput: e => { F.key = e.target.value; } })) : null,
    F.provider ? field('Model', h('div', {}, h('input', { type: 'text', list: 'llmModels', value: F.model, placeholder: F.models.length ? 'Choose a model' : 'Validate to see the models this key can use, or type one', autocomplete: 'off', 'aria-label': 'Model', oninput: e => { F.model = e.target.value; } }),
      h('datalist', { id: 'llmModels' }, F.models.map(m => h('option', { value: m }))))) : null,
    F.provider ? h('div', { class: 'small', style: 'margin:8px 0' }, h('b', {}, 'What the model may do '), h('span', { class: 'muted' }, '(each is checked against your own documents before it is used)'),
      [['polish', 'Reword a stored FAQ answer that already fully answers the question'], ['grounded', 'Combine passages that already support the answer'], ['assist', 'Suggest other searches when the first search finds too little']]
        .map(([k, t]) => h('label', { style: 'display:flex;gap:8px;align-items:center;margin-top:4px' }, h('input', { type: 'checkbox', checked: caps[k], onchange: e => { F.caps = { ...caps, [k]: e.target.checked }; } }), t))) : null,
    F.provider ? h('p', { class: 'small banner info' }, L.provider === F.provider && L.disclosure ? L.disclosure : 'Customer questions, and the FAQ answer or document passages found for them, are sent to the provider you choose to be worded or searched. Your key stays on this computer and is never shown again.') : null,
    F.msg ? h('div', { class: 'banner ' + (F.msg.bad ? 'warn' : 'good'), role: 'status' }, F.msg.text) : null,
    h('div', { class: 'row', style: 'margin-top:10px' },
      F.provider ? h('button', { class: 'btn', type: 'button', disabled: F.busy, onclick: validate }, F.busy ? 'Working…' : 'Check key and model') : null,
      F.provider ? h('button', { class: 'btn primary', type: 'button', disabled: F.busy, onclick: () => save(true) }, 'Save and turn on') : null,
      h('button', { class: 'btn link', type: 'button', disabled: F.busy, onclick: skip }, 'Continue in local-only mode')));
  const choices = h('div', { class: 'choices' },
    h('button', { class: 'choice' + (F.choice === 'local' ? ' on' : ''), type: 'button', disabled: F.busy, onclick: skip },
      h('b', {}, 'No, use local-only mode'), h('span', {}, 'The full engine without any key: MiniLM embeddings, keyword and meaning search, evidence checks, and an honest "I don\'t know". Nothing leaves this computer.')),
    h('button', { class: 'choice' + (F.choice === 'llm' ? ' on' : ''), type: 'button', disabled: F.busy, onclick: () => { F.choice = 'llm'; redraw(); } },
      h('b', {}, 'Yes, enable LLM integration'), h('span', {}, 'A model can word answers more naturally. It only rewords what your own knowledge already supports, and every sentence is checked before it is used.')));
  const status = L.enabled && L.usable ? h('span', { class: 'pill good' }, 'On: ' + L.label + ' · ' + L.model) : L.provider ? h('span', { class: 'pill muted' }, 'Off' + (L.has_key ? ' (key saved)' : '')) : h('span', { class: 'pill muted' }, 'Local-only mode');
  if (onboarding) {
    return h('div', { id: 'llmCard' }, card('Would you like to enable LLM integration for enhanced AI-generated answers?', 'Choose how this business answers. Either way the knowledge engine itself runs locally, and you can change this later in the settings.',
      choices, F.choice === 'llm' ? form : null));
  }
  const open = F.choice === 'llm';
  return h('div', { id: 'llmCard' }, card('Language model (optional)', 'The engine answers on its own. A model, if you turn one on, only rewords answers that your documents already support, and nothing it writes is used unless it is checked against them.',
    h('div', { class: 'row' }, status, h('span', { class: 'small muted' }, L.storage ? 'Key kept in ' + L.storage + '.' : ''),
      h('button', { class: 'btn', type: 'button', onclick: () => { F.choice = open ? null : 'llm'; redraw(); } }, open ? 'Close' : L.provider ? 'Change' : 'Set up'),
      L.provider ? h('button', { class: 'btn', type: 'button', disabled: F.busy || (!L.enabled && !L.has_key), onclick: toggle }, L.enabled ? 'Turn off' : 'Turn on') : null,
      L.has_key ? h('button', { class: 'btn link', type: 'button', onclick: removeAll }, 'Remove key') : null),
    L.problem && L.provider && !L.usable && L.enabled ? h('p', { class: 'small muted' }, 'Not usable yet: ' + L.problem + '.') : null,
    open ? form : null));
}


// ---------- choosing a table's columns by hand
const ROLES_BY_MODE = {
  faq: [['question', 'Question', true], ['answer', 'Answer', false], ['id', 'Id', false], ['category', 'Category', false], ['alt', 'Other ways of asking', false]],
  documents: [['title', 'Title', false], ['text', 'Text', true]],
  records: [['title', 'What names a row (optional)', false]],
};
const MODE_LABELS = { faq: 'A question and its answer', documents: 'A title and a text (a document per row)', records: 'A record: every column of a row becomes a sentence ("Pro plan, seats: 5.")' };
async function openColumns(name) {
  try {
    const info = await api('/api/projects/' + S.pid + '/columns', { name });
    const m = info.mapping || {};
    S.cols = { name, info, as: m.as || info.mode, pick: { ...info.detected, ...Object.fromEntries(Object.entries(m).filter(([k]) => k !== 'as')) } };
    render();
  } catch (e) { notify(e.message); }
}
function columnsCard() {
  const C = S.cols, info = C.info;
  const select = (role, label, required) => h('label', { class: 'field' }, label + (required ? ' *' : ''),
    h('select', { 'aria-label': label, onchange: e => { C.pick[role] = e.target.value || null; } },
      h('option', { value: '' }, '(none)'), info.columns.map(c => h('option', { value: c, selected: C.pick[role] === c }, c))));
  return card('Columns of ' + C.name, 'Header names do not have to match anything. Say how each row is read and which column plays which part; the file you uploaded is kept as it is, and the project reads a copy with your choices.',
    h('div', { class: 'banner info' }, 'As it stands this file is read as ' + (info.mode === 'faq' ? 'questions and answers' : info.mode === 'documents' ? 'documents' : 'records') + ' (' + info.rows + ' rows).'),
    h('label', { class: 'field' }, 'Read each row as', h('select', { 'aria-label': 'Read each row as', onchange: e => { C.as = e.target.value; render(); } },
      Object.entries(MODE_LABELS).map(([k, t]) => h('option', { value: k, selected: C.as === k }, t)))),
    h('div', { class: 'grid3' }, ROLES_BY_MODE[C.as].map(([role, label, required]) => select(role, label, required))),
    h('div', { class: 'scroll', style: 'margin-top:10px' }, h('table', {}, h('thead', {}, h('tr', {}, info.columns.map(c => h('th', {}, c)))),
      h('tbody', {}, info.sample.map(r => h('tr', {}, info.columns.map(c => h('td', { class: 'small' }, r[c] || ''))))))),
    h('div', { class: 'row', style: 'margin-top:12px' },
      h('button', { class: 'btn primary', type: 'button', onclick: applyColumns }, 'Use these columns'),
      h('button', { class: 'btn link', type: 'button', onclick: () => { S.cols = null; render(); } }, 'Cancel')));
}
async function applyColumns() {
  const C = S.cols;
  const keep = new Set(ROLES_BY_MODE[C.as].map(r => r[0]));
  const mapping = { as: C.as, ...Object.fromEntries(Object.entries(C.pick).filter(([k, v]) => keep.has(k) && v)) };
  try {
    await api('/api/projects/' + S.pid + '/columns/apply', { name: C.name, mapping });
    S.cols = null;
    await loadProject(); render();
    notify('Columns saved. Train again to use them.', 'good');
  } catch (e) { notify(e.message); }
}

function sourcesCard() {
  const f = S.project.faq;
  const input = h('input', { type: 'file', multiple: true, accept: ACCEPT, hidden: true, onchange: e => e.target.files.length && addFiles(e.target.files) });
  return card('Files in this project', 'Everything below is searched together. A FAQ file (a question and an answer per row) can train the model. A document (handbook, policy, web page, PDF) is cut into passages, embedded and searched like the FAQ; an answer built from them quotes their sentences, with a reference. A table with no question column (a price list, a schedule) is read row by row. Use Columns to change how a table is read.',
    h('div', { class: 'scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'File'), h('th', {}, 'Read as'), h('th', {}, 'Contents'), h('th', { class: 'num' }, 'Size'), h('th', {}, 'Version'), h('th', {}, ''))),
      h('tbody', {}, f.sources.map(x => h('tr', {}, h('td', {}, x.url ? h('a', { href: x.url, target: '_blank', rel: 'noopener noreferrer' }, clip(x.url, 70)) : x.name), h('td', {}, h('span', { class: 'pill ' + (x.kind === 'faq' ? '' : 'muted') }, x.url ? 'Web page' : x.kind === 'faq' ? 'FAQ' : x.kind === 'records' ? 'Table of values' : 'Document')),
        h('td', {}, x.faq_entries ? x.faq_entries + ' FAQ entries' : '', x.faq_entries && x.passages ? ' + ' : '', x.passages ? x.passages + ' passages' : ''),
        h('td', { class: 'num small' }, x.size > 1048576 ? (x.size / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(x.size / 1024)) + ' KB'),
        h('td', { class: 'small' }, 'v' + (x.version || 1), x.changed ? h('span', { class: 'muted' }, ' · +' + x.changed.added + ' −' + x.changed.removed + ' =' + x.changed.unchanged) : null),
        h('td', {}, x.table ? h('button', { class: 'btn link', type: 'button', onclick: () => openColumns(x.name) }, x.mapped ? 'Columns ✓' : 'Columns') : null, f.sources.length > 1 ? h('button', { class: 'btn link', type: 'button', onclick: () => removeSource(x.name) }, 'Remove') : null)))))),
    h('div', { class: 'row', style: 'margin-top:12px' }, h('button', { class: 'btn', type: 'button', onclick: () => input.click() }, '+ Add files'), input,
      h('span', { class: 'muted small' }, 'FAQ tables and documents. Adding a file with the same name replaces it (the old reading is not kept as current). Then update the search index, or train again.')),
    webRow(addWeb, true),
    h('p', { class: 'muted small' }, 'A web page is read once and kept here, as text, so the engine searches it with everything else. Add the same address again to read it afresh. Pages that build themselves with JavaScript, need a login, or ask not to be read (robots.txt) cannot be read: save such a page from your browser and upload it as a file instead.'));
}
async function addWeb(u, n) {
  const r = await api('/api/projects/' + S.pid + '/web', { url: u, pages: n });
  await loadProject(); render();
  notify('Read ' + r.added + (r.added === 1 ? ' page' : ' pages') + (r.notes.length ? '. Skipped: ' + r.notes.slice(0, 3).join('; ') : '') + '. Train again to include ' + (r.added === 1 ? 'it.' : 'them.'), 'good');
}
async function addFiles(list) {
  try {
    const files = await readFiles(list);
    await api('/api/projects/' + S.pid + '/sources', { files });
    await loadProject(); render();
    notify('Added ' + files.length + (files.length === 1 ? ' file' : ' files') + '. Train again to include ' + (files.length === 1 ? 'it.' : 'them.'), 'good');
  } catch (e) { notify(e.message); }
}
async function removeSource(name) {
  if (!confirm('Remove ' + name + ' from this project?')) return;
  try { await api('/api/projects/' + S.pid + '/sources/remove', { name }); await loadProject(); render(); } catch (e) { notify(e.message); }
}
function regressionCard() {
  const R = S.project.regression, input = h('input', { type: 'file', accept: '.csv', hidden: true, onchange: async e => {
    const f = e.target.files[0];
    if (!f) return;
    try { const r = await api('/api/projects/' + S.pid + '/regression', { text: await f.text() }); await loadProject(); render(); notify('Loaded ' + r.regression.total + ' regression questions.', 'good'); } catch (err) { notify(err.message); }
  } });
  const ok = R && !R.error;
  return card('Deployment regression suite (optional)', 'Questions a new version of the engine must still get right before it replaces the live one (the retrain script checks them). Same file format as the held-out questions. Because it influences which version goes live, it is not a test set: keep your held-out questions separate and use them only to report results.',
    R && R.error ? h('div', { class: 'banner bad' }, R.error) : null,
    ok ? facts([['Questions', R.total + ' usable (' + R.unique_questions + ' unique)']]) : null,
    h('div', { class: 'row', style: 'margin-top:12px' }, h('button', { class: 'btn', type: 'button', onclick: () => input.click() }, ok ? 'Replace the regression suite' : 'Choose a regression file (.csv)'), input,
      ok || (R && R.error) ? h('button', { class: 'btn link', type: 'button', onclick: async () => { await api('/api/projects/' + S.pid + '/regression/clear', {}); await loadProject(); render(); } }, 'Remove') : null));
}
function heldoutCard() {
  const H = S.project.heldout;
  const input = h('input', { type: 'file', accept: '.csv', hidden: true, onchange: e => e.target.files[0] && uploadHeldout(e.target.files[0]) });
  const ok = H && !H.error;
  return card('Locked benchmark: held-out test questions (optional)', 'Questions written separately from the FAQ, each with its right entry and a type. With them the report measures real held-out accuracy, by type, with refusal checked separately. Without them it can only run sanity checks generated from your own FAQ.',
    H && H.error ? h('div', { class: 'banner bad' }, H.error) : null,
    ok ? [facts([['Questions', H.total + ' usable questions: all of them are test questions, used only to measure the engine (never indexed, trained on, calibrated on or used to choose a model)'],
      ['Unique questions', H.unique_questions + ' of ' + H.rows_read + ' rows' + (H.exact_duplicates_removed ? ' (' + H.exact_duplicates_removed + ' repeated questions removed, not counted twice)' : '') + (H.near_duplicates ? ' · ' + H.near_duplicates + ' near-identical pairs kept, count them as one' : '')],
      ['Expected answers', H.expected_entries + ' different entries are expected somewhere · ' + H.abstentions + ' questions should be answered with “not in the knowledge base”'],
      ['By type', Object.entries(H.by_type).map(([t, d]) => t + ' ' + (d.dev + d.test)).join(' · ')],
      ['Leakage', H.removed_for_leakage ? H.removed_for_leakage + ' removed because they were near-duplicates of a training phrasing' : 'none: no question is a near-duplicate of a training phrasing']])] : null,
    h('div', { class: 'row', style: 'margin-top:12px' }, h('button', { class: 'btn', type: 'button', onclick: () => input.click() }, ok ? 'Replace the held-out file' : 'Choose a held-out file (.csv)'), input,
      ok || (H && H.error) ? h('button', { class: 'btn link', type: 'button', onclick: clearHeldout }, 'Remove') : null),
    h('details', {}, h('summary', {}, 'File format'),
      h('p', { class: 'small' }, 'A CSV with the columns query, entry_id, type and, optionally, split (dev or test; the default is test). Use the IDs shown in the table below.'),
      h('ul', { class: 'plain small' },
        h('li', {}, h('b', {}, 'type'), ': paraphrase, answer-terms, typo, short, ambiguous, out-of-scope-near, out-of-scope-far.'),
        h('li', {}, 'ambiguous questions list every acceptable entry in entry_id, separated by |.'),
        h('li', {}, 'out-of-scope questions (the FAQ cannot answer them) leave entry_id empty.'),
        h('li', {}, 'every row is a test question. The model and the "not in the FAQ" line are chosen on validation questions generated from your FAQ, never on these; a split column is ignored.'),
        h('li', {}, 'any question that is a near-duplicate of a training phrasing is removed and listed, so alternate phrasings cannot leak into the test.'))));
}
async function uploadHeldout(file) {
  try {
    const r = await api('/api/projects/' + S.pid + '/heldout', { text: await file.text() });
    await loadProject(); render();
    notify('Loaded ' + r.heldout.total + ' held-out questions' + (r.heldout.removed_for_leakage ? ' (' + r.heldout.removed_for_leakage + ' removed as near-duplicates of training phrasings)' : '') + '. Train again to use them.', 'good');
  } catch (e) { notify(e.message); }
}
async function clearHeldout() {
  try { await api('/api/projects/' + S.pid + '/heldout/clear', {}); await loadProject(); render(); } catch (e) { notify(e.message); }
}
async function startTrain(useFeedback) {
  if (S.project.status.state === 'trained' && !confirm('Training again replaces the current model. Your tests and judgements are kept. Continue?')) return;
  try {
    await api('/api/projects/' + S.pid + '/train', { use_feedback: !!useFeedback });
    await loadProject();
    go('train');
  } catch (e) { notify(e.message); }
}

// ---------- tab 2: train
function trainTab() {
  const P = S.project, st = P.status.state, hw = S.overview.hardware;
  const kids = [h('div', { id: 'notice' })];
  if (st === 'new') {
    kids.push(card('Ready to train', 'Here is what happens when you press the button.',
      h('ul', { class: 'plain' },
        h('li', {}, 'Your FAQ is turned into numbers (embeddings) by a small neural network, about 90 MB, downloaded once.'),
        h('li', {}, 'It is then nudged to understand your wording, with checks generated from your FAQ to see whether that helped.'),
        h('li', {}, 'If the nudge does not help, it says so and keeps the original model: it will not ship something worse.'),
        h('li', {}, 'Then you get a report, and you can test it yourself.')),
      h('div', { class: 'row', style: 'margin-top:14px' }, h('button', { class: 'btn primary', type: 'button', onclick: () => startTrain(false) }, 'Train my engine'))));
  } else if (st === 'failed') {
    kids.push(h('div', { class: 'banner bad', role: 'alert' }, h('b', {}, 'Training stopped. '), P.status.error || 'Unknown error.'),
      h('button', { class: 'btn primary', type: 'button', onclick: () => startTrain(false) }, 'Try again'));
  } else if (st === 'training') {
    const live = h('div', { id: 'live' });
    fillLive(live);
    kids.push(live);
  } else {
    const t = P.report.training, m = P.report.model, n = P.feedback ? usableFeedback(P.feedback.history) : 0;
    kids.push(card('Trained', 'Your engine is ready.',
      facts([['Time', secs(t.train_seconds)], ['Where', m.trained_on], ['Training steps', t.steps || 'none (skipped)'], ['Result', m.tuned ? 'Tuned model beat the starting model on the checks' : 'Starting model kept: tuning did not beat it on the checks']]),
      h('div', { class: 'row', style: 'margin-top:14px' },
        h('button', { class: 'btn primary', type: 'button', onclick: () => go('results') }, 'See the results'),
        h('button', { class: 'btn', type: 'button', onclick: () => go('test') }, 'Test it'),
        h('button', { class: 'btn', type: 'button', onclick: () => startTrain(false) }, 'Train again'),
        n ? h('button', { class: 'btn', type: 'button', onclick: () => startTrain(true) }, 'Train again using ' + n + ' of my judged questions') : null)));
  }
  kids.push(hardwareCard(hw));
  return h('div', {}, kids);
}
function usableFeedback(history) {
  const seen = new Set();
  for (const t of history) {
    if ((t.verdict === 'approve' && t.band !== 'none') || (t.verdict === 'reject' && t.correct_entry && t.correct_entry !== 'none')) seen.add(t.query.toLowerCase());
  }
  return seen.size;
}
function fillLive(el) {
  const P = S.project, st = P.status, pr = P.progress;
  const idx = Math.max(0, STAGES.findIndex(s => s[0] === st.stage));
  const children = [
    h('div', { class: 'head' }, h('div', { class: 'grow' }, h('h2', {}, STAGES[idx][1] + '…'), h('p', { class: 'muted small' }, 'You can leave this page; training continues.')), pr && pr.device ? h('span', { class: 'pill' }, pr.device) : null),
    h('ol', { class: 'steps', 'aria-label': 'Stages' }, STAGES.map((s, i) => h('li', { class: i < idx ? 'done' : i === idx ? 'now' : '' }, s[1]))),
  ];
  if (pr && pr.of) {
    const frac = Math.min(1, pr.step / pr.of);
    children.push(h('div', { class: 'progress', role: 'progressbar', 'aria-valuenow': pr.step, 'aria-valuemax': pr.of }, h('i', { style: 'width:' + Math.max(3, frac * 100) + '%' })),
      h('p', { class: 'muted small', style: 'margin-top:6px' }, 'Step ' + pr.step + ' of up to ' + pr.of + '. It stops early as soon as more training stops helping.'),
      h('div', { class: 'grid3', style: 'margin:12px 0' }, stat(num(pr.loss, 3), 'training loss (lower is better)'), stat(pr.samples_per_s ? Math.round(pr.samples_per_s) : '…', 'examples per second'), stat(secs(pr.eta_s), 'at most left'),
        stat(secs(pr.elapsed_s), 'elapsed'), stat(pr.mem_mb ? Math.round(pr.mem_mb) + ' MB' : '…', 'memory in use'), stat(pr.evals.length ? pct(pr.evals[pr.evals.length - 1].dev_hit1) : '…', 'right first on the check')),
      h('div', { class: 'grid2' },
        h('div', {}, h('h3', {}, 'Training loss'), lineChart(pr.curve.map(p => ({ x: p.step, y: p.loss })), { label: 'Training loss by step', yFmt: v => v.toFixed(2) })),
        h('div', {}, h('h3', {}, 'Right first on the check'), lineChart(pr.evals.map(p => ({ x: p.step, y: p.dev_hit1 })), { label: 'Right first on the check by step', yMin: 0, yMax: 1, color: 'var(--good)', yFmt: v => pct(v) }))));
  }
  const ws = (S.overview.hardware.warnings || []);
  if (ws.length) children.push(h('div', { class: 'banner warn' }, ws.join(' ')));
  el.replaceChildren(card(null, null, children));
}
function hardwareCard(hw) {
  return card('This computer', 'Training uses what is here. Nothing is sent anywhere.',
    facts([['Processor', hw.cpu + ' (' + hw.cores + ' cores)'], ['Memory', gb(hw.ram_gb) + ' (' + gb(hw.ram_free_gb) + ' free' + (hw.swap_used_gb ? ', ' + gb(hw.swap_used_gb) + ' swap in use' : '') + ')'],
      ['Accelerator', hw.accelerator], ['Disk free', gb(hw.disk_free_gb)], ['Software', 'PyTorch ' + hw.torch + ' · Python ' + hw.python + ' · ' + hw.system]]),
    (hw.warnings || []).map(w => h('div', { class: 'banner warn', style: 'margin:10px 0 0' }, w)));
}

// ---------- tab 3: results
const METRICS = [['hit@1', 'Right first', 'p'], ['p@3', 'P@3', 'p'], ['hit@3', 'R@3', 'p'], ['p@5', 'P@5', 'p'], ['hit@5', 'R@5', 'p'], ['mrr@10', 'MRR@10', 'n'], ['ndcg@10', 'nDCG@10', 'n']];
const HMETRICS = [['hit@1', 'Hit@1', 'p'], ['p@3', 'P@3', 'p'], ['r@3', 'R@3', 'p'], ['p@5', 'P@5', 'p'], ['r@5', 'R@5', 'p'], ['mrr@10', 'MRR@10', 'n'], ['ndcg@10', 'nDCG@10', 'n']];
const HNAMES = { bm25: 'Keyword search', base: 'Base model (unchanged)', tuned: 'Trained model' };
function resultsTab() {
  const P = S.project, R = P.report, mode = R.mode || 'selfcheck';
  const kids = [h('div', { id: 'notice' })];
  if (P.stale) kids.push(staleBanner());
  kids.push(...(mode === 'heldout' ? heldoutCards(R) : mode === 'documents' ? [card('What we found', null, h('ul', { class: 'plain' }, R.verdict.map((v, i) => h('li', { style: i === 0 ? 'font-weight:600' : '' }, v))))] : selfcheckCards(R)));
  kids.push(...commonCards(R));
  return h('div', {}, kids);
}
function verdictList(R) { return h('ul', { class: 'plain', style: 'margin-bottom:14px' }, R.verdict.map((v, i) => h('li', { style: i === 0 ? 'font-weight:600' : '' }, v))); }
function selfcheckCards(R) {
  const sc = R.selfcheck, sys = sc.systems, names = sc.names, lo = sc.loss, off = sc.off_topic.tuned, kids = [];
  kids.push(card('What we found', null, verdictList(R),
    h('div', { class: 'grid3' }, ['tuned', 'base', 'bm25'].map((k, i) => stat(pct(sys[k]['hit@1']), names[k] + ': right entry first' + (i === 0 ? ' (the engine you will use)' : ''), i === 0)))));
  kids.push(h('div', { class: 'banner info' }, h('b', {}, 'How to read these numbers. '), 'They come from questions generated from your FAQ file (typos, shortened versions, answer keywords, off-topic questions), not from real customers, so they are sanity checks, not a promise. For an honest held-out measurement add held-out questions on the Files tab; the real test is the Test tab.'));
  kids.push(card('Scores', 'Every question has exactly one right entry. Higher is better.',
    h('div', { class: 'scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'System'), METRICS.map(([, label]) => h('th', { class: 'num' }, label)))),
      h('tbody', {}, ['bm25', 'base', 'tuned'].map(k => h('tr', { class: k === 'tuned' ? 'me' : '' }, h('td', {}, names[k]),
        METRICS.map(([key, , kind]) => h('td', { class: 'num' }, kind === 'p' ? pct(sys[k][key]) : num(sys[k][key], 3)))))))),
    h('p', { class: 'muted small', style: 'margin-top:8px' }, sc.counts ? 'Measured on ' + Object.entries(sc.counts).filter(([k]) => k !== 'off-topic').reduce((a, [, v]) => a + v, 0) + ' generated questions with a known right entry.' : ''),
    liftLine(sc.lift), glossary()));
  const srcRows = Object.entries(sc.by_source);
  kids.push(h('div', { class: 'grid2' },
    card('By kind of question', 'Where it is strong and where it is weak (right entry first).',
      h('table', {}, h('tbody', {}, srcRows.map(([name, x]) => h('tr', {}, h('td', {}, h('b', {}, name), h('div', { class: 'muted small' }, SOURCES[name] || ''), h('div', { class: 'muted small' }, x.n + (x.n === 1 ? ' question' : ' questions'))),
        h('td', { style: 'min-width:150px' }, ['tuned', 'base', 'bm25'].map(k => h('div', { class: 'row', style: 'gap:8px;flex-wrap:nowrap' }, h('span', { class: 'small', style: 'width:92px' }, names[k]), bar(x[k], k === 'tuned' ? 'good' : ''), h('span', { class: 'small num', style: 'width:38px;text-align:right' }, pct(x[k])))))))))),
    card('Questions that are not in the FAQ', 'Off-topic questions (weather, trivia) it should refuse.',
      h('div', { class: 'grid2', style: 'margin:8px 0' }, stat(pct(off.off_topic_rejected), 'correctly told "not in the FAQ"'), stat(pct(off.real_questions_refused), 'of in-FAQ questions wrongly refused')),
      h('p', { class: 'muted small' }, 'The "not in the FAQ" line is a score of ' + num(off.floor) + ' or less. Median score for right matches: ' + num(off.median_score_right) + '; for off-topic questions: ' + num(off.median_score_off_topic) + '. Real near-miss questions (for example asking for something you do not sell) are harder: try some on the Test tab.'))));
  kids.push(card('Loss: did it learn or memorise?', 'Lower is better. A test loss far above the train loss means memorising.',
    h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, ''), h('th', { class: 'num' }, 'Train'), h('th', { class: 'num' }, 'Dev'), h('th', { class: 'num' }, 'Check'))),
      h('tbody', {}, h('tr', {}, h('td', {}, names.base), h('td', { class: 'num' }, '–'), h('td', { class: 'num' }, num(lo.dev_base, 3)), h('td', { class: 'num' }, num(lo.test_base, 3))),
        h('tr', { class: 'me' }, h('td', {}, names.tuned), h('td', { class: 'num' }, num(lo.train_tuned, 3)), h('td', { class: 'num' }, num(lo.dev_tuned, 3)), h('td', { class: 'num' }, num(lo.test_tuned, 3))))),
    h('p', { class: 'muted small', style: 'margin-top:8px' }, 'Train: the last training window. Dev: held back to decide when to stop. Check: the generated questions above.')));
  return kids;
}
function glossary() {
  return h('details', {}, h('summary', {}, 'What do these mean?'), h('dl', { class: 'facts', style: 'margin-top:10px' }, GLOSSARY.map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)])));
}
function metricRows(overall, ship) {
  return ['bm25', 'base', 'tuned'].map(k => h('tr', { class: k === ship ? 'me' : '' }, h('td', {}, HNAMES[k] + (k === ship ? ' (the default)' : '')),
    HMETRICS.map(([key, , kind]) => h('td', { class: 'num' }, kind === 'p' ? pct(overall[k][key]) : num(overall[k][key], 3)))));
}
function heldoutCards(R) {
  const H = R.heldout, t = H.test, dec = H.decision, eff = H.effect, c = H.counts, ship = dec.ship, kids = [];
  const head = h('thead', {}, h('tr', {}, h('th', {}, 'System'), HMETRICS.map(([, label]) => h('th', { class: 'num' }, label))));
  kids.push(card('What we found', null, verdictList(R), h('div', { class: 'grid3' }, ['tuned', 'base', 'bm25'].map(k => stat(pct(t.overall[k]['hit@1']), HNAMES[k] + ': right entry first' + (k === ship ? ' (the default)' : ''), k === ship)))));
  kids.push(h('div', { class: 'banner info' }, h('b', {}, 'How to read these numbers. '), 'They come from ' + t.n + ' held-out test questions written separately from the training phrasings and checked for near-duplicates of them and of the validation questions. The model and the refusal line were chosen on ' + H.dev.n + ' validation questions generated from your FAQ; the held-out questions were not used for any choice. Written by hand, not by customers: the Test tab is still the real test.'));
  kids.push(card('Test questions: scores', 'Every question has one or more acceptable entries. Higher is better.',
    h('div', { class: 'scroll' }, h('table', {}, head, h('tbody', {}, metricRows(t.overall, ship)))), glossary(),
    H.dev.n ? h('details', {}, h('summary', {}, 'Validation questions (generated from your FAQ; used to choose, never reported as results)'), h('div', { class: 'scroll' }, h('table', {}, head, h('tbody', {}, metricRows(H.dev.overall, ship))))) : null));
  kids.push(card('By type of question', 'Test questions, side by side. Where it is strong and where it is weak.',
    h('div', { class: 'scroll' }, h('table', {}, head, h('tbody', {}, Object.entries(t.by_type).map(([type, d]) => [
      h('tr', {}, h('td', { colspan: 8, style: 'padding-top:14px' }, h('b', {}, H.labels[type]), h('span', { class: 'muted small' }, '  ' + d.n + ' questions'))),
      ['bm25', 'base', 'tuned'].map(k => h('tr', { class: k === ship ? 'me' : '' }, h('td', {}, HNAMES[k]), HMETRICS.map(([key, , kind]) => h('td', { class: 'num' }, kind === 'p' ? pct(d[k][key]) : num(d[k][key], 3)))))])))),
    h('p', { class: 'muted small', style: 'margin-top:8px' }, 'Right first, P@K and R@K are shares of questions; with several acceptable entries (ambiguous questions) R@K is the share of acceptable entries found.')));
  if (eff) {
    const row = (key, label, pctUnit) => { const e = eff[key], f = v => pctUnit ? (100 * v).toFixed(1) + '%' : v.toFixed(3), g = v => (pctUnit ? (100 * v).toFixed(1) : v.toFixed(3)); return h('tr', {}, h('td', {}, label), h('td', { class: 'num' }, f(e.base)), h('td', { class: 'num' }, f(e.tuned)), h('td', { class: 'num' }, (e.diff >= 0 ? '+' : '') + g(e.diff) + (pctUnit ? ' pts' : '')), h('td', { class: 'num' }, (e.low >= 0 ? '+' : '') + g(e.low) + ' to ' + (e.high >= 0 ? '+' : '') + g(e.high))); };
    kids.push(card('Did training help?', 'The trained model against the unchanged base model, on the same ' + eff.n + ' test questions.',
      h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Metric'), h('th', { class: 'num' }, 'Base'), h('th', { class: 'num' }, 'Trained'), h('th', { class: 'num' }, 'Difference'), h('th', { class: 'num' }, '95% interval'))),
        h('tbody', {}, row('hit@1', 'Hit@1', true), row('mrr@10', 'MRR@10', false), row('ndcg@10', 'nDCG@10', false))),
      h('div', { class: 'banner ' + (dec.improvement_demonstrated ? 'good' : 'info'), style: 'margin:12px 0 0' }, h('b', {}, 'Default model: ' + (ship === 'tuned' ? 'the trained model. ' : 'the unchanged base model. ')), dec.reason.charAt(0).toUpperCase() + dec.reason.slice(1) + '.'),
      h('p', { class: 'muted small', style: 'margin-top:8px' }, 'An improvement is only claimed when the interval excludes zero. The trained model is the default only if it beat the base on dev and is not worse on test.')));
  }
  kids.push(answeringCard(H));
  kids.push(lossCard(R, H));
  kids.push(hybridCard(H));
  kids.push(refusalCard(H));
  kids.push(card('Data checks', 'What keeps the test honest.',
    h('ul', { class: 'plain' },
      h('li', {}, c.total + ' held-out test questions kept from ' + c.rows_read + ' rows: ' + c.unique_questions + ' unique' + (c.exact_duplicates_removed ? ', ' + c.exact_duplicates_removed + ' repeated questions removed (not counted twice)' : '') + ', ' + c.abstentions + ' that should be answered with “not in the knowledge base”, ' + c.expected_entries + ' different entries expected, ' + Object.keys(c.by_type).length + ' kinds of question.'),
      h('li', {}, c.removed_for_leakage + ' removed because they were near-duplicates of a training phrasing' + (c.removed_for_leakage ? ':' : '.')),
      H.removed.slice(0, 8).map(x => h('li', { class: 'small' }, '“' + x.query + '” ≈ “' + x.matches_training_phrasing + '”')),
      H.warnings.map(w => h('li', { class: 'small' }, w)),
      h('li', {}, 'Training uses the FAQ entries and all their phrasings; the held-out file is separate, so alternate phrasings never end up on both sides.'),
      H.owner_typed ? h('li', {}, H.owner_typed + ' questions were typed by the owner in the app.') : null)));
  return kids;
}
function answeringCard(H) {
  const A = H.answering || {};
  if (!A.all) return null;
  const order = ['answered correctly', 'partial answer', 'correct abstention', 'missed answer', 'answered from the wrong source', 'stored FAQ answer for a document question', 'fabricated answer'];
  const good = new Set(['answered correctly', 'correct abstention']);
  const cell = (d, v) => h('td', { class: 'num' }, pct((d.verdicts[v] || 0) / d.n));
  return card('Evidence-aware answers on the test questions', 'What the engine would actually say (the FAQ answer only when it supports the question, otherwise the documents, otherwise “not covered”), judged against what each question should get. A fabricated answer is one given to a question that should be declined.',
    h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Kind of question'), h('th', { class: 'num' }, 'n'), order.map(v => h('th', { class: 'num' }, v)))),
      h('tbody', {}, Object.entries(A).sort((x, y) => (x[0] === 'all' ? -1 : y[0] === 'all' ? 1 : 0)).map(([k, d]) => h('tr', { class: k === 'all' ? 'me' : '' }, h('td', {}, k === 'all' ? 'All' : (H.labels[k] || k)), h('td', { class: 'num' }, d.n), order.map(v => cell(d, v))))))),
    h('p', { class: 'muted small', style: 'margin-top:8px' }, 'Judged from wording (see the limits in runtime/evidence.py), so a different wording of the same fact can be missed.'));
}
function lossCard(R, H) {
  const t = R.training, v = (t.dev_before && t.dev_after) ? [t.dev_before.loss, t.dev_after.loss] : null, lt = H.loss_test || {};
  return card('Loss: train, validation and test, each on its own questions', 'Lower is better. Three separate sets: training uses your FAQ’s phrasings, validation (generated from your FAQ) stops training and picks the model, the held-out test questions are only measured.',
    h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Set'), h('th', {}, 'What it is'), h('th', { class: 'num' }, 'Pretrained'), h('th', { class: 'num' }, 'Fine-tuned'))),
      h('tbody', {},
        h('tr', {}, h('td', {}, 'Train'), h('td', { class: 'small' }, 'contrastive loss on the last training steps (a different loss, so not comparable in size with the two below)'), h('td', { class: 'num' }, '–'), h('td', { class: 'num' }, t.train_loss_last == null ? 'n/a' : num(t.train_loss_last, 3))),
        h('tr', {}, h('td', {}, 'Validation'), h('td', { class: 'small' }, 'listwise loss on ' + H.dev.n + ' generated questions'), h('td', { class: 'num' }, v ? num(v[0], 3) : 'n/a'), h('td', { class: 'num' }, v ? num(v[1], 3) : 'n/a')),
        h('tr', { class: 'me' }, h('td', {}, 'Test'), h('td', { class: 'small' }, 'the same listwise loss on the held-out questions with one right entry'), h('td', { class: 'num' }, lt.base == null ? 'n/a' : num(lt.base, 3)), h('td', { class: 'num' }, lt.tuned == null ? 'n/a' : num(lt.tuned, 3))))));
}
function hybridCard(H) {
  const Y = H.hybrid;
  if (!Y || !Y.available) return null;
  const names = [['keyword', 'Keyword only'], ['embedding', 'Embedding only (' + (H.decision.ship === 'tuned' ? 'fine-tuned' : 'pretrained') + ')'], ['hybrid', 'Hybrid (keyword + embedding)']];
  const row = (label, d) => h('tr', {}, h('td', {}, label), HMETRICS.map(([key, , kind]) => h('td', { class: 'num' }, kind === 'p' ? pct(d[key]) : num(d[key], 3))));
  return card('Hybrid retrieval (experiment)', 'The same held-out test questions for all three. The keyword share (' + Y.alpha + ') was chosen on the validation questions, never on these. The engine itself still uses the embedding alone.',
    h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'System'), HMETRICS.map(([, label]) => h('th', { class: 'num' }, label)))),
      h('tbody', {}, names.map(([k, label]) => row(label, Y.overall[k]))))),
    h('details', {}, h('summary', {}, 'By kind of question'), h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Kind'), h('th', {}, 'System'), h('th', { class: 'num' }, 'Right first'), h('th', { class: 'num' }, 'MRR@10'))),
      h('tbody', {}, Object.entries(Y.by_type).map(([t, d]) => names.map(([k, label], i) => h('tr', {}, h('td', {}, i === 0 ? (H.labels[t] || t) + ' (' + d.n + ')' : ''), h('td', {}, label), h('td', { class: 'num' }, pct(d[k]['hit@1'])), h('td', { class: 'num' }, num(d[k]['mrr@10'], 3))))))))),
    h('p', { class: 'small', style: 'margin-top:8px' }, Y.verdict));
}
function refusalCard(H) {
  const ship = H.decision.ship, sweep = H.refusal[ship].sweep;
  return card('Refusal: does it say “not in the FAQ” when it should?', 'Validated separately from retrieval. The line is chosen on validation questions only; test questions report it. Near-topic = about your subject but not covered; unrelated = anything else.',
    h('div', { class: 'scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'Model'), h('th', { class: 'num' }, 'Line'), h('th', { class: 'num' }, 'Near-topic refused'), h('th', { class: 'num' }, 'Unrelated refused'), h('th', { class: 'num' }, 'Not labelled, refused'), h('th', { class: 'num' }, 'Answerable wrongly refused'), h('th', { class: 'num' }, 'AUROC near / unrelated'))),
      h('tbody', {}, ['base', 'tuned'].map(k => { const x = H.refusal[k], te = x.test; return h('tr', { class: k === ship ? 'me' : '' }, h('td', {}, HNAMES[k] + (k === ship ? ' (default)' : '')), h('td', { class: 'num' }, num(x.floor) + ' · ' + x.floor_source),
        h('td', { class: 'num' }, pct(te.off_topic_near_refused) + ' (' + te.n_near + ')'), h('td', { class: 'num' }, pct(te.off_topic_far_refused) + ' (' + te.n_far + ')'), h('td', { class: 'num' }, pct(te.off_topic_other_refused) + ' (' + te.n_other + ')'), h('td', { class: 'num' }, pct(te.in_scope_refused) + ' (' + te.n_in_scope + ')'), h('td', { class: 'num' }, num(te.auroc_near) + ' / ' + num(te.auroc_far))); })))),
    h('details', {}, h('summary', {}, 'Threshold sweep for the default model'), h('div', { class: 'scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'Line'), h('th', { class: 'num' }, 'Near-topic refused'), h('th', { class: 'num' }, 'Unrelated refused'), h('th', { class: 'num' }, 'Answerable wrongly refused'))),
      h('tbody', {}, sweep.map(x => h('tr', {}, h('td', {}, x.floor.toFixed(2)), h('td', { class: 'num' }, pct(x.off_topic_near_refused)), h('td', { class: 'num' }, pct(x.off_topic_far_refused)), h('td', { class: 'num' }, pct(x.in_scope_refused)))))))),
    h('p', { class: 'muted small', style: 'margin-top:8px' }, 'The line is configurable: set it yourself on the Test tab.'));
}
function commonCards(R) {
  const P = S.project, m = R.model, t = R.training, th = R.thresholds, hl = R.faq.health, kids = [];
  if (R.faq.sources && (R.faq.sources.length > 1 || R.faq.passages)) {
    kids.push(card('Sources', 'Everything the engine searches.',
      h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'File'), h('th', {}, 'Read as'), h('th', {}, 'Contents'))),
        h('tbody', {}, R.faq.sources.map(x => h('tr', {}, h('td', {}, x.name), h('td', {}, x.kind === 'faq' ? 'FAQ' : isWeb(x.name) ? 'Web page' : x.kind === 'records' ? 'Table of values' : 'Document'), h('td', {}, (x.faq_entries ? x.faq_entries + ' FAQ entries' : '') + (x.faq_entries && x.passages ? ' + ' : '') + (x.passages ? x.passages + ' passages' : '')))))),
      R.faq.passages ? h('p', { class: 'muted small', style: 'margin-top:8px' }, 'Documents and web pages are embedded and searched together with the FAQ, and answers are built from them as evidence (see the Test page). Training uses only the FAQ entries, since a passage is not a question and answer pair. Passages are not scored unless held-out questions name them.') : null));
  }
  kids.push(card('Training', t.skipped ? 'Skipped: ' + t.skipped : t.beat_untuned_on_dev ? 'Stopped at the best point, not the last one.' : 'No point beat the starting model, so the starting model is what you get.',
    facts([['Weights moved', t.weights && t.weights.kept ? (t.weights.kept.relative_l2 > 0 ? 'yes: ' + (100 * t.weights.kept.relative_l2).toFixed(2) + '% from the pretrained weights (' + t.weights.kept.tensors_changed + ' of ' + t.weights.kept.tensors_total + ' tensors)' : 'no: the pretrained model is what you get' + (t.weights.after_last_step ? ' (training did move the weights ' + (100 * t.weights.after_last_step.relative_l2).toFixed(2) + '%, but no checkpoint beat the pretrained model on validation)' : '')) : 'n/a'], ['Steps', t.steps], ['Best at step', t.beat_untuned_on_dev ? t.best_step : 'none beat the starting model'], ['Time', secs(t.train_seconds)], ['On', m.trained_on], ['Peak memory', t.peak_memory_mb ? Math.round(t.peak_memory_mb) + ' MB' : 'n/a']]),
    R.curve.length > 1 ? h('div', { class: 'grid2', style: 'margin-top:10px' },
      h('div', {}, h('h3', {}, 'Training loss'), lineChart(R.curve.map(c => ({ x: c.step, y: c.train_loss })), { label: 'Training loss', yFmt: v => v.toFixed(2) })),
      h('div', {}, h('h3', {}, 'Right first on the validation questions'), lineChart(R.curve.map(c => ({ x: c.step, y: c['dev_hit@1'] })), { label: 'Right first on the validation questions', yMin: 0, yMax: 1, color: 'var(--good)', yFmt: v => pct(v) }))) : null));
  kids.push(card('Embeddings: your FAQ as a map', m.vectors + ' vectors of ' + m.dimension + ' numbers (' + num(m.vectors_mb, 2) + ' MB) cover everything searched. Entries about similar things sit close together.',
    R.map ? h('div', { class: 'grid2' }, entryMap(R.map), h('div', {}, h('p', { class: 'small' }, 'Each dot is one FAQ entry, flattened from ' + m.dimension + ' dimensions to 2. When a customer types a question it is turned into the same kind of numbers and the nearest entry wins. Hover a dot to see its question.'),
      h('p', { class: 'small muted' }, 'Dots that sit on top of each other are entries customers may confuse.'))) : h('p', { class: 'muted' }, 'Needs at least 3 FAQ entries.')));
  kids.push(card('FAQ health', 'Problems that hurt any engine, found without needing customers.',
    hl.confusable.length ? h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Closest pairs of entries'), h('th', { class: 'num' }, 'Similarity'))),
      h('tbody', {}, hl.confusable.map(p => h('tr', {}, h('td', {}, h('div', {}, p.a_question), h('div', { class: 'muted' }, p.b_question)), h('td', { class: 'num' }, h('span', { class: 'pill ' + (p.likely_confused ? 'warn' : 'muted') }, num(p.similarity))))))) ) : null,
    hl.advice.length ? h('ul', { class: 'plain' }, hl.advice.map(a => h('li', {}, a))) : h('p', { class: 'muted' }, 'Nothing to flag.')));
  kids.push(h('div', { class: 'grid2' },
    card('The engine', 'What you would be shipping.',
      facts([['Base model', m.hf_id + ' · licence ' + m.license + (m.tuned ? ' · fine-tuned on your FAQ' : ' · unchanged')], ['Size', m.params_millions + 'M parameters · ' + m.size_fp32_mb + ' MB'], m.latency_ms_p50 ? ['Speed', num(m.latency_ms_p50, 1) + ' ms per question (p95 ' + num(m.latency_ms_p95, 1) + ' ms)'] : null,
        m.latency_note ? ['', h('span', { class: 'muted small' }, m.latency_note)] : null, ['Not in the FAQ below', 'a score of ' + num(th.floor) + ' (' + (th.source || 'calculated') + ')'],
        ['Answer without an LLM', th.direct == null ? 'off' + (th.direct_reason ? ': ' + th.direct_reason : '') : 'from a score of ' + num(th.direct) + ' (' + th.source + ')']])),
    hardwareCard(R.hardware && R.hardware.cpu ? R.hardware : S.overview.hardware)));
  kids.push(h('div', { class: 'row' },
    h('button', { class: 'btn primary', type: 'button', onclick: () => go('test') }, 'Test it yourself'),
    exportControl(),
    h('a', { class: 'btn', href: '/api/projects/' + P.id + '/report.md', download: '' }, 'Download this report')));
  return kids;
}
function liftLine(lift) {
  const d = lift['base->tuned'];
  if (!d) return null;
  const [mean, lo, hi] = d, clear = lo > 0 || hi < 0;
  return h('p', { class: 'small', style: 'margin-top:6px' }, 'Your engine vs the starting model, right-first: ' + (mean >= 0 ? '+' : '') + (mean * 100).toFixed(1) + ' points (95% range ' + (lo * 100).toFixed(1) + ' to ' + (hi * 100).toFixed(1) + '). ' + (clear ? '' : 'The range includes zero: no real difference on these checks.'));
}
function exportControl() {
  const ex = S.project.export;
  if (ex.state === 'running') return h('button', { class: 'btn', type: 'button', disabled: true }, 'Building the package…');
  if (ex.state === 'done') return h('a', { class: 'btn good', href: '/api/projects/' + S.pid + '/package.zip', download: '', title: 'Runs on any computer without this website or an LLM' }, 'Download engine package (' + num(ex.zip_mb, 1) + ' MB)');
  return h('span', { class: 'row' },
    h('button', { class: 'btn', type: 'button', onclick: startExport, title: 'A folder that runs on any computer without this website or an LLM' }, 'Prepare engine package'),
    ex.state === 'failed' ? h('span', { class: 'pill bad' }, ex.error || 'Export failed') : null);
}
async function startExport() {
  try { await api('/api/projects/' + S.pid + '/export', {}); await loadProject(); render(); maybePoll(); } catch (e) { notify(e.message); }
}

// ---------- tab 4: test
function testTab() {
  const P = S.project, entries = P.faq.entry_list;
  const input = h('input', { type: 'text', id: 'q', placeholder: 'Type a question a customer might ask…', autocomplete: 'off', maxlength: 500, 'aria-label': 'A question to test',
    onkeydown: e => { if (e.key === 'Enter') ask(input.value); } });
  setTimeout(() => input.focus(), 0);
  const view = h('div', {},
    h('div', { id: 'notice' }),
    card('Try it', 'Ask it anything a customer might. Then say whether the answer is right: your judgements become the real measure of accuracy.',
      h('div', { class: 'ask' }, input, h('button', { class: 'btn primary', type: 'button', onclick: () => ask(input.value) }, 'Ask')),
      h('div', { class: 'chips' }, entries.filter(e => e.kind === 'faq').slice(0, 4).map(e => h('button', { class: 'chip', type: 'button', onclick: () => { input.value = e.question; ask(e.question); } }, clip(e.question, 40))))),
    h('div', { id: 'resultBox' }), h('div', { id: 'tallyBox' }), h('div', { id: 'historyBox' }));
  setTimeout(() => { fillResult(); fillTally(); fillHistory(); }, 0);
  return view;
}
async function ask(text) {
  text = (text || '').trim();
  if (!text) return;
  try {
    S.test = await api('/api/projects/' + S.pid + '/test', { query: text });
    S.pending = null;
    fillResult();
  } catch (e) { notify(e.message); }
}
const OUTCOMES = {
  faq: ['good', 'Answered by the FAQ', 'The stored answer supports every part of the question.'],
  grounded: ['info', 'Answered from the documents', 'The FAQ did not fully answer it; this is assembled only from the passages below.'],
  partial: ['warn', 'Answered in part', 'Part of the question is supported; the rest is not covered by anything available.'],
  insufficient: ['warn', 'Not answered', 'Neither the FAQ nor the documents answer it.'],
  conflict: ['bad', 'Sources disagree', 'Two sources say different things; nothing is chosen for you.'],
  clarification: ['info', 'Needs a clarification', 'The question is too unspecific to answer; it asks for more detail instead of guessing.'],
};
function evidenceBox(R) {
  if (!R) return null;
  const [cls, title, why] = OUTCOMES[R.outcome] || ['info', R.outcome, ''];
  const signal = (label, value, note) => h('div', { class: 'small' }, h('b', {}, label + ': '), value, note ? h('span', { class: 'muted' }, ' ' + note) : null);
  return h('div', { class: 'banner ' + cls, style: 'margin-top:12px' },
    h('div', {}, h('b', {}, title + '. '), why),
    R.answer ? h('div', { style: 'margin:8px 0;white-space:pre-wrap' }, R.answer) : null,
    R.message ? h('div', { style: 'margin:8px 0' }, R.message) : null,
    R.evidence_class ? h('div', { class: 'small' }, 'Evidence: ' + R.evidence_class.replace('_', ' ')) : null,
    (R.partial || []).map(x => h('div', { class: 'small' }, '• ' + x.part + ': ' + x.text + ' (' + x.reference + ')')),
    (R.conflicts || []).map(c => h('div', { class: 'small' }, '• ' + c.between.join('  vs  ') + ': ' + c.reason)),
    (R.sources || []).map(x => h('div', { class: 'small' }, '[' + x.ref + '] ' + (x.kind === 'faq' ? 'FAQ: ' : isWeb(x.source) ? 'Web page: ' : 'Document: ') + x.location + (x.kind === 'faq' ? '' : ' · ' + clip(x.text || '', 140)))),
    h('div', { class: 'small', style: 'margin-top:8px' }, signal('Retrieval similarity', num(R.retrieval_similarity), '(how alike the best FAQ question is; not a probability and not how sure the answer is)'),
      signal('Answer sufficiency', pct(R.answer_sufficiency), '(share of the question’s parts the FAQ answer supports)'), signal('Evidence support', pct(R.evidence_support), '(share supported by what is used)'),
      signal('Response confidence', R.response_confidence, '(a category from rules, not a calibrated probability)')),
    modelBox(R), decisionBox(R), diagnosticsBox(R));
}
const GENERATED = { deterministic: 'the engine alone (no language model)', model: 'a language model, then checked claim by claim', deterministic_fallback: 'the engine alone, after the model could not be used (not a model\u2019s answer)',
  deterministic_no_consent: 'the engine alone, because this business has not agreed to send data to that provider', unavailable: 'nothing: the model was unavailable and no fallback was allowed', mock: 'a test double' };
function decisionBox(R) {
  const X = R.execution;
  if (!X) return null;
  const plan = R.query_plan, ev = R.evidence || [], suff = R.sufficiency, cv = R.claim_verification, claims = R.claims || [];
  const row = (label, value) => value == null || value === '' ? null : h('div', { class: 'small' }, h('b', {}, label + ': '), value);
  const list = a => (a || []).length ? a.join('; ') : 'none';
  const shadowed = X.shadow;
  return h('details', { style: 'margin-top:8px' }, h('summary', { class: 'small' }, 'How this was decided' + (X.path === 'pipeline' ? '' : ' (the existing path; set the pipeline to \u201con\u201d or \u201cshadow\u201d to see every stage)')),
    h('div', { style: 'padding:6px 0' },
      row('Path', (X.path === 'pipeline' ? 'reasoning pipeline' : 'existing path') + ' \u00b7 mode ' + X.mode + (X.provider ? ' \u00b7 provider ' + X.provider + (X.model ? ' ' + X.model : '') : ' \u00b7 no provider')),
      row('Written by', GENERATED[X.generated_by] || X.generated_by),
      row('Model called', X.inference_attempted ? (X.inference_succeeded ? 'yes, and its answer was used' : 'yes, and its answer was not used') : 'no: nothing was sent to a provider'),
      row('Fallback reason', X.failure_kind ? X.failure_kind + (X.failure_kind.startsWith('provider:') ? ' (the provider did not answer)' : X.failure_kind.startsWith('reasoning:') ? ' (the model answered and it was not usable)' : X.failure_kind.startsWith('consent:') ? ' (not allowed to send; nothing was sent)' : '') : null),
      row('Pipeline note', X.pipeline_note),
      shadowed ? h('div', { class: 'small' }, h('b', {}, 'Compared with the existing path: '), shadowed.differences.length ? h('span', { style: 'color:var(--bad,#b00)' }, shadowed.differences.join('; ')) : 'identical') : null,
      X.retrieval_passes ? row('Retrieval passes', X.retrieval_passes + (X.retrieval_passes > 1 ? ' (a second, targeted search was made for a gap)' : '') + ' \u00b7 ' + Object.entries(X.stage_ms || {}).map(([k, v]) => k + ' ' + num(v, 0) + ' ms').join(', ')) : null),
    plan ? h('div', { style: 'border-top:1px solid var(--line);padding-top:6px' }, h('b', { class: 'small' }, 'How the question was read'),
      row('Intent', plan.intent + (plan.needs_structured ? ' (worked out from table rows)' : '') + (plan.multi_source ? ' \u00b7 needs more than one source' : '')),
      row('About', list(plan.entities)), row('Attributes', list(plan.attributes)), row('Scope', list(plan.scope)),
      row('Conditions', (plan.conditions || []).map(c => c.text + (c.qualifier ? ' [' + c.qualifier + ']' : '') + (c.claim_type ? ' (' + c.claim_type + ')' : '')).join('; ') || 'none'),
      row('The answer must establish', list(plan.requirements))) : null,
    ev.length ? h('div', { style: 'border-top:1px solid var(--line);padding-top:6px;margin-top:6px' }, h('b', { class: 'small' }, 'Evidence considered (ids are fixed before any model sees anything)'),
      h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Id', 'Where', 'Judged', 'Why', 'States requirement'].map(t => h('th', {}, t)))),
        h('tbody', {}, ev.map(e => h('tr', {}, h('td', {}, e.id), h('td', { class: 'small' }, clip(e.location || e.source, 60)), h('td', { class: 'small' }, e.status),
          h('td', { class: 'small' }, clip((e.reasons || []).join('; ') || (e.status === 'accepted' ? 'a sentence of it states what was asked' : ''), 90)), h('td', { class: 'small' }, (e.supports || []).map(i => i + 1).join(', ') || ''))))))) : null,
    suff ? h('div', { style: 'border-top:1px solid var(--line);padding-top:6px;margin-top:6px' }, h('b', { class: 'small' }, 'Was the evidence enough?'),
      row('Decision', suff.status + ' \u2192 ' + suff.action), row('Supported', list(suff.supported)), row('Not supported', list(suff.unsupported)), row('Conditions not verified', list(suff.unverified_conditions)),
      row('Conflicts', suff.conflicts ? String(suff.conflicts) : null),
      row('Targeted retrieval', suff.needs_retrieval ? 'wanted: ' + list(suff.gap_queries) : (X.retrieval_passes > 1 ? 'was made' : 'not needed')), row('Notes', (suff.notes || []).join('; '))) : null,
    cv ? h('div', { style: 'border-top:1px solid var(--line);padding-top:6px;margin-top:6px' }, h('b', { class: 'small' }, 'Claim by claim: ' + (cv.passed ? 'every claim held' : cv.removed + ' left out')),
      cv.claims.map((c, i) => h('div', { class: 'small' }, (c.status === 'verified' ? '\u2713 ' : c.status === 'removed' ? '\u2717 ' : '\u00b7 ') + clip(c.text, 120) + ' \u2192 ' + ((c.evidence_ids || []).join(', ') || 'no evidence') + (claims[i] && (claims[i].refs || []).length ? ' (cited as ' + claims[i].refs.map(r => '[' + r + ']').join('') + ')' : '') + (c.issues && c.issues.length ? ' \u2014 ' + c.issues.join(', ') : '')))) : null);
}
function modelBox(R) {
  const M = R.llm || {}, V = R.verification, F = R.fallback;
  return h('div', { class: 'small', style: 'margin-top:8px;border-top:1px solid var(--line);padding-top:8px' },
    h('div', {}, h('b', {}, 'Language model: '), M.used ? 'used (' + (M.capability === 'polish' ? 'reworded the stored answer' : M.capability === 'grounded' ? 'combined the passages' : M.capability) + ', ' + (M.provider || '') + ' ' + (M.model || '') + ')'
      : M.enabled ? 'tried but not used' + (M.failure ? ' (' + M.failure.replace('_', ' ') + ')' : '') : 'not used (local-only answer)'),
    R.assist && R.assist.attempted ? h('div', {}, h('b', {}, 'Extra search: '), R.assist.queries.length ? 'tried “' + R.assist.queries.join('”, “') + '”' + (R.assist.improved ? ' and it found more evidence' : ' and it found nothing more') : 'the model had no useful suggestion') : null,
    V ? h('div', {}, h('b', {}, 'Check of the wording: '), V.passed ? 'passed' : h('span', {}, 'failed',
      h('ul', { class: 'plain' }, (V.issues || []).slice(0, 6).map(i => h('li', {}, i.type.replace('_', ' ') + ': ' + clip(i.detail, 140)))))) : null,
    F ? h('div', { class: 'banner warn', style: 'margin:6px 0' }, F.detail) : null,
    M.usage ? h('div', { class: 'muted' }, 'Usage: ' + M.usage.calls + ' call(s), ' + (M.usage.tokens_in || 0) + ' tokens in, ' + (M.usage.tokens_out || 0) + ' out' + (M.usage.cost != null ? ', cost ' + M.usage.cost : ', cost not configured')) : null,
    (R.unanswered_parts || []).length ? h('div', {}, h('b', {}, 'Not supported by anything available: '), R.unanswered_parts.join('; ')) : null,
    R.clarification && R.clarification.options.length ? h('div', {}, h('b', {}, 'It might mean: '), R.clarification.options.join(' · ')) : null);
}
function diagnosticsBox(R) {
  const D = R.retrieval;
  if (!D) return null;
  const d = D.diagnostics || {};
  return h('details', { style: 'margin-top:8px' }, h('summary', { class: 'small' }, 'Retrieval diagnostics'),
    h('p', { class: 'small muted' }, 'Search: ' + (d.method || '') + (d.alpha != null ? ' (keyword share ' + d.alpha + ')' : '') + ' · ' + num(d.embed_ms, 0) + ' ms to read the question, ' + num(d.search_ms, 0) + ' ms to search ' + (d.entries || '?') + ' entries. ' + D.note),
    (D.quarantined || []).length ? h('div', { class: 'banner warn small' }, 'Set aside because it speaks to an AI, not to a customer: ' + D.quarantined.map(q => q.id + ' (' + q.flags.join(', ') + ')').join('; ')) : null,
    h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Candidate', 'Source', 'Meaning', 'Keywords', 'Combined', 'Found by'].map(t => h('th', {}, t)))),
      h('tbody', {}, D.candidates.map(c => h('tr', {}, h('td', {}, clip(c.question || c.entry_id, 60)), h('td', { class: 'small' }, c.kind === 'passage' ? (c.source || 'document') : 'FAQ'),
        h('td', { class: 'num' }, num(c.similarity_score)), h('td', { class: 'num' }, num(c.keyword_score, 1)), h('td', { class: 'num' }, num(c.combined_score)), h('td', { class: 'small' }, c.found_by === 'assist' ? 'model’s suggestion' : c.retrieval_method || ''))))))); 
}
function fillResult() {
  const box = document.getElementById('resultBox');
  if (!box) return;
  const T = S.test;
  if (!T) { box.replaceChildren(); return; }
  const band = BANDS[T.band], th = T.thresholds, top = T.hits[0], P = S.project;
  const mark = (v, label, below) => h('div', { class: 'mark' + (below ? ' below' : ''), style: 'left:' + Math.min(100, v * 100) + '%' }, h('span', {}, label + ' ' + v.toFixed(2)));
  const hits = T.hits.map((x, i) => h('div', { class: 'cand' },
    h('span', { class: 'rank' }, '#' + (i + 1)),
    h('div', {}, h('b', {}, x.question), x.kind === 'passage' ? h('span', { class: 'pill muted', style: 'margin-left:8px' }, fromLabel(x.source)) : null,
      h('div', { class: 'matched' }, 'matched on: “' + clip(x.matched, 90) + '”' + (x.matched_kind === 'qa' ? ' (' + (x.kind === 'passage' ? 'heading + text' : 'question + answer') + ')' : '') + (x.learned ? ' · a question you approved earlier' : ''))),
    h('div', { class: 'small', style: 'text-align:right' }, num(x.score), bar(x.score, i === 0 ? 'good' : '')),
    h('div', { class: 'ans' }, x.answer ? clip(x.answer, 360) : '(no answer in the FAQ)')));
  const R = T.response, answered = R && ['faq', 'grounded', 'partial'].includes(R.outcome), cls = R && OUTCOMES[R.outcome] ? OUTCOMES[R.outcome][0] : band.cls;
  const scoreSection = h('details', { open: !answered, style: 'margin-top:12px' }, h('summary', { class: 'small' }, 'Match score and thresholds (how the FAQ alone would have answered, used to tune the lines)'),
    h('div', {},
    h('div', { class: 'head' }, h('div', { class: 'grow' }, h('span', { class: 'pill ' + band.cls }, band.label), h('p', { class: 'muted small', style: 'margin:6px 0 0' }, band.sub)),
      h('div', { class: 'small muted' }, num(T.latency_ms, 0) + ' ms')),
    h('div', { class: 'score', 'aria-label': 'Match score ' + num(T.confidence) }, h('div', { class: 'track' }), h('div', { class: 'fill', style: 'width:' + Math.min(100, T.confidence * 100) + '%' }),
      mark(th.floor, 'not in the FAQ below'), th.direct != null ? mark(th.direct, 'answer directly from', true) : null),
    T.band === 'none' ? h('p', { class: 'small muted' }, 'The closest entry scored ' + num(T.confidence) + ', below the line of ' + num(th.floor) + '. Its details are shown below only so you can check.') : null,
    T.band !== 'none' ? h('div', { class: 'answer-box' }, h('div', { class: 'small muted' }, T.band === 'direct' ? 'It would reply:' : 'Best match’s ' + (top.kind === 'passage' ? 'text:' : 'stored answer:')), h('div', {}, top.answer ? clip(top.answer, 600) : '(no answer in the FAQ)'), top.kind === 'passage' ? h('div', { class: 'small muted', style: 'margin-top:6px' }, 'From ' + (isWeb(top.source) ? 'the web page ' : 'the document ') + top.source + '. A passage from a document or web page is context: it is never given as a direct answer.') : null) : null));
  box.replaceChildren(h('section', { class: 'card result ' + cls },
    h('div', { class: 'small muted', style: 'text-align:right' }, num(T.latency_ms, 0) + ' ms'),
    evidenceBox(R),
    scoreSection,
    h('h3', { style: 'margin-top:12px' }, 'Closest entries'),
    hits,
    P.report && P.report.map ? h('details', {}, h('summary', {}, 'Show where your question lands on the map'), h('div', { style: 'max-width:460px' }, entryMap(P.report.map, { highlight: T.hits.map(x => x.id), point: T.point }))) : null,
    feedbackForm(T)));
}
function feedbackForm(T) {
  const none = T.band === 'none', entries = S.project.faq.entry_list;
  const wrap = h('div', { style: 'margin-top:14px;border-top:1px solid var(--line);padding-top:12px' });
  const draw = () => {
    if (S.test !== T) return;
    const kids = [h('h3', {}, none ? 'It said “not in the FAQ”. Is that right?' : 'Is this the right match?'),
      h('div', { class: 'verdict' },
        h('button', { class: 'btn' + (S.pending === 'approve' ? ' good' : ''), type: 'button', onclick: () => { S.pending = 'approve'; draw(); } }, none ? '✓ Right: it is not in the FAQ' : '✓ Right match'),
        h('button', { class: 'btn' + (S.pending === 'reject' ? ' bad' : ''), type: 'button', onclick: () => { S.pending = 'reject'; draw(); } }, none ? '✕ Wrong: the FAQ covers this' : '✕ Wrong match'))];
    if (S.pending) {
      const sel = h('select', { id: 'fixEntry', 'aria-label': 'What it should have matched' },
        h('option', { value: '' }, none ? 'Which entry should it have matched?' : 'What should it have matched? (optional)'),
        none ? null : h('option', { value: 'none' }, 'Nothing: this is not in the FAQ'),
        entries.map(e => h('option', { value: e.id }, (e.kind === 'passage' ? '[document] ' : '') + clip(e.question, 70))));
      kids.push(S.pending === 'reject' ? [h('label', { class: 'field', for: 'fixEntry' }, 'What should it have matched?'), sel] : null,
        h('label', { class: 'field', for: 'comment' }, 'Comment (optional)'),
        h('textarea', { id: 'comment', maxlength: 1000, placeholder: S.pending === 'approve' ? 'Anything worth noting…' : 'What was wrong?' }),
        h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn primary', type: 'button', onclick: () => saveVerdict(T) }, 'Save my judgement')));
    }
    wrap.replaceChildren(...kids.flat(Infinity).filter(Boolean));
  };
  draw();
  return wrap;
}
async function saveVerdict(T) {
  const sel = document.getElementById('fixEntry'), comment = document.getElementById('comment');
  const body = { test_id: T.test_id, verdict: S.pending, comment: comment ? comment.value : '', correct_entry: sel && sel.value ? sel.value : null };
  try {
    const r = await api('/api/projects/' + S.pid + '/feedback', body);
    S.project.feedback = r.feedback;
    S.project.thresholds = r.thresholds;
    S.test = null; S.pending = null;
    document.getElementById('resultBox').replaceChildren(h('div', { class: 'banner good' }, r.learned ? 'Saved, and added to what it searches: the next similar question can match it. Ask another question.' : 'Saved. Ask another question.'));
    fillTally(); fillHistory();
    const q = document.getElementById('q'); if (q) { q.value = ''; q.focus(); }
  } catch (e) { notify(e.message); }
}
function fillTally() {
  const box = document.getElementById('tallyBox');
  if (!box) return;
  const P = S.project, F = P.feedback, t = F.tally, th = P.thresholds;
  const proof = th.direct != null
    ? h('div', { class: 'banner good' }, h('b', {}, 'Proven. '), 'Answers scoring ' + num(th.direct) + ' or more were right at least ' + pct(P.settings.target_precision) + ' of the time (with 90% confidence) across your ' + th.labeled + ' judged tests, so they can be given without an LLM.')
    : h('div', { class: 'banner info' }, h('b', {}, 'Answering without an LLM is not proven yet. '), th.direct_reason ? th.direct_reason.charAt(0).toUpperCase() + th.direct_reason.slice(1) + '.' : 'Judge more tests.', ' Test a broad, realistic mix: easy questions would prove less than they seem to.');
  const n = usableFeedback(F.history);
  box.replaceChildren(card('Your tests', null,
    h('div', { class: 'grid3', style: 'margin:8px 0 14px' }, stat(t.tested, 'questions tested'), stat(t.right == null ? '–' : pct(t.right), 'judged right' + (t.right == null ? '' : ' (' + pct(t.right_low) + ' to ' + pct(t.right_high) + ')'), true), stat(t.approved + ' / ' + t.rejected, 'right / wrong')),
    F.earlier ? h('p', { class: 'muted small' }, F.earlier + ' earlier tests were run on a previous model and are not counted here.') : null,
    t.from_learned ? h('p', { class: 'muted small' }, t.from_learned + (t.from_learned === 1 ? ' test matched' : ' tests matched') + ' a question you had already approved, so ' + (t.from_learned === 1 ? 'it is' : 'they are') + ' not counted as right or wrong: the engine had been told the answer.') : null,
    h('p', { class: 'muted small' }, 'Every question you approve, or correct by naming its entry, is added to what the engine searches straight away, so it develops as you test. Train again to also train the model on them.'),
    proof,
    h('label', { class: 'field', for: 'target' }, 'How sure must it be before answering without an LLM?'),
    (() => {
      const s = h('select', { id: 'target', style: 'max-width:260px', onchange: async e => {
        try { const r = await api('/api/projects/' + S.pid + '/settings', { target_precision: Number(e.target.value) }); S.project.settings.target_precision = Number(e.target.value); S.project.thresholds = r.thresholds; fillTally(); } catch (err) { notify(err.message); }
      } }, [0.9, 0.95, 0.99].map(v => h('option', { value: v }, 'Right ' + pct(v) + ' of the time')));
      s.value = String(P.settings.target_precision);
      return s;
    })(),
    linesControl(th, P.settings.manual || {}),
    h('div', { class: 'row', style: 'margin-top:14px' },
      h('a', { class: 'btn', href: '/api/projects/' + S.pid + '/feedback.csv', download: '' }, 'Download my tests (CSV)'),
      n ? h('button', { class: 'btn', type: 'button', onclick: () => startTrain(true) }, 'Train again using ' + n + ' of my judged questions') : null,
      exportControl()),
    h('p', { class: 'muted small' }, 'The downloaded package keeps improving where you run it: it can be taught new questions while it is live (they are searched at once), and ci/improve.py inside rebuilds it from your files and everything it learned, tests the new version against the live one on your held-out questions, and promotes it only if it is not worse. ci/README.md explains it, and a GitHub Actions workflow is included.')));
}
function linesControl(th, manual) {
  const floor = h('input', { type: 'text', id: 'lineFloor', inputmode: 'decimal', placeholder: 'automatic: ' + num(th.floor), value: manual.floor != null ? String(manual.floor) : '', style: 'max-width:150px' });
  const direct = h('input', { type: 'text', id: 'lineDirect', inputmode: 'decimal', placeholder: 'automatic: ' + (th.direct != null ? num(th.direct) : 'off'), value: manual.direct != null ? String(manual.direct) : '', style: 'max-width:150px' });
  const read = el => (el.value.trim() === '' ? null : Number(el.value));
  const save = async (reset) => {
    const body = reset ? { floor: null, direct: null } : { floor: read(floor), direct: read(direct) };
    if (Object.values(body).some(v => v !== null && !(v >= 0 && v <= 1))) return notify('A line is a number from 0 to 1, or empty for automatic.');
    try { const r = await api('/api/projects/' + S.pid + '/settings', body); S.project.thresholds = r.thresholds; S.project.settings.manual = { floor: body.floor, direct: body.direct }; fillTally(); notify('Saved. New questions use these lines.', 'good'); } catch (e) { notify(e.message); }
  };
  return h('div', { style: 'margin-top:16px' }, h('h3', {}, 'Confidence lines'),
    h('p', { class: 'muted small' }, 'Scores run from 0 to 1. Below the first line it says “not in the FAQ”. From the second line it may answer a FAQ question without an LLM (leave it empty to keep that off until it is proven). Leave a box empty for automatic. Setting a line yourself overrides everything calculated.'),
    h('div', { class: 'row' }, h('label', { class: 'small', for: 'lineFloor' }, 'Not in the FAQ below'), floor, h('label', { class: 'small', for: 'lineDirect' }, 'Answer directly from'), direct,
      h('button', { class: 'btn', type: 'button', onclick: () => save(false) }, 'Apply'), h('button', { class: 'btn link', type: 'button', onclick: () => save(true) }, 'Back to automatic')));
}
function fillHistory() {
  const box = document.getElementById('historyBox');
  if (!box) return;
  const F = S.project.feedback, names = Object.fromEntries(S.project.faq.entry_list.map(e => [e.id, e.question]));
  if (!F.history.length) { box.replaceChildren(); return; }
  box.replaceChildren(card('History', 'Newest first.', h('div', { class: 'scroll' }, h('table', { class: 'hist' },
    h('thead', {}, h('tr', {}, h('th', {}, 'When'), h('th', {}, 'Question'), h('th', {}, 'It said'), h('th', { class: 'num' }, 'Score'), h('th', {}, 'Your verdict'), h('th', {}, 'Comment'))),
    h('tbody', {}, F.history.slice(0, 60).map(x => h('tr', { class: x.gen !== F.gen ? 'old' : '' },
      h('td', { class: 'muted small' }, clock(x.ts)), h('td', {}, x.query),
      h('td', {}, x.band === 'none' ? h('span', { class: 'muted' }, 'not in the FAQ') : clip(names[x.top_entry] || x.top_entry, 50)),
      h('td', { class: 'num' }, num(x.top_score)),
      h('td', {}, x.verdict ? h('span', { class: 'v-' + x.verdict }, x.verdict === 'approve' ? '✓ right' : '✕ wrong') : h('span', { class: 'muted' }, 'not judged'),
        x.correct_entry ? h('div', { class: 'muted small' }, 'should be: ' + (x.correct_entry === 'none' ? 'not in the FAQ' : clip(names[x.correct_entry] || x.correct_entry, 40))) : null),
      h('td', {}, x.comment || '')))))) ));
}

// ---------- go
function render() {
  document.getElementById('app').replaceChildren(topBar(), h('div', { class: 'frame' }, sidebar(), h('main', { id: 'main' }, S.pid && S.project ? projectView() : welcome())));
  if (S.notice) notify(S.notice.msg, S.notice.kind);
}
window.addEventListener('hashchange', route);
route();
