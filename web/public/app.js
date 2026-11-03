// The signed-in application. Everything on the page is built with DOM calls, never from markup strings, so text from a document can not inject anything.
// The browser talks to two places only: Supabase (with the person's own token, so row level security decides what they see) and this site's /api functions.
'use strict';

const PROVIDERS = [
  { key: 'openai', label: 'OpenAI', needsKey: true },
  { key: 'anthropic', label: 'Anthropic (Claude)', needsKey: true },
  { key: 'gemini', label: 'Google Gemini', needsKey: true },
  { key: 'openrouter', label: 'OpenRouter', needsKey: true },
  { key: 'openai_compatible', label: 'OpenAI-compatible server (Ollama, LM Studio, vLLM, a gateway)', needsKey: false, needsAddress: true },
];
const MIME = { csv: 'text/csv', tsv: 'text/tab-separated-values', txt: 'text/plain', md: 'text/markdown', html: 'text/html', htm: 'text/html', json: 'application/json', pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' };
// ---------- a file of secrets is never knowledge, and a .env file is read here, in the browser, only for the one key it was dropped for
const SECRET_FILE = /(^|[\\/])\.env($|\.)|\.env$|\.(pem|key|p12|pfx|keystore|jks)$|(^|[\\/])id_(rsa|dsa|ecdsa|ed25519)|(^|[\\/])(credentials|secrets?)(\.[a-z]+)?$/i;
const ENV_NAMES = { OPENAI_API_KEY: 'openai', ANTHROPIC_API_KEY: 'anthropic', GEMINI_API_KEY: 'gemini', GOOGLE_API_KEY: 'gemini', OPENROUTER_API_KEY: 'openrouter' };
/** The provider keys in the text of a .env file. Only the five names above are read; every other line (database passwords, tokens of other services...) is skipped without being looked at again, and
 * nothing here stores, logs or sends anything: the caller decides what to do with the one key it needs. @param {string} text */
function parseEnvText(text) {
  const byProvider = new Map();
  let skipped = 0;
  if (typeof text !== 'string' || text.length > 65536) return { found: [], skipped: 0, tooBig: typeof text === 'string' };
  for (const raw of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    const provider = m && ENV_NAMES[m[1]];
    if (!provider) { skipped += 1; continue; }
    let v = m[2].trim();
    if ((v.startsWith('"') && v.endsWith('"') && v.length > 1) || (v.startsWith("'") && v.endsWith("'") && v.length > 1)) v = v.slice(1, -1); else v = v.replace(/\s+#.*$/, '');
    if (v.length >= 8 && v.length <= 400 && !/\s/.test(v)) byProvider.set(provider, v);
  }
  return { found: [...byProvider].map(([provider, key]) => ({ provider, key })), skipped, tooBig: false };
}
let ENV_HELD = {};  // the keys of a dropped file, held only until the person picks one (or leaves), never rendered, never stored
const mask = (k) => '••••' + k.slice(-4);

const OUTCOMES = {
  faq: ['good', 'Answered from a stored FAQ'], grounded: ['info', 'Answered from your documents'], partial: ['warn', 'Answered in part'], insufficient: ['warn', 'Not answered'],
  conflict: ['bad', 'Sources disagree'], clarification: ['info', 'Needs a clarification'],
};

const h = (tag, attrs, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style' && el.style) el.style.cssText = v;  // through the style API: the page's CSP forbids inline style ATTRIBUTES, and this is not one
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};
const clip = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s || '');
const num = (x, d = 2) => (x == null ? 'n/a' : Number(x).toFixed(d));

const S = { sb: null, session: null, businesses: [], current: null, tab: 'knowledge', llm: null, consent: null, disclosure: {}, ack: {}, ai: null, usage: null, history: null, team: null, invite: null, cstatus: null, chello: null, pairMsg: null, pull: null, local: null, sources: [], result: null, busy: false, msg: null, form: { provider: '', model: '', base_url: '', key: '', models: [], caps: null, choice: null, msg: null, mode: null, platform: null, fallback: null } };

function say(text, kind = 'info') { S.msg = { text, kind }; render(); try { const el = document.querySelector('.banner[role=status]'); if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch { /* no DOM */ } }
function fail(e) { say(e && e.message ? e.message : 'Something went wrong.', 'bad'); }

async function api(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json', authorization: 'Bearer ' + S.session.access_token }, body: JSON.stringify(body) });
  let data = {};
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw new Error(data.error || 'Something went wrong (' + res.status + ').');
  return data;
}

async function boot() {
  const root = document.getElementById('root');
  let cfg;
  try {
    const res = await fetch('/api/config');
    cfg = await res.json();
    if (!res.ok) throw new Error(cfg.error);
  } catch (e) { root.replaceChildren(h('div', { class: 'banner warn' }, e.message || 'This deployment is not configured yet.')); return; }
  S.sb = window.supabase.createClient(cfg.supabaseUrl, cfg.anonKey);
  S.sb.auth.onAuthStateChange((_event, session) => { const was = S.session && S.session.user && S.session.user.id; S.session = session; if ((session && session.user && session.user.id) !== was) load(); });
  S.session = (await S.sb.auth.getSession()).data.session;
  await load();
}

async function load() {
  if (S.session) {
    const { data, error } = await S.sb.from('business_members').select('role, businesses(id, name, slug, answer_mode, onboarding_completed_at)').order('created_at');
    if (error) return fail(error);
    S.businesses = (data || []).filter((m) => m.businesses).map((m) => ({ ...m.businesses, role: m.role }));
    let saved = null;
    try { saved = localStorage.getItem('business'); } catch { /* storage may be blocked */ }
    S.current = S.businesses.find((b) => b.id === saved) || S.businesses[0] || null;
    if (S.current) await loadBusiness();
  } else { S.businesses = []; S.current = null; }
  render();
  if (S.session && /^#invite=/.test(location.hash)) acceptInvitation();
}

async function loadBusiness() {
  const id = S.current.id;
  const admin = ['owner', 'admin'].includes(S.current.role);
  const [src, llm, consent, ai, usage, members, invitations] = await Promise.all([
    S.sb.from('knowledge_sources').select('id, name, source_type, status, parse_status, error, version, entry_count, chunk_count, updated_at, valid_from, valid_until, supersedes').eq('business_id', id).order('created_at', { ascending: false }),
    api('/api/llm/get', { business_id: id }).catch(() => null),
    api('/api/ai/consent/get', { business_id: id }).catch(() => null),
    api('/api/ai/settings/get', { business_id: id }).catch(() => null),
    admin ? api('/api/usage/get', { business_id: id }).catch(() => null) : null,
    S.sb.from('business_members').select('user_id, role, created_at, profiles(display_name)').eq('business_id', id).order('created_at'),
    admin ? S.sb.from('business_invitations').select('id, email, role, created_at, expires_at, accepted_at, revoked_at').eq('business_id', id).order('created_at', { ascending: false }).limit(50) : null,
  ]);
  S.sources = src.data || [];
  S.llm = llm;
  S.consent = consent;
  S.ai = ai;
  S.usage = usage;
  S.team = { members: (members && members.data) || [], invitations: (invitations && invitations.data) || [] };
  S.history = null;
  S.invite = null;
  S.disclosure = {};
  S.ack = {};
  S.result = null;
  Object.assign(S.form, { provider: llm?.provider || '', model: llm?.model || '', base_url: llm?.base_url || '', key: '', models: [], caps: null, choice: null, msg: null, mode: null, platform: null, fallback: null });
  S.chello = null; S.cstatus = null; S.pairMsg = null; S.pull = null; S._autoPairTried = false; S.train = null; if (typeof clearTimeout === 'function') clearTimeout(S._trainPoll);
}

// ---------- screens
function render() {
  const who = document.getElementById('who');
  const root = document.getElementById('root');
  who.replaceChildren(...(S.session ? [h('span', { class: 'small muted' }, S.session.user.email), h('button', { class: 'btn', type: 'button', onclick: async () => { await S.sb.auth.signOut(); } }, 'Sign out')] : []));
  const kids = [];
  if (S.msg) kids.push(h('div', { class: 'banner ' + S.msg.kind, role: 'status' }, S.msg.text));
  if (!S.session) kids.push(authPanel());
  else if (!S.current) kids.push(createBusinessPanel());
  else kids.push(businessView());
  // what a person has typed survives a redraw (an error banner, a status refresh): fields are matched by their id or placeholder, and only an untouched (empty) field is filled back in
  const typed = new Map();
  for (const el of root.querySelectorAll('input:not([type=password]):not([type=file]):not([type=checkbox]), textarea')) if (el.value && (el.id || el.placeholder)) typed.set(el.id || el.placeholder, el.value);
  const hadFocus = document.activeElement && root.contains(document.activeElement) ? (document.activeElement.id || document.activeElement.placeholder) : null;
  root.replaceChildren(...kids);
  for (const el of root.querySelectorAll('input:not([type=password]):not([type=file]):not([type=checkbox]), textarea')) {
    const k = el.id || el.placeholder;
    if (k && typed.has(k) && !el.value) el.value = typed.get(k);
    if (k && k === hadFocus && typeof el.focus === 'function') el.focus();
  }
}

function authPanel() {
  const signup = new URLSearchParams(location.search).get('signup') === '1';
  const email = h('input', { type: 'email', id: 'email', autocomplete: 'email', required: true });
  const password = h('input', { type: 'password', id: 'password', autocomplete: signup ? 'new-password' : 'current-password' });
  const go = (fn) => async (e) => { e.preventDefault(); try { const { error } = await fn(); if (error) throw error; } catch (err) { fail(err); } };
  return h('section', { class: 'panel', style: 'max-width:460px;margin:40px auto' },
    h('h2', {}, signup ? 'Create your account' : 'Sign in'),
    h('p', { class: 'muted' }, 'Each business you create is private to the people you invite to it.'),
    h('form', {},
      h('label', { class: 'field', for: 'email' }, 'Email'), email,
      h('label', { class: 'field', for: 'password' }, 'Password'), password,
      h('div', { class: 'row' },
        h('button', { class: 'btn primary', type: 'submit', onclick: go(() => (signup ? S.sb.auth.signUp({ email: email.value, password: password.value }).then((r) => { if (!r.error && !r.data.session) say('Check your email to confirm the account, then sign in.', 'good'); return r; }) : S.sb.auth.signInWithPassword({ email: email.value, password: password.value }))) }, signup ? 'Create account' : 'Sign in'),
        h('button', { class: 'btn', type: 'button', onclick: go(() => S.sb.auth.signInWithOtp({ email: email.value }).then((r) => { if (!r.error) say('A sign-in link is on its way to your email.', 'good'); return r; })) }, 'Email me a link'),
        h('a', { class: 'btn link', href: signup ? '/app' : '/app?signup=1' }, signup ? 'I have an account' : 'Create an account'))));
}

function createBusinessPanel() {
  const name = h('input', { type: 'text', id: 'bname', maxlength: 120, placeholder: 'Acme Bikes', required: true });
  return h('section', { class: 'panel' }, h('h2', {}, 'Create your first business'), h('p', { class: 'muted' }, 'A business holds its own knowledge, conversations, settings and team.'),
    h('form', { onsubmit: async (e) => {
      e.preventDefault();
      const slug = name.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) + '-' + crypto.randomUUID().slice(0, 6);
      const { error } = await S.sb.rpc('create_business', { p_name: name.value.trim(), p_slug: slug });
      if (error) return fail(error);
      await load();
    } }, h('label', { class: 'field', for: 'bname' }, 'Business name'), name, h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'submit' }, 'Create business'))));
}

