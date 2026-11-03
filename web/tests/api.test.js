// The API layer, with Supabase and the engine service stood in for by a fake `fetch`. Run: npm test
import assert from "node:assert/strict";
import { test, beforeEach } from "node:test";
import { POST as respond } from "../api/respond.js";
import { POST as ingest } from "../api/sources/ingest.js";
import { POST as llmSave } from "../api/llm/save.js";
import { POST as llmGet } from "../api/llm/get.js";
import { POST as llmValidate } from "../api/llm/validate.js";
import { POST as llmRemove } from "../api/llm/remove.js";
import { POST as aiDisclosure } from "../api/ai/disclosure.js";
import { POST as consentGet } from "../api/ai/consent/get.js";
import { POST as consentGrant } from "../api/ai/consent/grant.js";
import { POST as consentRevoke } from "../api/ai/consent/revoke.js";
import { POST as settingsGet } from "../api/ai/settings/get.js";
import { POST as settingsSave } from "../api/ai/settings/save.js";
import { POST as usageGet } from "../api/usage/get.js";
import { POST as history } from "../api/history.js";
import { POST as localPrepare } from "../api/local/prepare.js";
import { POST as localComplete } from "../api/local/complete.js";
import { POST as companionsGet } from "../api/local/companions/get.js";
import { POST as companionRegister } from "../api/local/companions/register.js";
import { POST as companionRevoke } from "../api/local/companions/revoke.js";
import { GET as config } from "../api/config.js";
import { GET as health } from "../api/health.js";
import { readConfig, ConfigError } from "../api/_lib/config.js";
import { resetRateLimits, isUuid } from "../api/_lib/http.js";

const SUPABASE = "https://proj.supabase.co";
const ENGINE = "https://engine.internal.example";
const SECRET = "engine-shared-secret-0123456789abcdef";
const cfg = { supabaseUrl: SUPABASE, anonKey: "anon-public-key", engineUrl: ENGINE, engineSecret: SECRET };
const ALICE = "11111111-1111-4111-8111-111111111111";
const BOB = "22222222-2222-4222-8222-222222222222";
const BIZ_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const BIZ_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const SRC = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

/** tokens -> users; members: "user:business" -> role; engine: a function (path, body, headers) -> [status, json] */
function world({ members = {}, engine = () => [200, { ok: true }], authStatus = null } = {}) {
  const calls = { engine: [], supabase: [] };
  const fetchImpl = async (url, init = {}) => {
    const u = String(url);
    const headers = Object.fromEntries(Object.entries(init.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]));
    if (u.startsWith(SUPABASE)) {
      calls.supabase.push({ url: u, headers });
      if (u.endsWith("/auth/v1/user")) {
        if (authStatus) return new Response("{}", { status: authStatus });
        const user = { "token-alice": ALICE, "token-bob": BOB }[(headers.authorization ?? "").replace("Bearer ", "")];
        return user ? Response.json({ id: user }) : new Response("{}", { status: 401 });
      }
      if (u.includes("/rest/v1/business_members")) {
        const b = /business_id=eq\.([^&]+)/.exec(u)[1];
        const uid = /user_id=eq\.([^&]+)/.exec(u)[1];
        const role = members[`${uid}:${b}`];
        return Response.json(role ? [{ role }] : []);
      }
    }
    if (u.startsWith(ENGINE)) {
      const path = u.slice(ENGINE.length);
      const body = JSON.parse(init.body);
      calls.engine.push({ path, body, headers });
      const [status, data, extra] = engine(path, body, headers);
      return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", ...(extra ?? {}) } });
    }
    throw new Error(`unexpected call to ${u}`);
  };
  return { calls, deps: { config: cfg, fetchImpl } };
}

const post = (body, token = "token-alice", extra = {}) =>
  new Request("https://app.example/api/x", { method: "POST", headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}), ...extra }, body: typeof body === "string" ? body : JSON.stringify(body) });
const asJson = async (res) => ({ status: res.status, body: await res.json(), headers: res.headers });

beforeEach(() => resetRateLimits());

