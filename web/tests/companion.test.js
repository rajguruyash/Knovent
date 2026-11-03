// The browser's companion client (public/companion.js), run in a sandbox with WebCrypto, a stand-in IndexedDB and a stand-in fetch. What it proves: its signature is byte-for-byte what the Python
// companion checks (a shared test vector), the pairing key cannot be read back, every signed request carries the four headers and nothing unsigned is sent after pairing, and failures read plainly.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { webcrypto } from "node:crypto";
import vm from "node:vm";

const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "public", "companion.js"), "utf8");
const SECRET = "0123456789abcdef".repeat(4);
// computed by faqengine.companion.auth.sign (Python) for the same inputs: the two sides must agree
const VECTOR_POST = "97dd429c66b4a970691728d0e0aa67eb018e3049d4cd6273402138bbad9ae5fa";
const VECTOR_GET = "d1f901c37b637b5f29d5a49fc7252769e00dc5e099d6107edbe1af04db02c17e";

const plain = (x) => JSON.parse(JSON.stringify(x)); // (objects made inside the sandbox have the sandbox's prototypes)

/** A tiny IndexedDB: one object store keyed by `business`. */
function fakeIndexedDB() {
  const rows = new Map();
  return { rows, open() {
    const req = { result: { createObjectStore() {}, transaction: () => {
      const tx = { objectStore: () => ({ get: (k) => { const r = { result: rows.get(k) }; queueMicrotask(() => tx.oncomplete()); return r; }, put: (v) => { rows.set(v.business, v); queueMicrotask(() => tx.oncomplete()); return { result: undefined }; }, delete: (k) => { rows.delete(k); queueMicrotask(() => tx.oncomplete()); return { result: undefined }; } }) };
      return tx;
    } } };
    queueMicrotask(() => { req.onupgradeneeded?.(); req.onsuccess(); });
    return req;
  } };
}

function sandbox(handler = () => ({ status: 200, body: {} })) {
  const calls = [];
  const idb = fakeIndexedDB();
  const ctx = vm.createContext({
    crypto: webcrypto, TextEncoder, indexedDB: idb, AbortController, setTimeout, clearTimeout, Date, JSON, String, Math, Uint8Array, Error, Promise,
    fetch: async (url, init = {}) => {
      calls.push({ url, ...init });
      const { status, body } = handler(url, init);
      return { ok: status < 400, status, json: async () => body };
    },
  });
  vm.runInContext(source + "\nglobalThis.Companion = Companion;", ctx);
  return { C: ctx.Companion, calls, idb, ctx };
}

test("the browser signs exactly what the companion verifies (a shared vector with the Python side)", async () => {
  const { C } = sandbox();
  const key = await webcrypto.subtle.importKey("raw", new TextEncoder().encode(SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  assert.equal(await C._signature(key, "post", "/v1/infer", "1800000000", "nonce-0123456789ab", '{"user":"café ✓"}'), VECTOR_POST); // a body with non-ASCII text
  assert.equal(await C._signature(key, "GET", "/v1/status", "1800000000", "nonce-0123456789ab", ""), VECTOR_GET);
});

test("pairing stores a key that cannot be read back, and nothing but the code is sent unsigned", async () => {
  const { C, calls, idb } = sandbox((url) => (url.endsWith("/v1/pair") ? { status: 200, body: { key_id: "k".repeat(16), secret: SECRET } } : { status: 200, body: {} }));
  assert.equal(await C.paired("biz"), false);
  assert.equal(await C.pair("biz", "1234-5678", "my browser"), "k".repeat(16));
  assert.equal(await C.paired("biz"), true);
  const row = idb.rows.get("biz");
  assert.equal(row.key.extractable, false); // the page can sign with it, and nothing can export it
  await assert.rejects(() => webcrypto.subtle.exportKey("raw", row.key));
  assert.deepEqual(JSON.parse(calls[0].body), { code: "1234-5678", label: "my browser" });
  assert.equal(calls[0].url, "http://127.0.0.1:47890/v1/pair");
});

test("every request to the companion is signed, to the loopback address only, and the secret is never sent again", async () => {
  const { C, calls } = sandbox((url) => (url.endsWith("/v1/pair") ? { status: 200, body: { key_id: "k".repeat(16), secret: SECRET } } : { status: 200, body: { ok: true } }));
  await C.pair("biz", "1234-5678", "b");
  calls.length = 0;
  await C.status("biz");
  await C.infer("biz", "req-0123456789", { system: "s", user: "u", schema: { type: "object" } }, 120);
  await C.cancel("biz", "req-0123456789");
  assert.equal(calls.length, 3);
  for (const c of calls) {
    assert.ok(c.url.startsWith("http://127.0.0.1:47890/v1/"));
    for (const h of ["X-Companion-Key", "X-Companion-Ts", "X-Companion-Nonce", "X-Companion-Sig"]) assert.ok(c.headers[h], h);
    assert.match(c.headers["X-Companion-Sig"], /^[0-9a-f]{64}$/);
    assert.ok(!JSON.stringify(c).includes(SECRET)); // only a signature travels
  }
  assert.notEqual(calls[0].headers["X-Companion-Nonce"], calls[1].headers["X-Companion-Nonce"]);
  assert.deepEqual(JSON.parse(calls[1].body), { request_id: "req-0123456789", system: "s", user: "u", schema: { type: "object" }, timeout: 120 });
  assert.equal(calls[0].headers["Content-Type"], undefined); // a GET has no body and says no type
});

test("each business has its own pairing, and forgetting one leaves the others", async () => {
  const { C } = sandbox((url) => (url.endsWith("/v1/pair") ? { status: 200, body: { key_id: "k".repeat(16), secret: SECRET } } : { status: 200, body: {} }));
  await C.pair("a", "1111-1111", "x");
  await C.pair("b", "2222-2222", "x");
  await C.forget("a");
  assert.equal(await C.paired("a"), false);
  assert.equal(await C.paired("b"), true);
  await assert.rejects(() => C.status("a"), /no paired companion/);
});

test("failures read plainly: no companion, a refused code, a pairing the companion no longer accepts", async () => {
  const down = sandbox(() => { throw new TypeError("fetch failed"); });
  assert.deepEqual(plain(await down.C.hello()), { present: false });
  await assert.rejects(() => down.C.pair("biz", "1234-5678", "b"), /not reachable on this computer/);
  const refuse = sandbox(() => ({ status: 401, body: { error: "not authorised" } }));
  await assert.rejects(() => refuse.C.pair("biz", "0000-0000", "b"), /code was not accepted/);
  const rejecting = sandbox((url) => (url.endsWith("/v1/pair") ? { status: 200, body: { key_id: "k".repeat(16), secret: SECRET } } : { status: 401, body: { error: "not authorised" } }));
  await rejecting.C.pair("biz", "1234-5678", "b");
  await assert.rejects(() => rejecting.C.status("biz"), /no longer accepts this browser/);
});

test("hello learns only that a companion is there", async () => {
  const { C, calls } = sandbox(() => ({ status: 200, body: { companion: true, version: "0.1.0", pairing_open: true } }));
  assert.deepEqual(plain(await C.hello()), { present: true, companion: true, version: "0.1.0", pairing_open: true });
  assert.equal(calls[0].headers, undefined); // nothing identifying is sent
});