// The journey of a business, one step at a time: add knowledge, train, test. The way forward and back is always a button on the page, never the top bar.
const JOURNEY = [['knowledge', 'Add knowledge'], ['training', 'Train'], ['playground', 'Test it']];
function go(tab) {
  if (JOURNEY.some(([k]) => k === S.tab)) S.from = S.tab;
  S.tab = tab; S.msg = null; render();
  try { window.scrollTo(0, 0); } catch { /* no window */ }
}
function journeyStepper() {
  const at = JOURNEY.findIndex(([k]) => k === S.tab);
  return h('ol', { class: 'stepper journey', 'aria-label': 'Your progress' }, JOURNEY.map(([k, t], i) => h('li', { class: i < at ? 'done' : i === at ? 'now' : '', 'aria-current': i === at ? 'step' : null }, h('i', {}, i < at ? '✓' : String(i + 1)), t)));
}
async function startTraining() {
  try { S.train = await api('/api/train/start', { business_id: S.current.id }); S.trainErr = null; loadTrain(); } catch (e) { fail(e); }
}
function journeyFooter(canWrite) {
  if (!JOURNEY.some(([k]) => k === S.tab)) return h('div', { class: 'footnav' }, h('button', { class: 'btn', type: 'button', onclick: () => go(S.from || 'knowledge') }, '← Back'));
  const ready = (S.sources || []).filter((x) => x.status === 'ready').length, T = S.train || {};
  const running = ['training', 'queued'].includes(T.state), trained = T.state === 'trained' && !!T.report;
  const back = (to, label) => h('button', { class: 'btn', type: 'button', onclick: () => go(to) }, '← ' + label);
  const skip = h('button', { class: 'btn link', type: 'button', onclick: () => go('playground') }, 'Skip training and test');
  if (S.tab === 'knowledge') return h('div', { class: 'footnav' }, h('p', { class: 'small muted' }, ready ? ready + (ready === 1 ? ' source is' : ' sources are') + ' ready. Next, teach MiniLM your wording and measure it.' : 'Add at least one file above to continue.'),
    h('div', { class: 'row' }, ready ? skip : null, h('button', { class: 'btn primary big', type: 'button', disabled: !ready || !canWrite, onclick: () => { go('training'); startTraining(); } }, 'Train on my knowledge →')));
  if (S.tab === 'training') return h('div', { class: 'footnav' }, back('knowledge', 'Back to knowledge'),
    h('div', { class: 'row' }, trained ? null : running ? null : skip, h('button', { class: 'btn primary big', type: 'button', disabled: !trained, onclick: () => go('playground') }, running ? 'Training…' : trained ? 'Test your answers →' : 'Train first to continue')));
  return h('div', { class: 'footnav' }, back('training', 'Back to training'), h('button', { class: 'btn link', type: 'button', onclick: () => go('knowledge') }, 'Add more knowledge'));
}

function businessView() {
  const b = S.current;
  const canWrite = ['owner', 'admin', 'editor'].includes(b.role), canAdmin = ['owner', 'admin'].includes(b.role);
  const wizard = canAdmin && (!b.onboarding_completed_at || S.reonboard);
  return h('div', {},
    h('div', { class: 'row', style: 'justify-content:space-between' },
      h('div', {}, h('h1', { style: 'font-size:1.8rem;margin:0' }, b.name), h('span', { class: 'pill muted' }, b.role), canAdmin && !wizard && b.onboarding_completed_at ? h('button', { class: 'btn link', type: 'button', onclick: () => { S.reonboard = true; S.form.choice = null; render(); } }, 'Change AI setup') : null, wizard ? null : h('button', { class: 'btn link', type: 'button', onclick: () => go('history') }, 'History'), wizard ? null : h('button', { class: 'btn link', type: 'button', onclick: () => go('settings') }, 'Settings')),
      S.businesses.length > 1 ? h('select', { 'aria-label': 'Business', style: 'max-width:240px', onchange: async (e) => { S.current = S.businesses.find((x) => x.id === e.target.value); try { localStorage.setItem('business', S.current.id); } catch { /* ignore */ } await loadBusiness(); render(); } },
        S.businesses.map((x) => h('option', { value: x.id, selected: x.id === b.id }, x.name))) : null),
    wizard ? onboardingPanel() : null,
    wizard ? null : JOURNEY.some(([k]) => k === S.tab) ? journeyStepper() : h('div', { class: 'row backbar' }, h('button', { class: 'btn back', type: 'button', onclick: () => go(S.from || 'knowledge') }, '← Back'), h('h2', { class: 'pagetitle' }, S.tab === 'history' ? 'History' : 'Settings')),
    wizard ? null : S.tab === 'knowledge' ? knowledgeTab(canWrite) : S.tab === 'training' ? trainingTab(canWrite) : S.tab === 'playground' ? playgroundTab() : S.tab === 'history' ? historyTab() : settingsTab(canAdmin),
    wizard ? null : journeyFooter(canWrite));
}

// ---------- the optional language model: onboarding and settings
function llmForm(onDone) {
  const F = S.form, L = S.llm || {};
  const spec = PROVIDERS.find((p) => p.key === F.provider) || {};
  const caps = F.caps || { polish: true, grounded: true, assist: true, ...(L.capabilities || {}) };
  const redraw = () => render();
  const body = () => ({ business_id: S.current.id, provider: F.provider, model: F.model, base_url: spec.needsAddress ? F.base_url : undefined, api_key: F.key || undefined });
  const run = (fn) => async () => { S.busy = true; F.msg = null; redraw(); try { await fn(); } catch (e) { F.msg = { kind: 'bad', text: e.message }; } S.busy = false; redraw(); };
  const validate = run(async () => {
    const r = await api('/api/llm/validate', body());
    F.models = r.models || [];
    F.msg = { kind: r.ok ? 'good' : 'warn', text: r.message + (r.ok ? '' : ' You can try again, or continue in local-only mode.') };
    if (r.ok && !F.model && F.models.length) F.model = F.models[0];
  });
  const save = run(async () => {
    S.llm = await api('/api/llm/save', { ...body(), enabled: true, capabilities: caps });
    F.key = ''; F.choice = null;
    await finish('llm');
    let note = '';
    try { S.ai = await api('/api/ai/settings/save', { business_id: S.current.id, policy: 'byok_preferred' }); } catch (e) { note = ' To use it, ' + String(e.message).replace(/\.$/, '') + '; then choose “Your own provider account first” under How this business answers.'; }
    say((S.llm && S.llm.in_use && S.llm.in_use.used ? 'Saved. Questions can now use this model.' : 'Saved. ' + ((S.llm && S.llm.in_use && S.llm.in_use.message) || 'It is not in use yet.') + ' See “AI providers and your data” in Settings.') + note, S.llm && S.llm.in_use && S.llm.in_use.used && !note ? 'good' : 'warn');
  });
  const finish = async (mode) => { const { error } = await S.sb.from('businesses').update({ answer_mode: mode, onboarding_completed_at: new Date().toISOString() }).eq('id', S.current.id); if (error) throw error; S.current.onboarding_completed_at = new Date().toISOString(); S.current.answer_mode = mode; S.reonboard = false; };
  const local = run(async () => { await finish('local_only'); F.choice = null; say('Local-only mode. You can add a language model later in Settings.', 'good'); });
  const useEnvKey = (provider) => {
    const key = ENV_HELD[provider];
    F.provider = provider; F.key = key; F.model = ''; F.models = []; F.envFound = null;
    F.msg = { kind: 'good', text: 'Using the ' + (PROVIDERS.find((p) => p.key === provider) || {}).label + ' key from the file (' + mask(key) + '). Nothing else in the file was kept. Check the key, choose a model, then save.' };
    ENV_HELD = {};
    redraw();
  };
  const readEnv = async (file) => {
    ENV_HELD = {};
    if (!file) return;
    if (file.size > 65536) { F.msg = { kind: 'warn', text: 'That file is too large to be a .env file.' }; return redraw(); }
    let parsed;
    try { parsed = parseEnvText(await file.text()); } catch (e) { F.msg = { kind: 'warn', text: 'That file could not be read.' }; return redraw(); }
    if (!parsed.found.length) { F.envFound = null; F.msg = { kind: 'warn', text: 'No key for OpenAI, Anthropic, Google Gemini or OpenRouter (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY) was found in that file. Nothing from it was kept.' }; return redraw(); }
    for (const f of parsed.found) ENV_HELD[f.provider] = f.key;
    if (parsed.found.length === 1) return useEnvKey(parsed.found[0].provider);
    F.envFound = parsed.found.map((f) => ({ provider: f.provider, mask: mask(f.key) }));
    F.msg = null;
    redraw();
  };
  const zone = h('div', { class: 'dropzone', role: 'group', 'aria-label': 'Drop a .env file', ondragover: (e) => { e.preventDefault(); }, ondrop: (e) => { e.preventDefault(); readEnv(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]); } },
    h('div', {}, h('b', {}, 'Have a .env file with your key? Drop it here.'), h('span', { class: 'small muted' }, ' It is read in this browser only for the one provider key you choose. The file is not uploaded, and nothing else in it is kept.')),
    h('input', { type: 'file', accept: '.env,text/plain', 'aria-label': 'Choose a .env file', onchange: (e) => { readEnv(e.target.files && e.target.files[0]); e.target.value = ''; } }),
    F.envFound ? h('div', { class: 'row' }, h('span', { class: 'small' }, 'The file holds more than one provider’s key. Use:'), F.envFound.map((f) => h('button', { class: 'btn', type: 'button', onclick: () => useEnvKey(f.provider) }, (PROVIDERS.find((p) => p.key === f.provider) || {}).label + ' (' + f.mask + ')'))) : null);
  return h('div', {}, zone,
    h('label', { class: 'field' }, 'Provider', h('select', { disabled: S.busy, onchange: (e) => { F.provider = e.target.value; F.model = ''; F.models = []; F.msg = null; redraw(); } },
      h('option', { value: '' }, 'Choose a provider…'), PROVIDERS.map((p) => h('option', { value: p.key, selected: p.key === F.provider }, p.label)))),
    spec.needsAddress ? h('label', { class: 'field' }, 'Server address (https)', h('input', { type: 'url', value: F.base_url, placeholder: 'https://llm.example.com/v1', oninput: (e) => { F.base_url = e.target.value; } })) : null,
    F.provider ? h('label', { class: 'field' }, spec.needsKey === false ? 'API key (optional for your own server)' : 'API key',
      h('input', { type: 'password', autocomplete: 'new-password', spellcheck: 'false', value: F.key, placeholder: L.has_key && L.provider === F.provider ? 'A key is saved. Type a new one to replace it.' : 'Paste the key', oninput: (e) => { F.key = e.target.value; } })) : null,
    F.provider ? h('label', { class: 'field' }, 'Model', h('input', { type: 'text', list: 'models', autocomplete: 'off', value: F.model, placeholder: F.models.length ? 'Choose a model' : 'Check the key to list models, or type one', oninput: (e) => { F.model = e.target.value; } }), h('datalist', { id: 'models' }, F.models.map((m) => h('option', { value: m })))) : null,
    F.provider ? h('div', { class: 'small', style: 'margin:10px 0' }, h('b', {}, 'What the model may do'), [['polish', 'Reword a stored FAQ answer that already answers the question'], ['grounded', 'Combine passages that already support the answer'], ['assist', 'Suggest other searches when the first finds too little']]
      .map(([k, t]) => h('label', { style: 'display:flex;gap:8px;align-items:center;margin-top:4px' }, h('input', { type: 'checkbox', checked: caps[k], onchange: (e) => { F.caps = { ...caps, [k]: e.target.checked }; } }), t))) : null,
    F.provider ? h('p', { class: 'banner info small' }, 'If the provider is in use, customer questions, and the FAQ answer or document passages found for them, are sent to it to be worded or searched. Nothing else is sent. A provider is used only after your business has agreed to that in Settings (“AI providers and your data”) and only if this service has verified the provider’s data-handling terms. Your key is encrypted on the server and never shown again. Every sentence the model writes is checked against your documents before it is used.') : null,
    F.msg ? h('div', { class: 'banner ' + F.msg.kind, role: 'status' }, F.msg.text) : null,
    h('div', { class: 'row' },
      F.provider ? h('button', { class: 'btn', type: 'button', disabled: S.busy, onclick: validate }, S.busy ? 'Working…' : 'Check key and model') : null,
      F.provider ? h('button', { class: 'btn primary', type: 'button', disabled: S.busy, onclick: save }, 'Save and turn on') : null,
      h('button', { class: 'btn link', type: 'button', disabled: S.busy, onclick: local }, 'Continue in local-only mode')));
}