test("a request without a valid sign-in is refused before anything else happens", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "viewer" } });
  assert.equal((await respond(post({ business_id: BIZ_A, query: "hi" }, null), w.deps)).status, 401);
  assert.equal((await respond(post({ business_id: BIZ_A, query: "hi" }, "token-forged"), w.deps)).status, 401);
  assert.equal((await respond(post({ business_id: BIZ_A, query: "hi" }, "not a token!"), w.deps)).status, 401);
  assert.equal(w.calls.engine.length, 0);
  assert.equal((await respond(post({ business_id: BIZ_A, query: "hi" }), world({ authStatus: 500 }).deps)).status, 503); // sign-in being down is not "wrong password"
});

test("a person reaches only the businesses they belong to, and a business id from the browser is never trusted", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "viewer", [`${BOB}:${BIZ_B}`]: "owner" } });
  const mine = await asJson(await respond(post({ business_id: BIZ_A, query: "hours?" }), w.deps));
  assert.equal(mine.status, 200);
  for (const [token, biz] of [["token-alice", BIZ_B], ["token-bob", BIZ_A]]) {
    const r = await asJson(await respond(post({ business_id: biz, query: "hours?" }, token), w.deps));
    assert.equal(r.status, 403);
    assert.match(r.body.error, /access/);
  }
  assert.equal(w.calls.engine.length, 1); // the refused requests never reached the engine
  assert.equal((await respond(post({ business_id: "not-an-id", query: "x" }), w.deps)).status, 400);
  assert.equal((await respond(post({ query: "x" }), w.deps)).status, 400);
  // the membership lookup is made with the PERSON's token, so row level security answers it
  const lookup = w.calls.supabase.find((c) => c.url.includes("business_members"));
  assert.equal(lookup.headers.authorization, "Bearer token-alice");
});

test("each endpoint needs the right role", async () => {
  const members = { [`${ALICE}:${BIZ_A}`]: "viewer", [`${BOB}:${BIZ_A}`]: "editor" };
  const body = { business_id: BIZ_A, source_id: SRC, provider: "openai", model: "m", enabled: false };
  const w = world({ members });
  assert.equal((await ingest(post(body), w.deps)).status, 403); // a viewer cannot add knowledge
  assert.equal((await ingest(post(body, "token-bob"), w.deps)).status, 200); // an editor can
  assert.equal((await llmSave(post(body, "token-bob"), w.deps)).status, 403); // but not manage the model
  assert.equal((await llmValidate(post(body, "token-bob"), w.deps)).status, 403);
  assert.equal((await llmRemove(post({ business_id: BIZ_A }, "token-bob"), w.deps)).status, 403);
  assert.equal((await llmGet(post({ business_id: BIZ_A }), w.deps)).status, 200); // anyone in the business may see the state
  const admin = world({ members: { [`${ALICE}:${BIZ_A}`]: "admin" } });
  assert.equal((await llmSave(post(body), admin.deps)).status, 200);
});

test("the engine is called with the shared secret and the verified user, and only whitelisted fields are forwarded", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "viewer" } });
  await respond(post({ business_id: BIZ_A, query: "hours?", conversation_id: null, user_id: BOB, role: "owner", admin: true, __proto__: { x: 1 } }), w.deps);
  const call = w.calls.engine[0];
  assert.equal(call.path, "/v1/respond");
  assert.equal(call.headers.authorization, `Bearer ${SECRET}`);
  assert.equal(call.body.user_id, ALICE); // never the user id the browser claimed
  assert.equal(call.body.business_id, BIZ_A);
  assert.deepEqual(Object.keys(call.body).sort(), ["business_id", "query", "user_id"]); // a null conversation is simply not sent
  await respond(post({ business_id: BIZ_A, query: "again", conversation_id: SRC, extra: "dropped" }), w.deps);
  assert.deepEqual(Object.keys(w.calls.engine[1].body).sort(), ["business_id", "conversation_id", "query", "user_id"]);
});

