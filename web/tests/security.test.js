// Cross-origin and cross-site request forgery. The API is called by the page with a bearer token in a header, never with a cookie, so a foreign site cannot make a visitor's browser act for them: this
// proves it rather than assuming it. No response allows another origin, a form post is refused before anything is read, a cookie is never a credential, and a preflight is not answered.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { test, beforeEach } from "node:test";
import { POST as respond } from "../api/_routes/respond.js";
import { POST as consentGrant } from "../api/_routes/ai/consent/grant.js";
import { POST as settingsSave } from "../api/_routes/ai/settings/save.js";
import { POST as localComplete } from "../api/_routes/local/complete.js";
import { GET as health } from "../api/_routes/health.js";
import { resetRateLimits } from "../api/_lib/http.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const API = join(HERE, "..", "api");
const SUPABASE = "https://proj.supabase.co";
const ENGINE = "https://engine.internal.example";
const cfg = { supabaseUrl: SUPABASE, anonKey: "anon-public-key", engineUrl: ENGINE, engineSecret: "engine-shared-secret-0123456789abcdef" };
const ALICE = "11111111-1111-4111-8111-111111111111";
const BIZ = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

/** a fake Supabase and engine: one user, owner of one business; the engine answers anything */
function deps() {
  const seen = [];
  const fetchImpl = async (url, init = {}) => {
    const u = String(url);
    seen.push({ url: u, headers: Object.fromEntries(Object.entries(init.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v])) });
    if (u.endsWith("/auth/v1/user")) {
      const auth = seen.at(-1).headers.authorization ?? "";
      return auth === "Bearer token-alice" ? Response.json({ id: ALICE }) : new Response("{}", { status: 401 });
    }
    if (u.includes("/rest/v1/business_members")) return Response.json([{ role: "owner" }]);
    if (u.startsWith(ENGINE)) return Response.json({ ok: true });
    throw new Error(`unexpected call to ${u}`);
  };
  return { seen, deps: { config: cfg, fetchImpl } };
}

const request = (method, { type = "application/json", headers = {}, body = JSON.stringify({ business_id: BIZ, query: "hi" }) } = {}) =>
  new Request("https://app.example/api/x", { method, headers: { ...(type ? { "content-type": type } : {}), ...headers }, ...(method === "GET" || method === "HEAD" ? {} : { body }) });
const ORIGIN = { origin: "https://evil.example" };
const authed = { authorization: "Bearer token-alice" };

beforeEach(() => resetRateLimits());

const CORS = /^access-control-/i;
const corsHeaders = (res) => [...res.headers.keys()].filter((k) => CORS.test(k));

test("no response, success or failure, names another origin or allows credentials", async () => {
  const handlers = [respond, consentGrant, settingsSave, localComplete];
  for (const handler of handlers) {
    const w = deps();
    const cases = [
      request("POST", { headers: { ...authed, ...ORIGIN } }), // a normal call from a foreign origin
      request("POST", { headers: { ...ORIGIN } }), // no sign-in
      request("POST", { headers: { ...authed, ...ORIGIN }, body: "not json" }),
      request("POST", { headers: { authorization: "Bearer token-bob", ...ORIGIN } }), // wrong token
      request("GET", { type: null, headers: { ...authed, ...ORIGIN } }), // wrong method
      request("OPTIONS", { type: null, headers: { ...ORIGIN, "access-control-request-method": "POST", "access-control-request-headers": "authorization,content-type" } }),
    ];
    for (const req of cases) {
      const res = await handler(req, w.deps);
      assert.deepEqual(corsHeaders(res), [], `${req.method} answered with ${corsHeaders(res)}`);
      assert.notEqual(res.headers.get("access-control-allow-origin"), "*");
    }
  }
  assert.deepEqual(corsHeaders(await health(request("GET", { type: null, headers: ORIGIN }))), []);
});

test("a preflight is not answered: only POST is accepted and OPTIONS is a refusal, so a browser never gets permission for a cross-origin call", async () => {
  const w = deps();
  for (const handler of [respond, consentGrant, settingsSave, localComplete]) {
    const res = await handler(request("OPTIONS", { type: null, headers: { ...ORIGIN, "access-control-request-method": "POST" } }), w.deps);
    assert.equal(res.status, 405);
    assert.equal(res.headers.get("allow"), "POST");
  }
  assert.equal(w.seen.length, 0); // not even a sign-in lookup happened
});

test("a request a foreign page can send without a preflight (a form post, plain text, multipart) is refused before anything is read or called", async () => {
  const w = deps();
  for (const type of ["application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x", null]) {
    for (const handler of [respond, consentGrant, settingsSave, localComplete]) {
      const res = await handler(request("POST", { type, headers: { ...authed, ...ORIGIN }, body: `business_id=${BIZ}&query=hi` }), w.deps);
      assert.equal(res.status, 415, `${type}`);
    }
  }
  assert.ok(!w.seen.some((c) => c.url.startsWith(ENGINE)), "nothing reached the engine");
});

test("a cookie is never a credential: a request carrying only cookies is not signed in", async () => {
  const w = deps();
  const res = await respond(request("POST", { headers: { cookie: "sb-access-token=token-alice; session=token-alice", ...ORIGIN } }), w.deps);
  assert.equal(res.status, 401);
  assert.ok(!w.seen.some((c) => c.url.startsWith(ENGINE) || c.url.includes("business_members")));
  // and the token that is accepted comes only from the Authorization header, with the scheme spelled out
  for (const header of ["token-alice", "Basic token-alice", "bearer", "Bearer  "]) {
    assert.equal((await respond(request("POST", { headers: { authorization: header } }), w.deps)).status, 401, header);
  }
});

test("no source file sets a cross-origin header, a wildcard, or reads a cookie", () => {
  const offenders = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (name.endsWith(".js")) {
        const src = readFileSync(full, "utf8");
        if (/access-control-/i.test(src) || /headers\.get\(["']cookie["']\)/i.test(src) || /set-cookie/i.test(src)) offenders.push(relative(API, full));
      }
    }
  };
  walk(API);
  assert.deepEqual(offenders, []);
  const vercel = JSON.parse(readFileSync(join(HERE, "..", "vercel.json"), "utf8"));
  const all = JSON.stringify(vercel.headers);
  assert.ok(!/access-control/i.test(all), "vercel.json adds a cross-origin header");
  const csp = vercel.headers.flatMap((h) => h.headers).find((h) => h.key === "Content-Security-Policy").value;
  assert.match(csp, /frame-ancestors 'none'/);
  assert.match(csp, /default-src 'self'/);
  assert.match(csp, /object-src 'none'/);
  assert.ok(!/script-src[^;]*(unsafe-inline|unsafe-eval|\*)/.test(csp), "scripts may run only from this site");
  assert.ok(!/connect-src[^;]*\s\*(\s|;|$)/.test(csp), "the page may not connect anywhere");
});