function onboardingPanel() {
  const F = S.form;
  const complete = async (mode) => { const { error } = await S.sb.from('businesses').update({ answer_mode: mode, onboarding_completed_at: new Date().toISOString() }).eq('id', S.current.id); if (error) throw error; S.current.onboarding_completed_at = new Date().toISOString(); S.current.answer_mode = mode; };
  const none = async () => { try { await complete('local_only'); say('No AI for now: answers come from the deterministic engine, and nothing is sent anywhere. You can turn AI on later in Settings.', 'good'); } catch (e) { fail(e); } };
  const usesMyComputer = async () => {
    try { S.ai = await api('/api/ai/settings/save', { business_id: S.current.id, policy: 'local_only' }); await complete('local_only'); say('AI will run on your own computer. Nothing is sent to any other company. Outside fallbacks can be added later in Settings, and are off until you turn them on.', 'good'); } catch (e) { fail(e); }
  };
  const mine = ((S.ai && S.ai.companions) || []).filter((c) => !c.revoked);
  const ready = mine.length > 0 && mine.every((c) => c.fit !== 'not_advised');
  const pick = (k, title, text) => h('button', { class: 'choice' + (F.choice === k ? ' on' : ''), type: 'button', onclick: () => { F.choice = k; render(); } }, h('b', {}, title), h('span', {}, text));
  const back = () => { F.choice = null; F.provisionOk = false; render(); };
  const skip = h('button', { class: 'btn link', type: 'button', onclick: () => { S.reonboard = false; render(); } }, 'Cancel');
  const stepper = h('ol', { class: 'stepper', 'aria-label': 'Setup progress' },
    h('li', { class: F.choice ? 'done' : 'now' }, h('i', {}, F.choice ? '✓' : '1'), 'Choose how AI answers'),
    h('li', { class: F.choice ? 'now' : '' }, h('i', {}, '2'), F.choice === 'local' ? 'Set up your computer' : F.choice === 'llm' ? 'Connect your provider' : F.choice === 'none' ? 'Confirm' : 'Set up'),
    h('li', {}, h('i', {}, '3'), 'Ask your first question'));
  const backBar = h('div', { class: 'row backbar' }, h('button', { class: 'btn back', type: 'button', onclick: back }, '← Back'));
  const head = h('div', {}, stepper,
    F.choice ? null : h('h2', {}, 'How should AI answer for this business?'),
    F.choice ? null : h('p', { class: 'muted' }, 'Either way the knowledge engine finds and checks the evidence first, and every sentence a model writes is checked against your documents. You can change this later.'));
  if (!F.choice) return h('section', { class: 'panel wizard', id: 'setup' }, head,
    h('div', { class: 'choices' },
      pick('local', 'On my computer (recommended)', 'We look for a model on this computer and download a small, fast one if none is found. Your questions and documents stay here; no account or key is needed.'),
      pick('llm', 'Through an API service', 'Use your own account with OpenAI, Anthropic, Google, OpenRouter or your own server: drop your .env file or paste the key. Data goes to that provider only after you agree.'),
      pick('none', 'No AI for now', 'The deterministic engine answers from your knowledge by its own rules. Nothing is sent anywhere.')),
    S.current.onboarding_completed_at ? h('div', { class: 'row' }, skip) : null);
  if (F.choice === 'none') return h('section', { class: 'panel wizard', id: 'setup' }, head, backBar,
    h('h2', {}, 'Continue without AI'), h('p', { class: 'muted' }, 'Answers come from your knowledge by the engine’s own rules, with the evidence shown. Nothing is sent anywhere, and you can turn AI on later from “Change AI setup”.'),
    h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', onclick: none }, 'Continue without AI')));
  if (F.choice === 'local') return h('section', { class: 'panel wizard', id: 'setup' }, head, backBar,
    h('h2', {}, 'Run AI on your own computer'),
    h('ol', { class: 'setup-steps' },
      h('li', {}, h('b', {}, '1'), h('span', {}, 'The page looks for the companion on this computer and checks which models fit your hardware.')),
      h('li', {}, h('b', {}, '2'), h('span', {}, 'Pair it with the code it shows. If a model is already installed it is selected for you.')),
      h('li', {}, h('b', {}, '3'), h('span', {}, 'If none is found, one click downloads the recommended small, fast model.'))),
    localPanel(true),
    h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', disabled: !ready, onclick: usesMyComputer }, 'Use my computer for AI'), ready ? null : h('span', { class: 'small muted' }, 'Available once a model is set up on this computer.')),
    h('p', { class: 'small muted' }, 'Local AI keeps inference on your machine. Outside fallbacks (free or paid hosted AI, your own provider account) are off unless you turn them on and agree to each provider.'));
  return h('section', { class: 'panel wizard', id: 'setup' }, head, backBar, llmForm());
}

// ---------- training: MiniLM on the business's own files, with the measured results
async function loadTrain() {
  try { S.train = await api('/api/train/status', { business_id: S.current.id }); S.trainErr = null; } catch (e) { S.trainErr = e.message; }
  if (S.train && !['training', 'queued'].includes(S.train.state) && S.msg && /^Training started/.test(S.msg.text)) S.msg = null;
  if (typeof clearTimeout === 'function') clearTimeout(S._trainPoll);
  if (S.train && typeof setTimeout === 'function' && ['training', 'queued'].includes(S.train.state) && S.tab === 'training') S._trainPoll = setTimeout(loadTrain, 2500);
  render();
}
const PCT = (x) => (typeof x === 'number' ? (100 * x).toFixed(1) + '%' : '–');
function trainingTab(canWrite) {
  if (S.train === null && !S.trainErr) { S.train = undefined; loadTrain(); }
  const T = S.train || { state: 'none' }, R = T.report, P = T.progress;
  const start = async () => { await startTraining(); if (!S.trainErr) say('Training started. This takes a minute or two on a laptop; you can wait here.', 'good'); };
  const running = ['training', 'queued'].includes(T.state);
  const bar = (v, of) => h('div', { class: 'bar', role: 'progressbar' }, h('div', { style: 'width:' + (of ? Math.min(100, Math.round(100 * v / of)) : 6) + '%' }));
  const sys = R && R.systems ? [['bm25', 'Keyword search'], ['base', 'MiniLM, as downloaded'], ['tuned', 'MiniLM, trained on your files']].filter(([k]) => R.systems[k]) : [];
  const raw = (P && P.curve && P.curve.length ? P.curve : (R && R.curve) || []);
  const pts = (Array.isArray(raw) ? raw : []).map((q) => ({ loss: q.loss != null ? q.loss : q.train_loss != null ? q.train_loss : q.value }));
  return h('div', {},
    h('section', { class: 'panel' }, h('h2', {}, 'Train MiniLM on your knowledge'),
      h('p', { class: 'muted' }, 'MiniLM is the small model that turns your questions and your files into numbers so the right passage can be found. Training teaches it your own words. It runs on this computer; nothing is uploaded.'),
      S.trainErr ? h('div', { class: 'banner warn', role: 'status' }, S.trainErr) : null,
      T.state === 'failed' ? h('div', { class: 'banner bad', role: 'status' }, T.error || 'Training failed.') : null,
      T.state === 'trained' && R ? h('div', { class: 'banner good', role: 'status' }, 'Training finished. The results are below; the next step is to test your answers.') : null,
      h('div', { class: 'row' }, canWrite ? h('button', { class: 'btn primary', type: 'button', disabled: running, onclick: start }, running ? 'Training…' : R ? 'Train again' : 'Train on my knowledge') : h('span', { class: 'small muted' }, 'Only editors can start training.'),
        T.state === 'none' ? h('span', { class: 'small muted' }, 'Add your files in Knowledge first, then train.') : null),
      running ? h('div', {}, h('p', { class: 'small' }, h('b', {}, 'Stage: '), T.stage || 'starting', P && P.of ? '  ·  step ' + P.step + ' of ' + P.of : '', P && P.loss != null ? '  ·  loss ' + Number(P.loss).toFixed(3) : '', P && P.eta_s ? '  ·  about ' + Math.max(1, Math.round(P.eta_s / 60)) + ' min left' : '', P && P.device ? '  ·  ' + P.device : ''), bar(P ? P.step : 0, P ? P.of : 0)) : null),
    R && !sys.length ? h('section', { class: 'panel' }, h('h2', {}, 'Nothing to measure yet'),
      h('p', { class: 'muted' }, 'Your documents are searched with MiniLM as downloaded, which works without training. Training and its measurements need question and answer pairs: a FAQ file (CSV, Excel or JSON), or a document written as “Q: question” followed by its answer.'),
      Array.isArray(R.verdict) && R.verdict.length ? h('ul', { class: 'small how' }, R.verdict.map((v) => h('li', {}, typeof v === 'string' ? v : (v.text || v.message || '')))) : null) : null,
    R && sys.length ? h('section', { class: 'panel' }, h('h2', {}, 'How the search performs on checks made from your own files'),
      h('p', { class: 'small muted' }, R.entries + ' entries, ' + R.views + ' searchable phrasings. These checks are generated from your FAQ (held-back phrasings, typos, answer words, off-topic questions): sanity checks on this knowledge, not a promise of accuracy on real customers.'),
      h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Search', 'Right answer first', 'In the top 3', 'Ranking quality (MRR)', 'nDCG'].map((t) => h('th', {}, t)))),
        h('tbody', {}, sys.map(([k, label]) => h('tr', {}, h('td', {}, label), h('td', {}, PCT(R.systems[k]['hit@1'])), h('td', {}, PCT(R.systems[k]['hit@3'])), h('td', {}, PCT(R.systems[k]['mrr@10'])), h('td', {}, PCT(R.systems[k]['ndcg@10']))))))),
      R.training ? h('div', { class: 'small' }, h('b', {}, 'Training run: '), (R.training.steps || 0) + ' steps, ' + (R.training.epochs || 0) + ' epochs, ' + (R.training.train_seconds != null ? Math.round(R.training.train_seconds) + ' s' : '') + ' on ' + (R.training.device || 'this computer') + (R.training.train_loss_last != null ? ', final loss ' + Number(R.training.train_loss_last).toFixed(3) : '')) : null,
      pts.length ? h('div', { class: 'losscurve', 'aria-label': 'Training loss, falling over the run' }, pts.slice(0, 80).map((q) => h('i', { style: 'height:' + Math.max(4, Math.min(100, Math.round(100 * (q.loss || 0) / Math.max(0.0001, ...pts.map((z) => z.loss || 0))))) + '%' }))) : null,
      Array.isArray(R.verdict) && R.verdict.length ? h('ul', { class: 'small how' }, R.verdict.map((v) => h('li', {}, typeof v === 'string' ? v : (v.text || v.message || JSON.stringify(v))))) : null,
      h('p', { class: 'small muted' }, R.model ? 'Model: ' + R.model.base + ', ' + R.model.params_millions + ' million parameters, ' + R.model.dimension + ' numbers per phrase. ' : '', 'Answers in “Test it” currently search with the model as downloaded; the trained model is measured here first.')) : null);
}

// How answers are worded: the engine's own checked wording (fast, exact), or the local model wording the checked answer as a reply (slower, friendlier, verified against the evidence)
function styleCard(canAdmin) {
  const A = S.ai || {};
  const local = A.settings && A.settings.mode === 'local' && A.route && A.route.usable;
  const cur = (A.settings && A.settings.answer_style_set && A.settings.answer_style) || (local ? 'chat' : 'plain');
  const set = async (v) => { try { S.ai = await api('/api/ai/settings/save', { business_id: S.current.id, policy: A.policy && A.policy !== 'custom' ? A.policy : 'local_only', answer_style: v }); say(v === 'chat' ? 'Answers will now be worded by your local model, checked against your evidence.' : 'Answers will be given in the engine’s own checked wording.', 'good'); } catch (e) { fail(e); } };
  const pick = (v, title, text) => h('button', { class: 'choice' + (cur === v ? ' on' : ''), type: 'button', disabled: !canAdmin || !local, 'aria-pressed': String(cur === v), onclick: () => set(v) }, h('b', {}, title), h('span', {}, text));
  return h('section', { class: 'panel' }, h('h2', {}, 'How answers are worded'),
    h('p', { class: 'muted' }, 'Either way, the engine finds and checks the evidence first. This only decides who writes the sentence you read.'),
    h('div', { class: 'choices' },
      pick('plain', 'Exact and fast', 'Answers come straight from your evidence in milliseconds. An exact FAQ match never waits for a model.'),
      pick('chat', 'Chat style (default with a local model)', 'Your local model words each checked answer as a friendly reply. It takes seconds, sometimes longer, and any reply that adds a fact is thrown away.')),
    local ? null : h('p', { class: 'small muted' }, 'Chat style needs a local model: choose “On my computer” under “Change AI setup” first.'));
}