test("the shared secret and the engine's address never come back to the browser", async () => {
  const members = { [`${ALICE}:${BIZ_A}`]: "viewer" };
  for (const [status, data] of [[200, { result: { outcome: "faq" } }], [500, { error: `boom at ${ENGINE} with ${SECRET}` }], [401, { error: "unauthorised" }]]) {
    const w = world({ members, engine: () => [status, data] });
    const res = await respond(post({ business_id: BIZ_A, query: "hours?" }), w.deps);
    const text = JSON.stringify([...res.headers.entries()]) + (await res.text());
    assert.ok(!text.includes(SECRET) && !text.includes("engine.internal"), `leaked in a ${status} case`);
  }
});

test("engine failures become plain messages and the right status", async () => {
  const members = { [`${ALICE}:${BIZ_A}`]: "viewer" };
  const run = async (engine) => asJson(await respond(post({ business_id: BIZ_A, query: "q" }), world({ members, engine }).deps));
  assert.equal((await run(() => [500, { error: "traceback ..." }])).status, 502);
  assert.equal((await run(() => [500, { error: "traceback ..." }])).body.error, "The knowledge engine is not available right now.");
  assert.equal((await run(() => [401, { error: "unauthorised" }])).status, 502); // our own secret refused: a setup problem
  const limited = await run(() => [429, { error: "too many questions in a minute" }, { "retry-after": "17" }]);
  assert.equal(limited.status, 429);
  assert.equal(limited.headers.get("retry-after"), "17");
  assert.equal((await run(() => [422, { error: "the source could not be read" }])).status, 422);
  const down = world({ members });
  down.deps.fetchImpl = async (url, init) => { if (String(url).startsWith(ENGINE)) throw new TypeError("fetch failed"); return world({ members }).deps.fetchImpl(url, init); };
  assert.equal((await respond(post({ business_id: BIZ_A, query: "q" }), down.deps)).status, 504);
});

test("input is checked: method, content type, size, shape, and the model fields", async () => {
  const members = { [`${ALICE}:${BIZ_A}`]: "owner" };
  const w = world({ members });
  assert.equal((await respond(new Request("https://app.example/api/x", { method: "GET" }), w.deps)).status, 405);
  assert.equal((await respond(post("not json"), w.deps)).status, 400);
  assert.equal((await respond(post("[1,2]"), w.deps)).status, 400);
  assert.equal((await respond(post({ business_id: BIZ_A, query: "x" }, "token-alice", { "content-type": "text/plain" }), w.deps)).status, 415);
  assert.equal((await respond(post({ business_id: BIZ_A, query: "x".repeat(300_000) }), w.deps)).status, 413);
  assert.equal((await respond(post({ business_id: BIZ_A, query: "x".repeat(1001) }), w.deps)).status, 400);
  for (const bad of [{ provider: "made-up" }, { provider: "openai", model: 5 }, { provider: "openai", enabled: "yes" }, { provider: "openai", capabilities: { polish: "yes" } }, { provider: "openai", api_key: "k".repeat(500) }]) {
    assert.equal((await llmSave(post({ business_id: BIZ_A, ...bad }), w.deps)).status, 400, JSON.stringify(bad));
  }
  assert.equal(w.calls.engine.length, 0);
});

test("a flood from one person is slowed down", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "owner" } });
  const codes = [];
  for (let i = 0; i < 12; i++) codes.push((await llmSave(post({ business_id: BIZ_A, provider: "openai", model: "m" }), w.deps)).status);
  assert.equal(codes.filter((c) => c === 429).length, 2); // 10 a minute
  const last = await llmSave(post({ business_id: BIZ_A, provider: "openai", model: "m" }), w.deps);
  assert.ok(Number(last.headers.get("retry-after")) >= 1);
});

