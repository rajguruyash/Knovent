// The browser's side of the local companion (a small program on THIS computer; see docs/local-companion.md). It does three things: finds the companion, pairs with it, and signs every request to it.
// The pairing secret is imported once as a NON-EXTRACTABLE signing key and kept in IndexedDB: no script on this page can read it back, only use it to sign. Nothing is ever sent to the companion
// unsigned except the pairing code the person typed. The companion's address is this computer's loopback address; there is no setting to point it anywhere else.
'use strict';

const Companion = (() => {
  // Served from this computer, the page reaches the companion THROUGH its own server (same address, so every browser allows it, Safari included); a hosted page reaches it directly on the loopback address.
  const ON_THIS_COMPUTER = typeof location !== 'undefined' && ['127.0.0.1', 'localhost'].includes(location.hostname);
  const BASE = ON_THIS_COMPUTER ? '/companion' : 'http://127.0.0.1:47890';
  const enc = new TextEncoder();
  const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
  const randomNonce = () => hex(crypto.getRandomValues(new Uint8Array(12)).buffer) + 'n';

  // ---------- where the signing key lives
  function idb() {
    return new Promise((resolve, reject) => {
      const open = indexedDB.open('faqengine-companion', 1);
      open.onupgradeneeded = () => open.result.createObjectStore('keys', { keyPath: 'business' });
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(new Error('This browser will not keep the companion pairing (is private browsing on?).'));
    });
  }
  async function store(mode, fn) {
    const db = await idb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('keys', mode);
      const req = fn(tx.objectStore('keys'));
      tx.oncomplete = () => resolve(req && req.result);
      tx.onerror = () => reject(tx.error);
    });
  }
  const saved = (business) => store('readonly', (s) => s.get(business));

  // ---------- signing: HMAC-SHA256 over METHOD, path, timestamp, nonce and the hash of the body, exactly as companion/auth.py checks it
  async function sha256Hex(bytes) {
    return hex(await crypto.subtle.digest('SHA-256', bytes));
  }
  async function signature(key, method, path, ts, nonce, bodyText, business) {
    const material = [method.toUpperCase(), path, ts, nonce, await sha256Hex(enc.encode(bodyText)), ...(business ? [business] : [])].join('\n');  // a key made for a business signs for that business only
    return hex(await crypto.subtle.sign('HMAC', key, enc.encode(material)));
  }

  /** Why a pairing was refused is said plainly, with a `code` the page can act on; only a request that never got an answer is reported as "not reachable". */
  function failure(code, message) { const e = new Error(message); e.code = code; return e; }
  async function pairNow(business, code, label) {
    let res;
    try { res = await fetch(BASE + '/v1/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...(code ? { code } : {}), label, business }), mode: 'cors', cache: 'no-store' }); } catch { throw failure('unreachable', 'The companion is not reachable on this computer. Is it running?'); }
    if (!res.ok) {
      let why = ''; try { why = (await res.json()).error || ''; } catch { /* not JSON */ }
      if (why === 'origin_not_trusted') throw failure('origin_not_trusted', 'This website is not set up on your computer yet. Install Knovent from this site, or type the pairing code the companion shows.');
      if (why === 'code_not_accepted' || code) throw failure('code_not_accepted', 'The code was not accepted. It works once, for five minutes: ask the companion for a new one.');
      throw failure('refused', 'The companion answered but refused to pair (' + res.status + ').');
    }
    const { key_id: keyId, secret, bound } = await res.json();
    const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);  // extractable: false
    await store('readwrite', (st) => st.put({ business, keyId, key, bound: !!bound }));
    return keyId;
  }

  async function request(business, method, path, bodyObj, retried) {
    const k = await saved(business);
    if (!k) throw new Error('This business has no paired companion in this browser. Pair it in Settings.');
    const bodyText = bodyObj === undefined ? '' : JSON.stringify(bodyObj);
    const ts = String(Math.floor(Date.now() / 1000)), nonce = randomNonce();
    const headers = { 'X-Companion-Key': k.keyId, 'X-Companion-Ts': ts, 'X-Companion-Nonce': nonce, 'X-Companion-Sig': await signature(k.key, method, path, ts, nonce, bodyText, k.bound ? business : null) };
    if (k.bound) headers['X-Companion-Business'] = business;
    if (bodyText) headers['Content-Type'] = 'application/json';
    let res;
    try { res = await fetch(BASE + path, { method, headers, body: bodyText || undefined, mode: 'cors', cache: 'no-store' }); } catch { throw failure('unreachable', 'The companion is not reachable on this computer. Is it running?'); }
    let data = {};
    try { data = await res.json(); } catch { /* not JSON */ }
    if ((res.status === 401 || res.status === 403) && !retried) {  // the companion does not know this key (it was restarted, or its pairing file changed): pair again by itself, which a page served from this computer may do, and retry once
      try { await pairNow(business, '', 'this browser'); return await request(business, method, path, bodyObj, true); } catch { /* falls through to the plain message */ }
    }
    if (res.status === 401 || res.status === 403) throw failure('not_accepted', 'The companion no longer accepts this browser. Press “Look again”, or pair it with the code it shows.');
    if (!res.ok) throw new Error(data.error || 'The companion refused the request (' + res.status + ').');
    return data;
  }

  return {
    BASE,
    /** Is a companion running on this computer? Nothing but that is learned. */
    async hello() {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 1500);
      try {
        const res = await fetch(BASE + '/v1/hello', { mode: 'cors', cache: 'no-store', signal: ctl.signal });
        return res.ok ? { present: true, ...(await res.json()) } : { present: false };
      } catch { return { present: false }; } finally { clearTimeout(t); }
    },
    async paired(business) { return !!(await saved(business)); },
    /** Pairs this browser with the companion for one business, using the code the companion printed on this computer's screen. */
    async pair(business, code, label) { return pairNow(business, code, label); },
    async forget(business) { await store('readwrite', (s) => s.delete(business)); },
    status: (b) => request(b, 'GET', '/v1/status'),
    recommend: (b) => request(b, 'GET', '/v1/recommend'),
    select: (b, runtime, model, device) => request(b, 'POST', '/v1/models/select', { runtime, model, device: device || 'auto' }),
    installed: (b) => request(b, 'GET', '/v1/models/installed'),
    health: (b) => request(b, 'GET', '/v1/health'),
    storage: (b) => request(b, 'GET', '/v1/storage'),
    remove: (b, runtime, model) => request(b, 'POST', '/v1/models/delete', { runtime, model }),
    update: (b, runtime, model) => request(b, 'POST', '/v1/models/update', { runtime, model }),
    /** Downloads and selects a model for this computer, only because the person confirmed (the page shows the model, its size and the free space first). */
    provision: (b, model) => request(b, 'POST', '/v1/provision', { confirm: true, ...(model ? { model } : {}) }),
    pull: (b, runtime, model) => request(b, 'POST', '/v1/models/pull', { runtime, model }),
    pullStatus: (b, job) => request(b, 'GET', '/v1/models/pull/' + encodeURIComponent(job)),
    /** Asks the local model. The prompt goes from this page to this computer's companion and nowhere else. */
    infer: (b, requestId, prompt, timeout) => request(b, 'POST', '/v1/infer', { request_id: requestId, system: prompt.system, user: prompt.user, schema: prompt.schema, timeout }),
    cancel: (b, requestId) => request(b, 'POST', '/v1/cancel', { request_id: requestId }),
    unpair: async (b) => { try { await request(b, 'POST', '/v1/unpair', {}); } finally { await Companion.forget(b); } },
    _signature: signature,
  };
})();