function settingsTab(canAdmin) {
  const F = S.form, L = S.llm || {};
  const status = L.enabled ? h('span', { class: 'pill good' }, 'On: ' + (L.label || L.provider) + ' · ' + L.model) : h('span', { class: 'pill muted' }, L.provider ? 'Off' + (L.has_key ? ' (key saved)' : '') : 'Local-only mode');
  const act = (fn) => async () => { try { S.llm = await fn(); render(); } catch (e) { fail(e); } };
  return h('div', {},
    answerModePanel(canAdmin),
    styleCard(canAdmin),
    h('section', { class: 'panel' }, h('h2', {}, 'Your own AI provider'), h('p', { class: 'muted' }, 'Connect your own account with OpenAI, Anthropic, Google, OpenRouter or your own server. It is used only when “Your own AI provider” is chosen under How this business answers, and only after you agree to send data to it below. Your key is encrypted on the server and never shown again.'),
      h('div', { class: 'row' }, status, L.storage ? h('span', { class: 'small muted' }, 'Key: ' + L.storage) : null,
        canAdmin ? h('button', { class: 'btn', type: 'button', onclick: () => { F.choice = F.choice === 'llm' ? null : 'llm'; render(); } }, F.choice === 'llm' ? 'Close' : L.provider ? 'Change' : 'Set up') : null,
        canAdmin && L.enabled ? h('button', { class: 'btn', type: 'button', onclick: act(() => api('/api/llm/save', { business_id: S.current.id, provider: L.provider, model: L.model, base_url: L.base_url || undefined, enabled: false, capabilities: L.capabilities })) }, 'Turn off') : null,
        canAdmin && L.has_key ? h('button', { class: 'btn link', type: 'button', onclick: act(() => api('/api/llm/remove', { business_id: S.current.id })) }, 'Remove key') : null),
      L.enabled && L.in_use && !L.in_use.used ? h('div', { class: 'banner warn small', role: 'status' }, 'Not in use. ' + (L.in_use.message || '')) : null,
      !canAdmin ? h('p', { class: 'small muted' }, 'Only owners and admins can change this.') : null,
      F.choice === 'llm' && canAdmin ? llmForm() : null),
    localPanel(canAdmin),
    consentPanel(canAdmin),
    usagePanel(canAdmin),
    teamPanel(canAdmin),
    h('section', { class: 'panel' }, h('h2', {}, 'Your business'), h('p', { class: 'small muted' }, 'Answer mode: ' + (S.current.answer_mode === 'llm' ? 'with a language model' : 'local-only') + '. Your role: ' + S.current.role + '.')));
}