test("an unconfigured deployment says so without naming what is missing", async () => {
  const saved = { ...process.env };
  for (const k of ["SUPABASE_URL", "SUPABASE_ANON_KEY", "ENGINE_URL", "ENGINE_SERVICE_SECRET"]) delete process.env[k];
  const logged = [];
  const original = console.error;
  console.error = (...a) => logged.push(a.join(" "));
  try {
    const res = await asJson(await respond(post({ business_id: BIZ_A, query: "q" })));
    assert.equal(res.status, 500);
    assert.equal(res.body.error, "Something went wrong on the server.");
    assert.ok(logged.join(" ").includes("ENGINE_SERVICE_SECRET")); // the operator sees which settings are missing, in the log
    assert.throws(() => readConfig({}), ConfigError);
    assert.equal((await asJson(config())).status, 503);
  } finally {
    console.error = original;
    Object.assign(process.env, saved);
  }
});

test("the page is given the public settings and nothing else", async () => {
  const saved = { ...process.env };
  Object.assign(process.env, { SUPABASE_URL: SUPABASE, SUPABASE_ANON_KEY: "anon-public-key", ENGINE_URL: ENGINE, ENGINE_SERVICE_SECRET: SECRET, SUPABASE_SERVICE_ROLE_KEY: "service-role-secret" });
  try {
    const res = await asJson(config());
    assert.deepEqual(Object.keys(res.body).sort(), ["anonKey", "supabaseUrl"]);
    assert.ok(!JSON.stringify(res.body).includes("secret") && !JSON.stringify(res.body).includes("engine"));
    assert.equal((await asJson(health())).body.ok, true);
  } finally {
    for (const k of ["SUPABASE_URL", "SUPABASE_ANON_KEY", "ENGINE_URL", "ENGINE_SERVICE_SECRET", "SUPABASE_SERVICE_ROLE_KEY"]) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
  }
});

test("uuid check", () => {
  assert.ok(isUuid(BIZ_A));
  for (const v of ["", "abc", null, 5, `${BIZ_A}x`, "'; drop table--"]) assert.ok(!isUuid(v));
});

// ---------- consent to send data to an AI provider
const VERSION = "2026-11-02.1a2b3c4d";

test("consent routes need a sign-in and the right role: any member reads, only an owner or admin agrees or withdraws", async () => {
  const members = { [`${ALICE}:${BIZ_A}`]: "viewer", [`${BOB}:${BIZ_A}`]: "editor" };
  const w = world({ members });
  const agree = { business_id: BIZ_A, provider: "cloudflare", disclosure_version: VERSION };
  assert.equal((await consentGet(post({ business_id: BIZ_A }, null), w.deps)).status, 401);
  assert.equal((await consentGet(post({ business_id: BIZ_A }), w.deps)).status, 200); // a viewer can see where the business stands
  assert.equal((await aiDisclosure(post({ business_id: BIZ_A, provider: "cloudflare" }), w.deps)).status, 200); // and read the text
  for (const [route, body] of [[consentGrant, agree], [consentRevoke, { business_id: BIZ_A, provider: "cloudflare" }]]) {
    assert.equal((await route(post(body), w.deps)).status, 403); // a viewer
    assert.equal((await route(post(body, "token-bob"), w.deps)).status, 403); // an editor
  }
  assert.equal(w.calls.engine.filter((c) => c.path.includes("grant") || c.path.includes("revoke")).length, 0); // refused before the engine was asked
  const admin = world({ members: { [`${ALICE}:${BIZ_A}`]: "admin" } });
  assert.equal((await consentGrant(post(agree), admin.deps)).status, 200);
  assert.equal((await consentRevoke(post({ business_id: BIZ_A, provider: "cloudflare" }), admin.deps)).status, 200);
});

test("another business's consent is out of reach: the business is the person's own, checked with their token", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "owner", [`${BOB}:${BIZ_B}`]: "owner" } });
  for (const route of [consentGet, aiDisclosure, consentGrant, consentRevoke]) {
    const r = await asJson(await route(post({ business_id: BIZ_B, provider: "cloudflare", disclosure_version: VERSION }), w.deps)); // Alice, asking about Bob's business
    assert.equal(r.status, 403);
  }
  assert.equal(w.calls.engine.length, 0);
  assert.equal((await consentGrant(post({ provider: "cloudflare", disclosure_version: VERSION }), w.deps)).status, 400); // no business named: nothing is guessed
});

test("the engine is told who agreed from the sign-in, never from the page, and nothing in the body can claim consent", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "owner" } });
  await consentGrant(post({ business_id: BIZ_A, provider: "cloudflare", disclosure_version: VERSION, user_id: BOB, granted_by: BOB, agreed: true, consent: true, consented: true, status: "granted", role: "owner",
                           data_categories: ["everything"], disclosure_sha256: "f".repeat(64), terms_verified: true, verified: true }), w.deps);
  const call = w.calls.engine[0];
  assert.equal(call.path, "/v1/ai/consent/grant");
  assert.equal(call.body.user_id, ALICE);
  assert.deepEqual(Object.keys(call.body).sort(), ["business_id", "disclosure_version", "provider", "user_id"]);
  assert.equal(call.headers.authorization, `Bearer ${SECRET}`);
  await consentRevoke(post({ business_id: BIZ_A, provider: "cloudflare", user_id: BOB, revoked_by: BOB, status: "revoked" }), w.deps);
  assert.deepEqual(Object.keys(w.calls.engine[1].body).sort(), ["business_id", "provider", "user_id"]);
  await consentGet(post({ business_id: BIZ_A, provider: "gemini", agreed: true }), w.deps);
  assert.deepEqual(Object.keys(w.calls.engine[2].body).sort(), ["business_id", "user_id"]);
  await aiDisclosure(post({ business_id: BIZ_A, provider: "gemini", disclosure_version: "x", text: "forged" }), w.deps);
  assert.deepEqual(Object.keys(w.calls.engine[3].body).sort(), ["business_id", "provider", "user_id"]);
});

test("a provider and a disclosure version are checked for shape before the engine is asked", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "owner" } });
  const grant = (extra) => { resetRateLimits(); return consentGrant(post({ business_id: BIZ_A, provider: "cloudflare", disclosure_version: VERSION, ...extra }), w.deps); }; // (not the rate limit under test here)
  for (const bad of [{ provider: "" }, { provider: "Cloud Flare" }, { provider: "https://evil.example" }, { provider: "../etc" }, { provider: 5 }, { provider: "a".repeat(70) }, { provider: undefined },
                     { disclosure_version: "" }, { disclosure_version: "v 1" }, { disclosure_version: "x".repeat(81) }, { disclosure_version: 3 }, { disclosure_version: "1; drop table" }, { disclosure_version: undefined }]) {
    assert.equal((await grant(bad)).status, 400, JSON.stringify(bad));
  }
  assert.equal((await consentRevoke(post({ business_id: BIZ_A, provider: "Not A Key" }), w.deps)).status, 400);
  assert.equal((await aiDisclosure(post({ business_id: BIZ_A }), w.deps)).status, 400);
  assert.equal(w.calls.engine.length, 0);
});

test("the engine's reasons for refusing an agreement reach the person in plain words, and nothing else of it does", async () => {
  const members = { [`${ALICE}:${BIZ_A}`]: "owner" };
  const body = { business_id: BIZ_A, provider: "cloudflare", disclosure_version: VERSION };
  const unverified = await asJson(await consentGrant(post(body), world({ members, engine: () => [409, { error: "this provider's terms have not been verified, so it cannot be agreed to yet" }] }).deps));
  assert.equal(unverified.status, 409);
  assert.match(unverified.body.error, /not been verified/);
  const changed = await asJson(await consentGrant(post(body), world({ members, engine: () => [409, { error: "the disclosure text has changed or was not the one shown; read the current text and agree to that" }] }).deps));
  assert.equal(changed.status, 409);
  const leaky = await consentGrant(post(body), world({ members, engine: () => [500, { error: `trace at ${ENGINE} with ${SECRET}` }] }).deps);
  const text = JSON.stringify([...leaky.headers.entries()]) + (await leaky.text());
  assert.equal(leaky.status, 502);
  assert.ok(!text.includes(SECRET) && !text.includes("engine.internal"));
});