// ---------- sending data to an AI provider: the agreement
const CATEGORY_WORDS = { question: 'the question a customer asks', evidence_passages: 'the passages of your own knowledge that were retrieved for that question' };
function consentPanel(canAdmin) {
  const C = S.consent, b = S.current.id;
  const wrap = (...kids) => h('section', { class: 'panel', id: 'ai-consent' }, h('h2', {}, 'AI providers and your data'),
    h('p', { class: 'muted' }, 'Answers are made inside this service and nothing is sent to an AI provider unless your business agrees below. An agreement is for one provider and one version of its disclosure text. It can be withdrawn at any time, and a withdrawal blocks the very next question.'), ...kids);
  if (!C) return wrap(h('div', { class: 'banner warn small' }, 'Where your business stands could not be loaded, so nothing is offered. Nothing is sent to an AI provider.'));
  const active = (p) => (p.history || []).find((x) => x.status === 'granted');
  const shown = C.providers.filter((p) => p.consentable || active(p));
  const hidden = C.providers.filter((p) => !shown.includes(p));
  const reload = async (fn) => { try { S.consent = await fn(); } catch (e) { fail(e); return; } render(); };
  const read = async (p) => { try { S.disclosure[p.provider] = await api('/api/ai/disclosure', { business_id: b, provider: p.provider }); S.ack[p.provider] = false; } catch (e) { return fail(e); } render(); };
  const agree = async (p, D) => {
    try { S.consent = await api('/api/ai/consent/grant', { business_id: b, provider: p.provider, disclosure_version: D.version }); delete S.disclosure[p.provider]; delete S.ack[p.provider]; say('Your agreement for ' + p.label + ' is recorded. It applies to this version of the text only.', 'good'); }
    catch (e) { delete S.disclosure[p.provider]; say(e.message + ' Read the text again before agreeing.', 'warn'); }
  };
  const card = (p) => {
    const grant = active(p), D = S.disclosure[p.provider];
    const state = p.agreed && p.in_use ? ['good', 'Agreed · in use'] : p.agreed ? ['warn', 'Agreed · not in use (this service can no longer vouch for the provider’s terms)'] : grant ? ['warn', 'Agreed to an earlier version of the text · not in use'] : ['muted', 'Not agreed'];
    return h('div', { class: 'panel', style: 'margin-top:12px', 'data-provider': p.provider },
      h('div', { class: 'row' }, h('b', {}, p.label), h('span', { class: 'pill muted' }, 'a third party'), h('span', { class: 'pill ' + state[0] }, state[1])),
      h('div', { class: 'small muted' }, 'Disclosure version ' + p.disclosure_version + (p.terms_verified ? ' · the provider’s terms were verified by this service' : ' · the provider’s terms are NOT verified')),
      D ? h('div', { style: 'margin-top:8px' },
        h('div', { class: 'small' }, h('b', {}, 'Provider: '), D.label + ' (a third party, separate from this service)'),
        h('div', { class: 'small' }, h('b', {}, 'Disclosure version: '), D.version),
        h('div', { class: 'small' }, h('b', {}, 'What may be sent: '), (D.data_categories || []).map((c) => CATEGORY_WORDS[c] || c).join('; ')),
        h('pre', { class: 'disclosure', style: 'white-space:pre-wrap;margin:8px 0;padding:10px;border:1px solid var(--line)' }, D.text))
        : h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', onclick: () => read(p) }, 'Read the disclosure')),
      D && canAdmin && D.consentable && !p.agreed ? h('div', {},
        h('label', { style: 'display:flex;gap:8px;align-items:flex-start;margin-top:6px' }, h('input', { type: 'checkbox', checked: !!S.ack[p.provider], onchange: (e) => { S.ack[p.provider] = e.target.checked; render(); } }),
          'I have read this text, and I agree, for my business, that the data it lists may be sent to ' + D.label + ', a third party.'),
        h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', disabled: !S.ack[p.provider] || S.busy, onclick: () => agree(p, D) }, 'Agree'))) : null,
      grant && canAdmin ? h('div', { style: 'margin-top:8px' },
        h('button', { class: 'btn', type: 'button', onclick: () => reload(() => api('/api/ai/consent/revoke', { business_id: b, provider: p.provider })) }, 'Withdraw agreement'),
        h('p', { class: 'small muted' }, 'Withdrawing blocks any further sending to this provider, starting with the very next question. Text that was already sent cannot be recalled by this service.')) : null,
      !canAdmin ? h('p', { class: 'small muted' }, 'Only owners and admins can agree or withdraw.') : null);
  };
  return wrap(
    shown.length ? shown.map(card) : h('div', { class: 'banner info small' }, 'No AI provider is available yet. A provider appears here only after this service has verified its data-handling terms. Until then nothing is sent anywhere: answers use your own knowledge and the engine’s checks.'),
    shown.length && hidden.length ? h('p', { class: 'small muted' }, 'Not offered, because their terms are not verified yet: ' + hidden.map((p) => p.label).join('; ') + '.') : null);
}

// ---------- who answered, said plainly: only what the service recorded, never more
const PROVIDER_LABELS = { cloudflare: 'Cloudflare Workers AI', openrouter: 'OpenRouter', openai: 'OpenAI', anthropic: 'Anthropic', gemini: 'Google Gemini', openai_compatible: 'your own server', ollama: 'Ollama', llamacpp: 'llama.cpp' };
const MODE_LABELS = { deterministic: 'Deterministic engine', platform: 'Platform AI', byok: 'Your own AI provider', local: 'Your own computer' };
const TIER_LABELS = { local: 'Your own computer', byok: 'Your own AI provider', free: 'Free hosted AI', paid: 'Paid hosted AI' };
const STEP_WORDS = { cancelled: 'stopped by you', refused: 'not used', unavailable: 'could not answer', answered: 'answered', reasoning_failed: 'answered, but its claims did not verify', waiting_for_the_computer: 'waiting for your computer', no_model_needed: 'not needed' };
const WHY = {
  'consent:missing_or_revoked': 'this business has not agreed to send data to that provider (or has withdrawn)', 'consent:provider_unverified': 'the provider’s data-handling terms have not been verified by this service',
  'consent:no_disclosure': 'the provider has no disclosure text', 'config:ai_disabled': 'the service operator has not switched this kind of AI on', 'config:model_not_offered': 'that model is not offered',
  'config:platform_unavailable': 'the platform has no account with that provider right now', 'config:byok_not_configured': 'your own provider is not set up', 'config:byok_key_missing': 'your own provider has no key saved',
  'config:byok_key_unusable': 'the saved key could not be used', 'limit:no_plan': 'this business has no plan with stated limits', 'limit:trial_ended': 'the trial has ended', 'limit:quota_requests': 'the monthly request limit is reached',
  'limit:quota_tokens': 'the monthly token limit is reached', 'limit:quota_cost': 'the monthly cost cap is reached', 'limit:rate_limited': 'too many AI requests in the last minute', 'limit:platform_busy': 'the platform’s shared capacity is busy',
  'provider:circuit_open': 'the provider has been failing, so it is not being asked for now', 'local:no_companion': 'no companion is paired', 'local:client_inference_required': 'this client did not run the model on your computer',
  'reasoning:unsupported_claims': 'the model wrote something the evidence does not support', 'reasoning:invalid_json': 'the model’s reply could not be read', 'reasoning:schema': 'the model’s reply was not in the expected form',
  'reasoning:malformed_claim': 'the model’s reply was not in the expected form',
  'local:unsuitable_hardware': 'this computer said it cannot run the chosen model comfortably', 'config:no_hosted_model': 'no hosted model has been released for that step', 'limit:no_free_quota': 'this business has no free hosted allowance',
  'limit:free_quota_requests': 'the monthly free request allowance is used up', 'limit:free_quota_tokens': 'the monthly free token allowance is used up', 'limit:spend_cap': 'your monthly spending limit for paid hosted AI is reached',
};
const whyText = (k) => (k && (WHY[k] || (k.startsWith('provider:') ? 'the provider did not answer (' + k.slice(9).replace('_', ' ') + ')' : k))) || '';
/** The steps that were tried for an answer, in order, and what each did: "Your own computer: could not answer (the provider did not answer) → Free hosted AI (OpenRouter): answered". */
function triedLine(ex) {
  const a = ex && ex.attempts;
  if (!a || a.length < 2) return null;
  return a.map((x) => (TIER_LABELS[x.step] || x.step) + (x.provider && x.step !== 'local' ? ' (' + (PROVIDER_LABELS[x.provider] || x.provider) + ')' : '') + ': ' + (STEP_WORDS[x.outcome] || x.outcome) + (x.reason && x.outcome !== 'answered' ? ' — ' + whyText(x.reason) : '')).join(' → ');
}
function whoAnswered(ex) {
  const base = whoAnsweredBase(ex), tried = triedLine(ex);
  return base && tried ? base + ' Steps tried: ' + tried + '.' : base;
}
function whoAnsweredBase(ex) {
  if (!ex) return null;
  const prov = PROVIDER_LABELS[ex.provider] || ex.provider || '', model = ex.model ? ' (' + ex.model + ')' : '';
  const g = ex.generated_by, mode = ex.mode;
  if (g === 'model') return mode === 'local' ? 'A model on your own computer' + (prov ? ', ' + prov : '') + model + '. The evidence stayed on your computer. Every claim was checked against your knowledge.'
    : 'A language model at ' + prov + model + ' (' + (TIER_LABELS[ex.tier] || MODE_LABELS[mode] || mode).toLowerCase() + '). The question and the passages found were sent to ' + prov + ', a third party. Every claim was checked against your knowledge.';
  if (g === 'deterministic') return 'The deterministic engine. Nothing was sent to any AI provider.';
  if (g === 'deterministic_no_consent') return 'The deterministic engine, because ' + (whyText(ex.failure_kind) || 'no agreement exists') + '. Nothing was sent to any AI provider.';
  if (g === 'deterministic_fallback') return 'The deterministic engine, after the AI could not be used: ' + (whyText(ex.failure_kind) || 'it did not answer') + '. This is not a model’s answer' + (ex.mode === 'local' || ex.mode === 'byok' || ex.mode === 'platform' ? ', and it was not handed to any other provider' : '') + '.';
  if (g === 'unavailable') return 'No answer: the AI was unavailable (' + (whyText(ex.failure_kind) || 'it did not answer') + ') and this business chose not to fall back to the deterministic answer.';
  return 'Generated by: ' + g;
}

// ---------- how this business answers: LOCAL FIRST. Your own computer, then (only if you turn them on) your own provider account, free hosted AI, paid hosted AI.
const POLICY_TEXT = {
  local_only: ['Local only (recommended)', 'AI runs on your own computer. Nothing is ever sent to another company. If your computer cannot answer, you get the deterministic answer (it says so).'],
  local_free: ['Local, then free hosted', 'If your computer cannot answer, the question and the passages found for it may be sent to the platform’s free hosted AI, a third party. It has monthly quotas and may be unavailable. Needs your agreement to that provider.'],
  local_free_paid: ['Local, then free hosted, then paid hosted', 'As above, and if free hosted cannot answer either, paid hosted AI may be used. Paid use can cost money: you set a monthly limit and it stops there. Needs your agreement to each provider.'],
  byok_alternative: ['Local, then your own provider account', 'If your computer cannot answer, your own account (OpenAI, Anthropic, Google, OpenRouter or your own server) may be used, and you pay that provider. Needs your agreement.'],
  byok_preferred: ['Your own provider account first', 'Your own account answers instead of your computer. The question and the passages go to that provider. Needs your agreement.'],
  deterministic_only: ['No AI (deterministic only)', 'Nothing is sent anywhere. The engine answers from your knowledge by its own rules. Always available.'],
};
const POLICY_NEEDS = { local_only: ['local'], local_free: ['local', 'platform'], local_free_paid: ['local', 'platform'], byok_alternative: ['local', 'byok'], byok_preferred: ['byok'], deterministic_only: [] };
function answerModePanel(canAdmin) {
  const A = S.ai;
  const wrap = (...kids) => h('section', { class: 'panel', id: 'answer-mode' }, h('h2', {}, 'How this business answers'),
    h('p', { class: 'muted' }, 'AI runs on your own computer first. Anything beyond that is off until you turn it on, and each outside provider is used only after you agree to send it data. Every mode uses the same knowledge, the same evidence checks and the same claim-by-claim verification. The deterministic engine is always available.'), ...kids);
  if (!A) return wrap(h('div', { class: 'banner warn small' }, 'This business’s answer settings could not be loaded. Answers are deterministic.'));
  const F = S.form, cur = A.policy || 'custom', picked = F.policy || (cur === 'custom' ? null : cur);
  const on = new Set(A.switched_on_by_the_operator || []);
  const available = (k) => POLICY_NEEDS[k].every((m) => on.has(m));
  const steps = A.steps || [];
  const stepLine = (x) => h('li', {}, h('b', {}, TIER_LABELS[x.step] || x.step), h('span', { class: 'small' }, x.provider ? (PROVIDER_LABELS[x.provider] || x.provider) + (x.model ? ' · ' + x.model : '') : ''),
    h('span', { class: 'pill ' + (x.usable ? 'good' : 'warn') }, x.usable ? 'ready' : 'not usable now'), x.usable ? null : h('span', { class: 'small muted' }, whyText(x.refusal)));
  const status = A.settings.mode === 'deterministic' ? 'Deterministic: nothing is sent anywhere.' : steps.length && steps.some((x) => x.usable) ? 'The next question will start with ' + (TIER_LABELS[steps.find((x) => x.usable).step] || '').toLowerCase() + '.'
    : 'No step is usable now, so questions get the deterministic answer (and it says why).';
  const wantsPaid = picked === 'local_free_paid';
  const cap = F.cap != null ? F.cap : (A.settings.paid_cap_usd != null ? String(A.settings.paid_cap_usd) : '');
  const save = async () => {
    try {
      const n = Number(cap);
      if (wantsPaid && !(n > 0)) throw new Error('Set a monthly spending limit above zero for paid hosted AI.');
      S.ai = await api('/api/ai/settings/save', { business_id: S.current.id, policy: picked, fallback: F.fallback || A.settings.fallback, ...(wantsPaid ? { paid_cap_usd: n } : {}) });
      F.policy = null; F.cap = null;
      say('Saved. ' + (picked === 'deterministic_only' || (S.ai.steps || []).some((x) => x.usable) ? '' : 'No step is usable yet: ' + whyText((S.ai.steps[0] || {}).refusal) + '.'), picked === 'deterministic_only' || (S.ai.steps || []).some((x) => x.usable) ? 'good' : 'warn');
    } catch (e) { fail(e); }
  };
  const H = A.hosted || {};
  return wrap(
    h('div', { class: 'banner ' + (A.settings.mode === 'deterministic' || steps.some((x) => x.usable) ? 'good' : 'warn') + ' small', role: 'status' }, 'Now: ' + (cur === 'custom' ? 'a custom set of steps' : POLICY_TEXT[cur][0]) + '. ' + status),
    steps.length ? h('ol', { class: 'setup-steps', 'aria-label': 'The order answers are tried in' }, steps.map(stepLine)) : null,
    Object.keys(POLICY_TEXT).map((k) => h('label', { class: 'choice' + (picked === k ? ' on' : ''), style: 'display:block;margin-top:8px;cursor:' + (canAdmin && available(k) ? 'pointer' : 'default') },
      h('div', { class: 'row' }, h('input', { type: 'radio', name: 'policy', value: k, checked: picked === k, disabled: !canAdmin || !available(k), onchange: () => { F.policy = k; render(); } }), h('b', {}, POLICY_TEXT[k][0]),
        !available(k) ? h('span', { class: 'pill muted' }, 'not switched on for this service') : null, cur === k ? h('span', { class: 'pill good' }, 'current') : null),
      h('div', { class: 'small' }, POLICY_TEXT[k][1]))),
    (picked === 'local_free' || picked === 'local_free_paid') && H.free ? h('p', { class: 'small' }, h('b', {}, 'Free hosted AI: '), H.free.available ? H.free.label + ' at ' + (PROVIDER_LABELS[H.free.provider] || H.free.provider) + ' (a third party; free, with a monthly quota; may be unavailable).' : 'not available now (' + whyText(H.free.reason) + ').') : null,
    wantsPaid && H.paid ? h('p', { class: 'small' }, h('b', {}, 'Paid hosted AI: '), H.paid.available ? H.paid.label + ' at ' + (PROVIDER_LABELS[H.paid.provider] || H.paid.provider) + (H.paid.price_in_per_m != null ? ' · $' + H.paid.price_in_per_m + ' in / $' + H.paid.price_out_per_m + ' out per million tokens (an estimate, not an invoice)' : '') + '.' : 'not available now (' + whyText(H.paid.reason) + ').') : null,
    wantsPaid ? h('label', { class: 'field' }, 'Monthly spending limit for paid hosted AI (USD)', h('input', { type: 'number', min: '0', step: '1', inputmode: 'decimal', disabled: !canAdmin, value: cap, oninput: (e) => { F.cap = e.target.value; } })) : null,
    picked && picked !== 'local_only' && picked !== 'deterministic_only' ? h('p', { class: 'banner info small' }, 'Turning this on sends the question and the passages found for it to the outside provider(s) named above when an earlier step cannot answer. It works only after you agree to each provider’s data terms under “AI providers and your data”, and you can withdraw that at any time. You can turn it off here whenever you like.') : null,
    h('label', { style: 'display:flex;gap:8px;align-items:center;margin-top:10px' }, h('input', { type: 'checkbox', disabled: !canAdmin, checked: (F.fallback || A.settings.fallback) === 'deterministic', onchange: (e) => { F.fallback = e.target.checked ? 'deterministic' : 'none'; render(); } }),
      'If no step can answer, give the deterministic answer (the answer says it is not a model’s). Otherwise say the answer is unavailable.'),
    canAdmin ? h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', disabled: !picked || (picked === cur && !F.fallback && F.cap == null), onclick: save }, 'Save')) : h('p', { class: 'small muted' }, 'Only owners and admins can change this.'));
}

// ---------- the companion on this computer
async function refreshCompanion() {
  S.checking = true; render();
  const t0 = Date.now();
  S.chello = await Companion.hello();
  if (Date.now() - t0 < 700) await new Promise((r) => setTimeout(r, 700 - (Date.now() - t0)));  // long enough to be seen
  S.checking = false; S.checkedAt = new Date();
  S.cstatus = null;
  if (S.chello.present && !(await Companion.paired(S.current.id)) && S.current && !S._autoPairTried) {
    S._autoPairTried = true;  // the companion pairs by itself with an origin it trusts; otherwise the code box stays as the fallback
    try { await Companion.pair(S.current.id, '', S.session.user.email.split('@')[0] + '’s browser'); } catch (e) { S.pairMsg = { kind: 'warn', text: 'Automatic pairing did not work (' + e.message + '). Press “Look again”, or type the code the companion shows.' }; }
  }
  if (S.chello.present && await Companion.paired(S.current.id)) {
    try {
      const id = S.current.id, soft = (p) => p.catch(() => null);
      S.cstatus = { ...(await Companion.status(id)), recommend: await Companion.recommend(id), installed: await soft(Companion.installed(id)), health: await soft(Companion.health(id)), storage: await soft(Companion.storage(id)) };
      S.pairMsg = null;
    } catch (e) { S.pairMsg = { kind: 'warn', text: e.message }; }
  }
  render();
}
const HEALTH_WORDS = { ready: ['good', 'Ready: the model is installed and can answer.'], busy: ['info', 'Busy: another question is being answered.'], no_model_selected: ['warn', 'No model is chosen yet.'], runtime_not_running: ['bad', 'Ollama is not running. Start it, then press Refresh.'], model_missing: ['bad', 'The chosen model is no longer installed. Choose or download one.'] };
function healthLine(Hh) {
  const [kind, text] = HEALTH_WORDS[Hh.state] || ['info', Hh.state];
  const st = Hh.stats || {};
  return h('div', { class: 'banner ' + kind + ' small', role: 'status' }, text + (st.questions ? ' ' + st.questions + ' question' + (st.questions === 1 ? '' : 's') + ' answered since the companion started, ' + st.failures + ' failed' + (st.last_seconds != null ? '; the last took ' + st.last_seconds + ' s' : '') + '.' : ''));
}
function noModelSelectedEarly(C) { return !(C.installed && C.installed.selected) && !((S.ai && S.ai.companions) || []).some((c) => !c.revoked); }
function localPanel(canAdmin) {
  const b = S.current.id, C = S.cstatus, H = S.chello;
  if (H === null) setTimeout(() => { if (S.chello === null) { S.chello = false; refreshCompanion(); } }, 0);
  const embedded = canAdmin && S.current && (!S.current.onboarding_completed_at || S.reonboard);
  if (embedded && S.chello === false && !S._poll) { S._poll = setTimeout(() => { S._poll = null; refreshCompanion(); }, 4000); }
  const wrap = (...kids) => h('section', { class: 'panel' + (embedded ? ' inner' : ''), id: 'local-companion' }, h('h2', {}, 'Your own computer (local AI)'),
    h('p', { class: 'muted' }, 'A small program, the companion, runs on this computer and lets this application use a model that runs here. The question’s evidence goes to your computer and to no AI provider. It needs a model runtime (Ollama or llama.cpp) on the same computer.'), ...kids);
  const pairs = (S.ai && S.ai.companions || []).filter((c) => !c.revoked);
  const notes = [h('p', { class: 'small muted' }, 'Tested on: macOS on Apple silicon only, with stand-ins for the runtimes; no real model has been run through it. Linux and Windows code paths are not run on real machines. The page reaches the companion at http://127.0.0.1:47890 from Chrome-family browsers; other browsers are untested.')];
  const check = h('button', { class: 'btn', type: 'button', disabled: !!S.checking, onclick: refreshCompanion }, S.checking ? 'Checking…' : H && H.present ? 'Refresh' : 'Look again');
  const cmd = '.venv/bin/faqengine companion run';
  const safari = typeof navigator !== 'undefined' && typeof location !== 'undefined' && /^((?!chrome|android|crios|edg).)*safari/i.test(navigator.userAgent) && location.protocol === 'https:';
  if (!H || !H.present) return wrap(
    S.checking || H === null ? h('div', { class: 'banner info small', role: 'status' }, 'Looking for the companion on this computer…')
      : h('div', { class: 'banner warn', role: 'status' }, h('b', {}, 'Not found yet'), h('div', { class: 'small' }, 'Checked at ' + (S.checkedAt ? S.checkedAt.toLocaleTimeString() : 'just now') + '. The companion is a small program that must be running on this computer. Start it with one of these, then press “Look again” (this page also re-checks every few seconds):')),
    S.checking || H === null ? null : h('ul', { class: 'small how' },
      h('li', {}, 'Double-click ', h('b', {}, 'Start Local AI Companion.command'), ' in the project folder, or'),
      h('li', {}, 'run this in a terminal: ', h('code', {}, cmd), ' ', h('button', { class: 'btn link', type: 'button', onclick: async (e) => { try { await navigator.clipboard.writeText(cmd); e.target.textContent = 'Copied'; } catch { e.target.textContent = 'Select and copy it'; } } }, 'Copy')),
      h('li', {}, 'you also need Ollama running (https://ollama.com/download).')),
    safari ? h('div', { class: 'banner info small' }, 'Safari does not let a secure web page talk to a program on your own computer. Open this page in Chrome, Edge or Brave to connect to the companion.') : null,
    h('div', { class: 'row' }, check), ...notes, pairs.length ? h('p', { class: 'small muted' }, 'Paired for this business: ' + pairs.map((c) => c.label + ' (' + c.runtime + ' ' + c.model + ')').join('; ') + '.') : null);
  const code = h('input', { type: 'text', inputmode: 'numeric', autocomplete: 'off', placeholder: '1234-5678', maxlength: 9, 'aria-label': 'Pairing code', style: 'max-width:160px' });
  if (!C) return wrap(h('div', { class: 'banner good small' }, 'A companion is running on this computer' + (H.version ? ' (version ' + H.version + ')' : '') + '. This browser is not paired with it for this business.'),
    canAdmin ? h('div', {}, h('p', { class: 'small' }, 'In the companion’s window it shows a pairing code (valid five minutes, usable once). Type it here.'),
      h('div', { class: 'row' }, code, h('button', { class: 'btn primary', type: 'button', onclick: async () => { try { await Companion.pair(b, code.value, S.session.user.email.split('@')[0] + '’s browser'); S.pairMsg = null; await refreshCompanion(); say('Paired. Choose the model to use below.', 'good'); } catch (e) { S.pairMsg = { kind: 'bad', text: e.message }; render(); } } }, 'Pair'), check))
      : h('p', { class: 'small muted' }, 'Only owners and admins can pair a companion.'),
    S.pairMsg ? h('div', { class: 'banner ' + S.pairMsg.kind + ' small', role: 'status' }, S.pairMsg.text) : null, ...notes);
  const hw = C.hardware, rec = C.recommend || { models: [], limits: [] };
  const installed = Object.entries(C.runtimes).flatMap(([rt, d]) => (d.models || []).map((m) => ({ rt, m })));
  const fitOf = (tag) => ((rec.models || []).find((m) => m.ollama === tag) || {}).verdict || 'unknown';
  const register = async (rt, model) => {  // tells the service which model this computer runs and what the companion itself said about the fit; nothing secret
    await api('/api/local/companions/register', { business_id: b, label: S.session.user.email.split('@')[0] + '’s computer', platform: (hw.os + ' ' + hw.arch).slice(0, 80), runtime: rt, model, fit: fitOf(model) });
    S.ai = await api('/api/ai/settings/get', { business_id: b });
  };
  const use = async (rt, model) => {
    try { await Companion.select(b, rt, model, S.form.cpu ? 'cpu' : 'auto'); await register(rt, model); say('This computer will answer with ' + model + ' when “Your own computer” is first in the order above.', 'good'); await refreshCompanion(); } catch (e) { fail(e); }
  };
  const track = async (job, tag) => {
    for (;;) { await new Promise((r) => setTimeout(r, 1500)); const j = await Companion.pullStatus(b, job); S.pull = { tag, done: j.done, total: j.total }; render(); if (j.state !== 'running') { S.pull = null; if (j.state === 'failed') throw new Error('The download failed (' + (j.error || 'unknown') + ').'); return j; } }
  };
  // ONE confirmation sets everything up: the recommended model is downloaded from Ollama's library, selected, and registered. Nothing is downloaded before the person has seen the model, its size and the free space and ticked the box.
  const provision = async (modelId) => {
    try {
      const r = await Companion.provision(b, modelId);
      if (r.job) await track(r.job, r.model);
      await register('ollama', r.model);
      S.form.provisionOk = false;
      say(r.already_installed ? r.model + ' was already on this computer; it is now selected.' : r.model + ' is downloaded and ready. Local AI is set up.', 'good');
      await refreshCompanion();
    } catch (e) { S.pull = null; fail(e); }
  };
  const update = async (tag) => {
    try { const { job } = await Companion.update(b, 'ollama', tag); const j = await track(job, tag); say(j.updated ? tag + ' was updated to the newest version.' : tag + ' is already the newest version.', 'good'); await refreshCompanion(); } catch (e) { S.pull = null; fail(e); }
  };
  const remove = async (tag) => {
    try { await Companion.remove(b, 'ollama', tag); say(tag + ' was removed from this computer.', 'good'); await refreshCompanion(); } catch (e) { fail(e); }
  };
  { const have = ((C.runtimes.ollama || {}).models || []).map((m) => m.name || m);
    const speedOf = (tag) => ((rec.models || []).find((m) => m.ollama === tag) || {}).speed;
    const tooSmall = (tag) => speedOf(tag) === 'fastest';  // the 1B class: its replies fail the check that every answer must pass, so it would never be heard
    const reg = ((S.ai && S.ai.companions) || []).filter((c) => !c.revoked).at(-1);
    const order = [(rec.models || []).find((m) => m.id === rec.start_with), ...(rec.models || [])].filter(Boolean).map((m) => m.ollama);
    const rank = (t) => { const k = order.indexOf(t); return k < 0 ? 99 : k; };
    const better = [...have].filter((t) => !tooSmall(t)).sort((a, b) => rank(a) - rank(b))[0];
    if (canAdmin && have.length && !S.form.autoTried && (!reg || (tooSmall(reg.model) && better))) { const best = better || [...have].sort((a, b) => rank(a) - rank(b))[0]; S.form.autoTried = true; setTimeout(() => use('ollama', best), 0); }
    S.tooSmallNote = reg && tooSmall(reg.model) && !better ? reg.model : null; }
  const startRow = (rec.models || []).find((m) => m.id === rec.start_with) || null;
  const gbOf = (m) => (m.params_b * 0.6 * 1.05).toFixed(1);
  const noModelSelected = !(C.installed && C.installed.selected) && !((S.ai && S.ai.companions) || []).some((c) => !c.revoked);
  const ollama = C.runtimes.ollama || {};
  const setup = !canAdmin || !noModelSelected ? null : !ollama.running
    ? h('div', { class: 'banner info small' }, 'Ollama, the program that runs the model, is not running on this computer. Install it from https://ollama.com/download and start it, then press Refresh. (The companion does not install software.)')
    : startRow ? h('div', { class: 'banner good' },
      h('div', {}, h('b', {}, 'Set up local AI for me. ')), h('div', { class: 'small' }, 'Recommended for this computer: ' + startRow.id + ' (' + startRow.speed + '). It will be downloaded from Ollama’s own library to this computer: about ' + gbOf(startRow) + ' GB' + (C.storage && C.storage.free_gb != null ? ', and ' + C.storage.free_gb + ' GB is free' : '') + '. Then it answers your customers’ questions on this computer; nothing is sent to anyone.'),
      S.pull ? h('div', { class: 'bar', role: 'progressbar' }, h('div', { style: 'width:' + (S.pull.total ? Math.round(100 * S.pull.done / S.pull.total) : 8) + '%' })) : null,
      h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', disabled: !!S.pull, onclick: () => provision(startRow.id) }, S.pull ? 'Downloading ' + (S.pull.total ? Math.round(100 * S.pull.done / S.pull.total) + '%' : '…') : 'Download ' + startRow.id + ' (about ' + gbOf(startRow) + ' GB) and set up'),
        h('span', { class: 'small muted' }, 'Prefer another? Choose one of the options in the table below.')))
      : h('div', { class: 'banner warn small' }, 'No model in the list fits this computer comfortably. Hosted options may serve this business better.');
  const pull = async (tag) => {
    try {
      const { job } = await Companion.pull(b, 'ollama', tag);
      for (;;) { await new Promise((r) => setTimeout(r, 1500)); const j = await Companion.pullStatus(b, job); S.pull = { tag, done: j.done, total: j.total }; render(); if (j.state !== 'running') { S.pull = null; if (j.state === 'failed') throw new Error('The download failed (' + (j.error || 'unknown') + ').'); break; } }
      await refreshCompanion();
    } catch (e) { S.pull = null; fail(e); }
  };
  return wrap(h('div', { class: 'banner good small' }, 'Paired with the companion on this computer.'),
    h('div', { class: 'small' }, h('b', {}, 'This computer: '), hw.os + ' ' + hw.os_version + ' · ' + hw.arch + ' · ' + hw.cpu + ' · ' + hw.cores + ' cores · ' + (hw.ram_gb == null ? 'memory unknown' : hw.ram_gb + ' GB') + (hw.gpus.length ? ' · ' + hw.gpus.map((g) => g.name + (g.vram_gb ? ' ' + g.vram_gb + ' GB' : '') + (g.unified ? ' (shared)' : '') + (g.usable_for_models ? '' : ' (not used for models)')).join(', ') : ' · no GPU found')),
    Object.entries(C.runtimes).map(([rt, d]) => h('div', { class: 'small' }, h('b', {}, rt + ': '), d.running ? 'running · ' + ((d.models || []).join(', ') || 'no models yet') : d.installed ? 'installed, not running' : 'not found')),
    (rec.limits || []).map((x) => h('div', { class: 'banner warn small' }, x)),
    S.tooSmallNote ? h('div', { class: 'banner warn' }, S.tooSmallNote + ' is too small to write answers that pass the check every answer must pass, so its replies are never used. Download granite3.3-2b or another model marked “fast” below, then press “Use”.') : null,
    setup,
    C.health ? healthLine(C.health) : null,
    C.installed && (C.installed.runtimes.ollama || {}).models && C.installed.runtimes.ollama.models.length ? h('div', {}, h('h3', {}, 'On this computer'),
      h('div', { class: 'small muted' }, 'Models take ' + ((C.storage && C.storage.models_gb) || 0) + ' GB' + (C.storage && C.storage.free_gb != null ? '; ' + C.storage.free_gb + ' GB free' : '') + '.'),
      h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Model', 'Size', 'In use', ''].map((t) => h('th', {}, t)))),
        h('tbody', {}, C.installed.runtimes.ollama.models.map((m) => h('tr', {}, h('td', {}, m.name), h('td', {}, m.size_bytes ? (m.size_bytes / 2 ** 30).toFixed(1) + ' GB' : '–'), h('td', {}, m.selected ? h('span', { class: 'pill good' }, 'in use') : ''),
          h('td', {}, canAdmin ? [m.selected ? null : h('button', { class: 'btn link', type: 'button', onclick: () => use('ollama', m.name) }, 'Use'), h('button', { class: 'btn link', type: 'button', disabled: !!S.pull, onclick: () => update(m.name) }, 'Update'),
            m.selected ? null : h('button', { class: 'btn link', type: 'button', onclick: () => remove(m.name) }, 'Remove')] : null)))))),
      canAdmin ? h('label', { style: 'display:flex;gap:8px;align-items:center;margin-top:6px' }, h('input', { type: 'checkbox', checked: !!S.form.cpu, onchange: (e) => { S.form.cpu = e.target.checked; render(); } }), h('span', { class: 'small' }, 'Use the processor only when I choose “Use” (slower, but it works where the graphics memory is too small).')) : null) : null,
    h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Model', 'Speed', 'Fit on this computer', 'Why', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, rec.models.map((m) => h('tr', {}, h('td', {}, m.id, m.id === rec.start_with ? h('span', { class: 'pill good' }, 'start here') : null, Object.entries(rec.suggested || {}).some(([k, v]) => v === m.id && m.id !== rec.start_with) ? h('span', { class: 'pill muted' }, 'good ' + m.speed + ' pick') : null),
        h('td', { class: 'small' }, m.speed), h('td', {}, h('span', { class: 'pill ' + (m.verdict === 'comfortable' ? 'good' : m.verdict === 'possible' ? 'warn' : 'muted') }, m.verdict.replace('_', ' '))), h('td', { class: 'small' }, m.why),
        h('td', {}, canAdmin && m.ollama && m.verdict !== 'not_advised' ? (installed.some((x) => x.m === m.ollama) ? h('button', { class: 'btn link', type: 'button', onclick: () => use('ollama', m.ollama) }, 'Use') : h('button', { class: 'btn link', type: 'button', disabled: !!S.pull || !(C.runtimes.ollama || {}).running, title: (C.runtimes.ollama || {}).running ? '' : 'Ollama is not running yet', onclick: () => pull(m.ollama) }, S.pull && S.pull.tag === m.ollama ? 'Downloading ' + (S.pull.total ? Math.round(100 * S.pull.done / S.pull.total) + '%' : '…') : 'Download')) : null)))))),
    h('p', { class: 'small muted' }, 'Fit is estimated from each model’s size, not measured, and says nothing about quality. Licences are not recorded here: read a model’s own licence before you use it.'),
    h('div', { class: 'row' }, check, canAdmin ? h('button', { class: 'btn link', type: 'button', onclick: async () => { try { await Companion.unpair(b); for (const c of pairs) await api('/api/local/companions/revoke', { business_id: b, companion_id: c.id }); S.ai = await api('/api/ai/settings/get', { business_id: b }); S.cstatus = null; say('Unpaired. This computer can no longer be used by this business.', 'good'); } catch (e) { fail(e); } } }, 'Unpair this computer') : null),
    ...notes);
}