test("agreeing and withdrawing are slowed down per person", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "owner" } });
  const codes = [];
  for (let i = 0; i < 12; i++) codes.push((await consentGrant(post({ business_id: BIZ_A, provider: "cloudflare", disclosure_version: VERSION }), w.deps)).status);
  assert.equal(codes.filter((c) => c === 429).length, 2);
});

// ---------- how a business answers: mode settings, usage, history, and the local two-phase flow
const RID = "a".repeat(48);
const COMP = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";

test("who may read and who may change how a business answers, and what usage and history need", async () => {
  const members = { [`${ALICE}:${BIZ_A}`]: "viewer", [`${BOB}:${BIZ_A}`]: "admin" };
  const w = world({ members });
  for (const [name, route, body] of [["settings", settingsGet, {}], ["history", history, {}], ["companions", companionsGet, {}], ["prepare", localPrepare, { query: "hours?" }]]) {
    assert.equal((await route(post({ business_id: BIZ_A, ...body }), w.deps)).status, 200, name); // any member
    assert.equal((await route(post({ business_id: BIZ_A, ...body }, null), w.deps)).status, 401, name);
  }
  for (const [name, route, body] of [["save", settingsSave, { mode: "deterministic" }], ["usage", usageGet, {}], ["register", companionRegister, { label: "laptop", runtime: "ollama", model: "qwen3:4b" }], ["revoke", companionRevoke, { companion_id: COMP }]]) {
    assert.equal((await route(post({ business_id: BIZ_A, ...body }), w.deps)).status, 403, name); // a viewer
    assert.equal((await route(post({ business_id: BIZ_A, ...body }, "token-bob"), w.deps)).status, 200, name); // an admin
  }
  const other = world({ members: { [`${BOB}:${BIZ_B}`]: "owner" } });
  for (const route of [settingsGet, settingsSave, usageGet, history, localPrepare, companionsGet, companionRegister, companionRevoke]) {
    assert.equal((await route(post({ business_id: BIZ_A, mode: "deterministic", query: "x", label: "x", runtime: "ollama", model: "m", companion_id: COMP }, "token-bob"), other.deps)).status, 403); // another business
  }
  assert.equal(other.calls.engine.length, 0);
});

test("only whitelisted, well-formed fields reach the engine for the mode and local functions, and the user is always the verified one", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "owner" } });
  const forged = { user_id: BOB, role: "owner", admin: true, provider: "cloudflare", consented: true, mode_override: "platform", granted_by: BOB };
  await settingsSave(post({ business_id: BIZ_A, mode: "platform", fallback: "none", platform_provider: "openrouter", platform_model: "qwen/qwen3.5-9b", ...forged }), w.deps);
  await localPrepare(post({ business_id: BIZ_A, query: "hours?", conversation_id: SRC, ...forged }), w.deps);
  await localComplete(post({ business_id: BIZ_A, request_id: RID, text: "{}", tokens_in: 5, tokens_out: 2, ...forged }), w.deps);
  await companionRegister(post({ business_id: BIZ_A, label: " my laptop ", platform: "macOS arm64", runtime: "ollama", model: "qwen3:4b", secret: "do-not-forward", ...forged }), w.deps);
  await history(post({ business_id: BIZ_A, limit: 20, before: "2026-11-02T10:00:00+00:00", ...forged }), w.deps);
  const sent = w.calls.engine.map((c) => [c.path, Object.keys(c.body).sort()]);
  assert.deepEqual(sent, [
    ["/v1/ai/settings/save", ["business_id", "fallback", "mode", "platform_model", "platform_provider", "user_id"]],
    ["/v1/local/prepare", ["business_id", "conversation_id", "query", "user_id"]],
    ["/v1/local/complete", ["business_id", "request_id", "text", "tokens_in", "tokens_out", "user_id"]],
    ["/v1/local/companions/register", ["business_id", "label", "model", "platform", "runtime", "user_id"]],
    ["/v1/history", ["before", "business_id", "limit", "user_id"]],
  ]);
  assert.ok(w.calls.engine.every((c) => c.body.user_id === ALICE)); // never the user the browser named
  assert.equal(w.calls.engine[3].body.label, "my laptop");
});