// ---------- usage
function usagePanel(canAdmin) {
  const wrap = (...kids) => h('section', { class: 'panel', id: 'usage' }, h('h2', {}, 'Usage and plan'), ...kids);
  if (!canAdmin) return wrap(h('p', { class: 'small muted' }, 'Only owners and admins can see usage.'));
  const U = S.usage;
  if (!U) return wrap(h('div', { class: 'banner warn small' }, 'Usage could not be loaded.'));
  const sm = U.summary, plan = sm.plan, used = sm.used;
  const none = { requests: 0, tokens_in: 0, tokens_out: 0, cost: null, failed: 0 };  // an engine that predates the free/paid split does not send these: show zero, never crash the page
  const byTier = { free: sm.used_free || none, paid: sm.used_paid || sm.used.platform || none };
  const fmt = (n) => (n == null ? '–' : Number(n).toLocaleString());
  const money = (c) => (c == null ? 'not priced' : '$' + Number(c).toFixed(4));
  return wrap(
    h('p', { class: 'small' }, plan ? h('span', {}, h('b', {}, 'Plan: '), plan.plan + ' · ' + (plan.requests_per_month == null ? 'no request limit stated' : fmt(plan.requests_per_month) + ' AI requests') + ', ' + (plan.tokens_per_month == null ? 'no token limit stated' : fmt(plan.tokens_per_month) + ' tokens') +
      ', ' + (plan.cost_cap_usd_per_month == null ? 'no cost cap stated' : '$' + plan.cost_cap_usd_per_month + ' estimated cost') + ' a month; ' + plan.per_minute + ' AI requests a minute.' + (plan.trial_ends_at ? ' Trial ends ' + plan.trial_ends_at.slice(0, 10) + '.' : ''))
      : 'No plan is set for this business, so hosted AI (free or paid) is not available. Your own provider and your own computer do not need one.'),
    sm.free_allowed ? h('p', { class: 'small' }, h('b', {}, 'Left this month (free hosted AI): '), fmt(sm.remaining_free.requests) + ' requests, ' + fmt(sm.remaining_free.tokens) + ' tokens') : null,
    sm.platform_allowed ? h('p', { class: 'small' }, h('b', {}, 'Left this month (paid hosted AI, plan): '), fmt(sm.remaining_platform.requests) + ' requests, ' + fmt(sm.remaining_platform.tokens) + ' tokens' + (sm.remaining_platform.cost_usd == null ? '' : ', $' + sm.remaining_platform.cost_usd)) : null,
    sm.spend_cap_usd != null ? h('p', { class: 'small' }, h('b', {}, 'Your spending limit for paid hosted AI: '), '$' + sm.spend_cap_usd + ' a month' + (sm.spend_cap_remaining_usd == null ? '' : ', $' + sm.spend_cap_remaining_usd + ' left') + '. Paid hosted AI stops when it is reached.') : null,
    h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['This month since ' + sm.period_start, 'AI requests', 'Failed', 'Tokens in / out', 'Estimated cost'].map((t) => h('th', {}, t)))),
      h('tbody', {}, [['free', byTier.free], ['paid', byTier.paid], ['byok', used.byok || none], ['local', used.local || none]].map(([m, u]) => h('tr', {}, h('td', {}, TIER_LABELS[m]), h('td', {}, fmt(u.requests)), h('td', {}, fmt(u.failed)), h('td', {}, fmt(u.tokens_in) + ' / ' + fmt(u.tokens_out)), h('td', {}, m === 'local' ? 'your computer' : m === 'byok' ? 'billed by your provider' : m === 'free' ? 'free' : money(u.cost))))))),
    h('p', { class: 'small muted' }, 'Cost is an estimate from a price that was verified or that you set; where there is no price it says so rather than guessing. Questions answered deterministically cost nothing and are not counted here.'),
    U.by_day.length ? h('details', {}, h('summary', { class: 'small' }, 'By day, last 30 days'), h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Day', 'What', 'Mode', 'Result', 'Count', 'Tokens in / out'].map((t) => h('th', {}, t)))),
      h('tbody', {}, U.by_day.slice(0, 60).map((r) => h('tr', {}, h('td', {}, r.day), h('td', {}, r.kind === 'llm_call' ? 'AI call' : r.kind), h('td', {}, r.mode || '–'), h('td', {}, r.status), h('td', {}, fmt(r.count)), h('td', {}, fmt(r.tokens_in) + ' / ' + fmt(r.tokens_out)))))))) : null);
}

// ---------- team: members, roles, invitations
function teamPanel(canAdmin) {
  const T = S.team || { members: [], invitations: [] }, me = S.session.user.id, isOwner = S.current.role === 'owner';
  const email = h('input', { type: 'email', id: 'invite-email', placeholder: 'colleague@example.com', autocomplete: 'off' });
  const role = h('select', { id: 'invite-role', 'aria-label': 'Role' }, ['viewer', 'editor', ...(isOwner ? ['admin'] : [])].map((r) => h('option', { value: r }, r)));
  const reload = async () => { await loadBusiness(); render(); };
  const invite = async () => {
    try {
      const { data, error } = await S.sb.rpc('create_invitation', { p_business: S.current.id, p_email: email.value, p_role: role.value });
      if (error) throw error;
      S.invite = { email: email.value.trim().toLowerCase(), url: location.origin + '/app#invite=' + data };
      await reload();
    } catch (e) { fail(e); }
  };
  const status = (i) => (i.accepted_at ? 'accepted' : i.revoked_at ? 'withdrawn' : new Date(i.expires_at) < new Date() ? 'expired' : 'open');
  return h('section', { class: 'panel', id: 'team' }, h('h2', {}, 'Team'),
    h('p', { class: 'muted' }, 'Roles: owners and admins manage settings and people; editors add knowledge; viewers can ask questions and read. A person joins by accepting an invitation sent to their own confirmed email address.'),
    h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Person', 'Role', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, T.members.map((m) => h('tr', {}, h('td', {}, ((m.profiles && m.profiles.display_name) || 'member') + (m.user_id === me ? ' (you)' : '')),
        h('td', {}, canAdmin && m.role !== 'owner' && m.user_id !== me ? h('select', { 'aria-label': 'Role of ' + ((m.profiles && m.profiles.display_name) || 'member'), onchange: async (e) => { const { error } = await S.sb.from('business_members').update({ role: e.target.value }).eq('business_id', S.current.id).eq('user_id', m.user_id); if (error) fail(error); else reload(); } },
          ['viewer', 'editor', 'admin'].map((r) => h('option', { value: r, selected: r === m.role, disabled: r === 'admin' && !isOwner }, r))) : h('span', { class: 'pill muted' }, m.role)),
        h('td', {}, canAdmin && m.role !== 'owner' && m.user_id !== me ? h('button', { class: 'btn link', type: 'button', onclick: async () => { if (!confirm('Remove this person from the business?')) return; const { error } = await S.sb.from('business_members').delete().eq('business_id', S.current.id).eq('user_id', m.user_id); if (error) fail(error); else reload(); } }, 'Remove') : null)))))),
    canAdmin ? h('div', {},
      h('h3', { style: 'margin-top:14px' }, 'Invite someone'),
      h('div', { class: 'row' }, email, role, h('button', { class: 'btn primary', type: 'button', onclick: invite }, 'Create invitation')),
      S.invite ? h('div', { class: 'banner good small', role: 'status' }, 'Invitation for ' + S.invite.email + '. Send them this link yourself (no email is sent by this service); it is shown only now, works only when they sign in with that address, once, within seven days:',
        h('div', { style: 'word-break:break-all;margin-top:6px' }, h('code', {}, S.invite.url))) : null,
      T.invitations.length ? h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Invited', 'Role', 'Status', ''].map((t) => h('th', {}, t)))),
        h('tbody', {}, T.invitations.slice(0, 20).map((i) => h('tr', {}, h('td', {}, i.email), h('td', {}, i.role), h('td', {}, status(i)), h('td', {}, status(i) === 'open' ? h('button', { class: 'btn link', type: 'button', onclick: async () => { const { error } = await S.sb.rpc('revoke_invitation', { p_invitation: i.id }); if (error) fail(error); else reload(); } }, 'Withdraw') : null)))))) : null) : null);
}

// ---------- history: the latest answers and who generated each
function historyTab() {
  if (S.history === null) { S.history = { items: [], loading: true, done: false }; loadHistory(true); }
  const H = S.history;
  const short = (ex) => (ex.generated_by === 'model' ? (TIER_LABELS[ex.tier] || MODE_LABELS[ex.mode] || ex.mode) + ' · ' + (PROVIDER_LABELS[ex.provider] || ex.provider || '') + (ex.model ? ' ' + ex.model : '') : ex.generated_by === 'deterministic' ? 'Deterministic engine' : ex.generated_by === 'deterministic_fallback' ? 'Deterministic (after: ' + whyText(ex.failure_kind) + ')'
    : ex.generated_by === 'deterministic_no_consent' ? 'Deterministic (no agreement)' : ex.generated_by === 'unavailable' ? 'Unavailable' : ex.generated_by || '–');
  return h('div', {}, h('section', { class: 'panel' }, h('h2', {}, 'History'), h('p', { class: 'muted' }, 'The latest answers, newest first, and exactly who generated each. Questions are shown only if this business keeps them.'),
    H.items.length ? h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['When', 'Question', 'Result', 'Generated by', 'Time'].map((t) => h('th', {}, t)))),
      h('tbody', {}, H.items.map((i) => h('tr', {}, h('td', { class: 'small' }, new Date(i.at).toLocaleString()), h('td', {}, clip(i.question || '(not kept)', 60)), h('td', {}, (OUTCOMES[i.outcome] || ['', i.outcome])[1]), h('td', { class: 'small' }, short(i.execution || {})), h('td', { class: 'small' }, i.latency_ms != null ? i.latency_ms + ' ms' : '–')))))) : h('p', { class: 'muted' }, H.loading ? 'Loading…' : 'Nothing yet. Ask a question under “Test it”.'),
    !H.done && H.items.length ? h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', onclick: () => loadHistory(false) }, 'Older')) : null));
}
async function loadHistory(first) {
  const H = S.history || (S.history = { items: [], loading: false, done: false });
  try {
    const r = await api('/api/history', { business_id: S.current.id, limit: 50, ...(first || !H.items.length ? {} : { before: H.items[H.items.length - 1].at }) });
    H.items = first ? r.items : H.items.concat(r.items);
    H.done = r.items.length < 50;
  } catch (e) { fail(e); }
  H.loading = false;
  render();
}

// ---------- an invitation link is a fragment (#invite=...), so the secret is never sent to any server by the page load itself
async function acceptInvitation() {
  const m = /^#invite=([0-9a-f]{64})$/.exec(location.hash);
  if (!m || !S.session) return;
  history.replaceState(null, '', location.pathname + location.search);  // the secret leaves the address bar at once
  const { error } = await S.sb.rpc('accept_invitation', { p_token: m[1] });
  say(error ? 'This invitation cannot be used. It may have expired, been used already, or been sent to a different email address than the one you are signed in with.' : 'You have joined the business.', error ? 'bad' : 'good');
  if (!error) await load();
}