test("malformed input to the mode and local functions is refused before the engine is asked", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "owner" } });
  const cases = [
    [settingsSave, [{}, { mode: "cloud" }, { mode: 5 }, { mode: "platform", fallback: "another" }, { mode: "platform", platform_provider: "Not A Key" }, { mode: "platform", platform_model: "x".repeat(300) }, { mode: "platform", platform_model: "a b" }]],
    [history, [{ limit: 0 }, { limit: 201 }, { limit: "20" }, { limit: 1.5 }, { before: "tomorrow" }, { before: "2026-11-02; drop" }, { before: "x".repeat(41) }]],
    [localPrepare, [{}, { query: "" }, { query: "x".repeat(1001) }, { query: 5 }, { query: "ok", conversation_id: 5 }]],
    [localComplete, [{}, { request_id: "short" }, { request_id: "g".repeat(48) }, { request_id: RID.toUpperCase() }, { request_id: RID, text: 5 }, { request_id: RID, text: "x".repeat(150_001) }, { request_id: RID, failure: "oops" },
                     { request_id: RID, tokens_in: -1 }, { request_id: RID, tokens_out: 1e9 }, { request_id: RID, tokens_in: 1.5 }]],
    [companionRegister, [{}, { label: "" }, { label: "x".repeat(81), runtime: "ollama", model: "m" }, { label: "tab\there", runtime: "ollama", model: "m" }, { label: "ok", runtime: "lmstudio", model: "m" }, { label: "ok", runtime: "ollama", model: "bad name; rm" },
                         { label: "ok", runtime: "ollama", model: "m", platform: "p".repeat(81) }]],
    [companionRevoke, [{}, { companion_id: "x" }, { companion_id: 5 }]],
  ];
  for (const [route, bodies] of cases) {
    for (const bad of bodies) {
      resetRateLimits();
      assert.equal((await route(post({ business_id: BIZ_A, ...bad }), w.deps)).status, 400, JSON.stringify(bad));
    }
  }
  assert.equal(w.calls.engine.length, 0);
});

test("the engine's refusals for mode and local requests reach the person in plain words, with the right status", async () => {
  const members = { [`${ALICE}:${BIZ_A}`]: "owner" };
  const run = async (route, body, engine) => asJson(await route(post({ business_id: BIZ_A, ...body }), world({ members, engine }).deps));
  assert.equal((await run(settingsSave, { mode: "local" }, () => [409, { error: "pair a companion first" }])).status, 409);
  assert.match((await run(settingsSave, { mode: "platform" }, () => [409, { error: "that model is not offered" }])).body.error, /not offered/);
  assert.equal((await run(localComplete, { request_id: RID }, () => [410, { error: "this request has expired or was already answered: ask again" }])).status, 410);
  assert.equal((await run(localPrepare, { query: "hours?" }, () => [429, { error: "too many local questions are waiting for their answers" }, { "retry-after": "10" }])).status, 429);
  const leaky = await run(localPrepare, { query: "hours?" }, () => [500, { error: `trace ${ENGINE} ${SECRET}` }]);
  assert.equal(leaky.status, 502);
  assert.ok(!JSON.stringify(leaky).includes(SECRET) && !JSON.stringify(leaky).includes("engine.internal"));
});

test("every provider the product supports for a business's own key can be saved, and nothing else", async () => {
  const w = world({ members: { [`${ALICE}:${BIZ_A}`]: "admin" } });
  for (const provider of ["openai", "anthropic", "gemini", "openrouter", "openai_compatible"]) {
    resetRateLimits();
    assert.equal((await llmSave(post({ business_id: BIZ_A, provider, model: "m", enabled: false }), w.deps)).status, 200, provider);
  }
  for (const provider of ["cloudflare", "ollama", "llamacpp", "OpenAI", "", "../openai"]) {
    resetRateLimits();
    assert.equal((await llmSave(post({ business_id: BIZ_A, provider, model: "m", enabled: false }), w.deps)).status, 400, provider); // platform-only and local providers are not a business's own key
  }
});