// ---------- knowledge
function knowledgeTab(canWrite) {
  const b = S.current.id;
  const ext = (n) => (n.split('.').pop() || '').toLowerCase();
  const note = h('div', { id: 'note' });
  const addSource = async (row, afterInsert) => {
    const { error } = await S.sb.from('knowledge_sources').insert({ business_id: b, ...row });
    if (error) throw new Error(error.code === '23505' ? 'A source with that name already exists in this business.' : error.message);
    if (afterInsert) await afterInsert();
    const done = await api('/api/sources/ingest', { business_id: b, source_id: row.id });
    return done;
  };
  const upload = async (files) => {
    for (const f of files) {
      if (SECRET_FILE.test(f.name)) { say(f.name + ' looks like a file of secrets (keys or passwords), not knowledge, so it was not uploaded. To connect an AI provider, drop it on the provider form in Settings instead.', 'warn'); continue; }
      if (f.size > 25 * 1024 * 1024) { say(f.name + ' is larger than 25 MB.', 'warn'); continue; }
      const id = crypto.randomUUID(), path = b + '/' + id + '/' + f.name, e = ext(f.name);
      say('Reading ' + f.name + '…');
      try {
        const up = await S.sb.storage.from('knowledge-files').upload(path, f, { contentType: MIME[e] || 'application/octet-stream', upsert: false });
        if (up.error) throw up.error;
        const r = await addSource({ id, name: f.name, source_type: ['csv', 'tsv', 'xlsx', 'json', 'jsonl'].includes(e) ? 'faq_table' : 'document', storage_path: path });
        say(f.name + ' is ready' + (r.faqs ? ': ' + r.faqs + ' FAQ entries' : '') + (r.chunks ? ', ' + r.chunks + ' passages' : '') + (r.flagged_passages ? ' (' + r.flagged_passages + ' set aside: they speak to an AI, not to a customer)' : '') + '.', 'good');
      } catch (err) { fail(err); }
    }
    await loadBusiness(); render();
  };
  const addWeb = async (url, whole) => {
    say('Reading ' + url + '…');
    try { const r = await addSource({ id: crypto.randomUUID(), name: url, source_type: whole ? 'website' : 'web_page', url }); say('Read ' + (r.chunks || 0) + ' passages from the page.', 'good'); } catch (err) { fail(err); }
    await loadBusiness(); render();
  };
  const reread = async (s) => { say('Reading ' + s.name + ' again…'); try { const r = await api('/api/sources/ingest', { business_id: b, source_id: s.id }); say(r.unchanged ? s.name + ' has not changed.' : s.name + ' updated: ' + (r.chunks_added || 0) + ' passages added, ' + (r.chunks_removed || 0) + ' removed.', 'good'); } catch (err) { fail(err); } await loadBusiness(); render(); };
  const setDates = async (s) => {  // when a source stops or starts being current, and which source it replaces (a file can also say so itself, in a header)
    const day = (label, now) => { const v = prompt(label + ' (YYYY-MM-DD, empty for none)', now || ''); if (v === null) return undefined; const t = v.trim(); if (t && !/^\d{4}-\d{2}-\d{2}$/.test(t)) { alert('Write the date as 2026-12-31.'); return undefined; } return t || null; };
    const from = day('Valid from', s.valid_from); if (from === undefined) return;
    const until = day('Valid until', s.valid_until); if (until === undefined) return;
    const rep = prompt('Replaces which sources? (names separated by commas, empty for none)', s.supersedes || ''); if (rep === null) return;
    const { error } = await S.sb.from('knowledge_sources').update({ valid_from: from, valid_until: until, supersedes: rep.trim() || null }).eq('id', s.id);
    if (error) return fail(error);
    say('Dates saved. They apply to the next question.', 'good'); await loadBusiness(); render();
  };
  const remove = async (s) => { if (!confirm('Remove ' + s.name + ' and everything read from it?')) return; const { error } = await S.sb.from('knowledge_sources').delete().eq('id', s.id); if (error) return fail(error); await loadBusiness(); render(); };
  const fileInput = h('input', { type: 'file', id: 'files', multiple: true, accept: '.csv,.tsv,.xlsx,.json,.jsonl,.pdf,.docx,.txt,.md,.html,.htm', onchange: (e) => upload([...e.target.files]) });
  const url = h('input', { type: 'url', id: 'webaddr', placeholder: 'https://example.com/help' });
  const whole = h('input', { type: 'checkbox', id: 'whole' });
  return h('div', {},
    note,
    canWrite ? h('section', { class: 'panel' }, h('h2', {}, 'Add knowledge'), h('p', { class: 'muted' }, 'FAQ tables (a question and an answer per row) can answer directly. Documents and web pages are cut into passages that support an answer, but are never given as a stored answer.'),
      h('label', { class: 'field', for: 'files' }, 'Files'), fileInput,
      h('label', { class: 'field', for: 'webaddr' }, 'Or a web page'), url,
      h('div', { class: 'row' }, h('label', { class: 'small' }, whole, ' Also read up to 9 pages it links to on the same site'), h('button', { class: 'btn', type: 'button', onclick: () => url.value && addWeb(url.value, whole.checked) }, 'Read the page')),
      h('p', { class: 'small muted' }, 'Pages are read the way a browser would without running scripts; pages that build themselves with JavaScript, need a login or ask not to be read cannot be read.')) : null,
    h('section', { class: 'panel' }, h('h2', {}, 'Sources'),
      S.sources.length ? h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Name', 'Kind', 'Status', 'Version', 'Contents', 'Current', ''].map((t) => h('th', {}, t)))),
        h('tbody', {}, S.sources.map((s) => h('tr', {}, h('td', {}, clip(s.name, 48)), h('td', { class: 'small' }, s.source_type.replace('_', ' ')),
          h('td', {}, h('span', { class: 'pill ' + (s.status === 'ready' ? 'good' : s.status === 'failed' ? 'bad' : 'muted') }, s.status), s.error ? h('div', { class: 'small muted' }, clip(s.error, 90)) : null, s.parse_status === 'needs_browser' ? h('div', { class: 'small muted' }, 'Needs a browser to read: save the page and upload it.') : null),
          h('td', {}, 'v' + s.version), h('td', { class: 'small' }, (s.entry_count ? s.entry_count + ' FAQ' : '') + (s.entry_count && s.chunk_count ? ', ' : '') + (s.chunk_count ? s.chunk_count + ' passages' : '')),
          h('td', { class: 'small' }, freshness(s)),
          h('td', { class: 'actions' }, canWrite ? [h('button', { class: 'btn link', type: 'button', onclick: () => reread(s) }, 'Read again'), h('button', { class: 'btn link', type: 'button', onclick: () => setDates(s) }, 'Dates'), h('button', { class: 'btn link', type: 'button', onclick: () => remove(s) }, 'Remove')] : null))))))
        : h('p', { class: 'muted' }, 'Nothing here yet. Add a FAQ file to begin.')));
}

function freshness(s) {
  const today = new Date().toISOString().slice(0, 10), bits = [];
  if (s.valid_until && s.valid_until < today) bits.push(h('span', { class: 'pill bad' }, 'expired ' + s.valid_until));
  else if (s.valid_from && s.valid_from > today) bits.push(h('span', { class: 'pill muted' }, 'from ' + s.valid_from));
  else if (s.valid_until) bits.push('until ' + s.valid_until);
  else bits.push('no end date');
  if (s.supersedes) bits.push(h('div', { class: 'muted' }, 'replaces ' + clip(s.supersedes, 40)));
  return bits;
}

// ---------- the playground: ask a question and see why the answer was given or refused
function playgroundTab() {
  const input = h('input', { type: 'text', id: 'q', placeholder: 'Type a question a customer might ask…', maxlength: 1000, autocomplete: 'off', onkeydown: (e) => { if (e.key === 'Enter') ask(input.value); } });
  setTimeout(() => input.focus(), 0);
  return h('div', {},
    h('section', { class: 'panel' }, h('h2', {}, 'Try it'), h('p', { class: 'muted' }, 'Ask what a customer might. You see the answer, why it was given, and what was not covered.'),
      h('div', { class: 'row' }, input, h('button', { class: 'btn primary', type: 'button', disabled: S.busy, onclick: () => ask(input.value) }, S.busy ? (S.local ? 'Waiting for your computer…' : 'Thinking…') : 'Ask'), S.busy && S.local ? h('button', { class: 'btn', type: 'button', onclick: () => Companion.cancel(S.current.id, S.local.requestId).catch(() => {}) }, 'Cancel') : null)),
    S.result ? resultPanel(S.result) : null);
}
async function ask(text) {
  text = (text || '').trim();
  if (!text) return;
  S.busy = true; S.local = null; render();
  try { S.result = (S.ai && S.ai.settings && S.ai.settings.mode === 'local' && S.ai.route && S.ai.route.usable) ? await askLocal(text) : (await api('/api/respond', { business_id: S.current.id, query: text })).result; S.msg = null; } catch (e) { S.msg = { text: e.message, kind: 'bad' }; }
  S.busy = false; S.local = null; render();
}
// Local mode, in two phases. The service runs everything up to the model and hands back what the model must be given; this page passes it to the companion on THIS computer (nothing goes to any provider);
// what comes back is returned to the service, which verifies every claim against the evidence it kept before anything is shown.
async function askLocal(text) {
  const b = S.current.id;
  const prep = await api('/api/local/prepare', { business_id: b, query: text });
  if (prep.done) return prep.result.result || prep.result;
  S.local = { requestId: prep.request_id }; render();
  let reply;
  try { reply = await Companion.infer(b, prep.request_id, prep.prompt, 240); } catch (e) { reply = { failure: /no paired/.test(e.message) ? 'not_connected' : 'not_connected' }; }
  const kinds = ['network', 'timeout', 'not_found', 'server', 'cancelled', 'not_connected', 'auth', 'bad_request'];
  const failure = reply.failure ? (kinds.includes(reply.failure) ? reply.failure : 'server') : undefined;
  const done = await api('/api/local/complete', { business_id: b, request_id: prep.request_id, text: reply.text || undefined, failure, tokens_in: reply.tokens_in || 0, tokens_out: reply.tokens_out || 0 });
  return done.result;
}
function resultPanel(R) {
  const [cls, title] = OUTCOMES[R.outcome] || ['info', R.outcome];
  const M = R.llm || {}, V = R.verification, F = R.fallback, D = R.retrieval || {}, diag = D.diagnostics || {};
  const line = (label, value) => h('div', { class: 'small' }, h('b', {}, label + ': '), value);
  return h('section', { class: 'panel' },
    h('span', { class: 'pill ' + cls }, title),
    R.execution ? h('div', { class: 'small', style: 'margin:6px 0' }, h('b', {}, 'Generated by: '), whoAnswered(R.execution)) : null,
    R.answer ? h('p', { style: 'margin:.8em 0;white-space:pre-wrap' }, R.answer) : null,
    R.partial_answer ? h('p', { style: 'margin:.8em 0;white-space:pre-wrap' }, R.partial_answer) : null,
    R.message ? h('p', { style: 'margin:.8em 0' }, R.message) : null,
    (R.sources || []).length ? h('div', {}, h('b', { class: 'small' }, 'Sources'), (R.sources || []).map((s) => line('[' + s.ref + '] ' + (s.kind === 'faq' ? 'FAQ' : 'Document'), s.location + (s.kind === 'faq' ? '' : ' · ' + clip(s.text || '', 120))))) : null,
    (R.unanswered_parts || []).length ? line('Not supported by anything available', R.unanswered_parts.join('; ')) : null,
    (R.outdated || []).length ? h('div', { class: 'banner warn small' }, 'Only out-of-date material covers this, and it was not used: ' + R.outdated.map((o) => o.source + ' (' + o.reason + ')').join('; ')) : null,
    R.clarification && R.clarification.options.length ? line('It might mean', R.clarification.options.join(' · ')) : null,
    h('div', { class: 'evidence', style: 'margin-top:12px' },
      line('Evidence', (R.evidence_class || '').replace('_', ' ')),
      line('Retrieval similarity', num(R.retrieval_similarity) + ' (how alike the best FAQ question is: not a probability, not how sure the answer is)'),
      line('Answer sufficiency / evidence support', Math.round(100 * (R.answer_sufficiency || 0)) + '% / ' + Math.round(100 * (R.evidence_support || 0)) + '%'),
      line('Language model', M.used ? 'used (' + M.capability + ')' : M.enabled ? 'tried, not used' + (M.failure ? ' (' + M.failure.replace('_', ' ') + ')' : '') : 'not used (local-only)'),
      V ? line('Check of the wording', V.passed ? 'passed' : 'failed: ' + (V.issues || []).slice(0, 4).map((i) => i.type.replace('_', ' ')).join(', ')) : null,
      F ? h('div', { class: 'banner warn small' }, F.detail) : null,
      R.assist && R.assist.attempted ? line('Extra search', R.assist.queries.length ? 'tried “' + R.assist.queries.join('”, “') + '”' + (R.assist.improved ? ' and found more evidence' : ' and found nothing more') : 'no useful suggestion') : null),
    h('details', { style: 'margin-top:10px' }, h('summary', { class: 'small' }, 'Retrieval diagnostics'),
      h('p', { class: 'small muted' }, 'Search: ' + (diag.method || '') + (diag.alpha != null ? ' (keyword share ' + diag.alpha + ')' : '') + ', ' + num(diag.embed_ms, 1) + ' ms to read the question, ' + num(diag.search_ms, 1) + ' ms to search ' + (diag.entries || '?') + ' entries.'),
      (D.quarantined || []).length ? h('div', { class: 'banner warn small' }, 'Set aside because it speaks to an AI, not to a customer: ' + D.quarantined.map((q) => q.id).join(', ')) : null,
      h('table', {}, h('thead', {}, h('tr', {}, ['Candidate', 'Source', 'Meaning', 'Keywords', 'Combined'].map((t) => h('th', {}, t)))),
        h('tbody', {}, (D.candidates || []).map((c) => h('tr', {}, h('td', {}, clip(c.question || c.entry_id, 56)), h('td', { class: 'small' }, c.kind === 'passage' ? c.source || 'document' : 'FAQ'), h('td', {}, num(c.similarity_score)), h('td', {}, num(c.keyword_score, 1)), h('td', {}, num(c.combined_score))))))));
}

boot();
