// A local preview of the whole web app with nothing real behind it: the static page, the REAL /api functions, and stand-ins for Supabase (auth, tables, storage) and for the engine
// service. For looking at the screens and for checking that the page's script and the API functions work together. Not a test of Supabase or of the engine.
//   node scripts/preview.mjs [port]      then open http://127.0.0.1:8768
import { createServer } from "node:http";
import { readFileSync, existsSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { extname, join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const port = Number(process.argv[2] ?? 8768);
const base = `http://127.0.0.1:${port}`;
Object.assign(process.env, { SUPABASE_URL: `${base}/supa`, SUPABASE_ANON_KEY: "preview-anon-key", ENGINE_URL: `${base}/engine`, ENGINE_SERVICE_SECRET: "preview-engine-secret-0123456789" });

const routes = { "/api/config": "config", "/api/health": "health", "/api/respond": "respond", "/api/sources/ingest": "sources/ingest", "/api/llm/get": "llm/get", "/api/llm/validate": "llm/validate", "/api/llm/save": "llm/save", "/api/llm/remove": "llm/remove",
  "/api/ai/disclosure": "ai/disclosure", "/api/ai/consent/get": "ai/consent/get", "/api/ai/consent/grant": "ai/consent/grant", "/api/ai/consent/revoke": "ai/consent/revoke",
  "/api/ai/settings/get": "ai/settings/get", "/api/ai/settings/save": "ai/settings/save", "/api/usage/get": "usage/get", "/api/history": "history", "/api/train/start": "train/start", "/api/train/status": "train/status", "/api/local/prepare": "local/prepare", "/api/local/complete": "local/complete",
  "/api/local/companions/get": "local/companions/get", "/api/local/companions/register": "local/companions/register", "/api/local/companions/revoke": "local/companions/revoke" };
const ENGINE = process.env.KNOVENT_ENGINE, SECRET = process.env.KNOVENT_SECRET, DATA = process.env.KNOVENT_DATA;  // set by `faqengine app`: then a REAL engine answers and everything is kept on disk
const db = { users: new Map(), members: [], businesses: [], sources: [], llm: new Map(), consents: new Map(), ai: new Map(), history: [], invitations: [], companions: new Map() };
const MAPS = ["users", "llm", "consents", "ai", "companions"];
const dbFile = DATA ? join(DATA, "site.json") : null;
if (dbFile && existsSync(dbFile)) { try { const saved = JSON.parse(readFileSync(dbFile, "utf8")); for (const k of Object.keys(db)) if (saved[k]) db[k] = MAPS.includes(k) ? new Map(saved[k]) : saved[k]; } catch { /* a damaged file starts empty */ } }
let saveTimer = null;
const persist = () => { if (!dbFile || saveTimer) return; saveTimer = setTimeout(() => { saveTimer = null; try { mkdirSync(DATA, { recursive: true }); writeFileSync(dbFile, JSON.stringify(Object.fromEntries(Object.entries(db).map(([k, v]) => [k, MAPS.includes(k) ? [...v] : v])))); } catch { /* disk full or read-only: the site keeps working from memory */ } }, 300); };
const py = async (path, payload, ms = 300_000) => {
  const r = await fetch(ENGINE + path, { method: "POST", headers: { "content-type": "application/json", "x-knovent-secret": SECRET }, body: JSON.stringify(payload), signal: AbortSignal.timeout(ms) });
  const out = await r.json().catch(() => ({}));
  return { status: r.status, out };
};
const localModel = (biz) => { const a = db.ai.get(biz); return a && a.mode === "local" ? ((db.companions.get(biz) ?? []).filter((c) => !c.revoked).at(-1)?.model ?? null) : null; };  // the model chosen last
const SECURITY_HEADERS = Object.fromEntries((JSON.parse(readFileSync(join(root, "vercel.json"), "utf8")).headers.find((h) => h.source === "/(.*)")?.headers ?? []).map((h) => [h.key.toLowerCase(), h.value]));
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
const json = (res, status, body) => { res.writeHead(status, { "content-type": "application/json" }); res.end(JSON.stringify(body)); };
const body = async (req) => { const c = []; for await (const x of req) c.push(x); const t = Buffer.concat(c).toString(); try { return t ? JSON.parse(t) : {}; } catch { return {}; } };
const userFrom = (req) => { const t = (req.headers.authorization ?? "").replace("Bearer ", ""); return db.users.get(t.replace("mock-token-", "")) ?? null; };

async function supabase(req, res, url) {
  const p = url.pathname.slice("/supa".length);
  if (p.startsWith("/auth/v1/token") || p.startsWith("/auth/v1/signup")) {
    const b = await body(req);
    const id = [...db.users.values()].find((u) => u.email === b.email)?.id ?? randomUUID();
    const user = { id, email: b.email ?? "someone@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {}, created_at: new Date().toISOString() };
    db.users.set(id, user);
    return json(res, 200, { access_token: "mock-token-" + id, token_type: "bearer", expires_in: 3600, expires_at: Math.floor(Date.now() / 1000) + 3600, refresh_token: "r-" + id, user });
  }
  if (p === "/auth/v1/user") { const u = userFrom(req); return u ? json(res, 200, u) : json(res, 401, {}); }
  const u = userFrom(req);
  if (!u) return json(res, 401, { message: "no" });
  if (p === "/rest/v1/business_invitations" && req.method === "GET") return json(res, 200, db.invitations.filter((i) => i.business_id === url.searchParams.get("business_id").replace("eq.", "")));
  if (p === "/rest/v1/rpc/create_invitation") { const b = await body(req); const token = randomUUID().replace(/-/g, "") + randomUUID().replace(/-/g, ""); db.invitations.unshift({ id: randomUUID(), business_id: b.p_business, email: String(b.p_email).trim().toLowerCase(), role: b.p_role, created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 7 * 864e5).toISOString() }); return json(res, 200, token); }
  if (p === "/rest/v1/rpc/revoke_invitation") { const b = await body(req); const i = db.invitations.find((x) => x.id === b.p_invitation); if (i) i.revoked_at = new Date().toISOString(); return json(res, 200, null); }
  if (p === "/rest/v1/business_members" && req.method === "GET" && url.searchParams.get("select")?.includes("profiles")) {
    return json(res, 200, db.members.filter((m) => m.business_id === url.searchParams.get("business_id").replace("eq.", "")).map((m) => ({ user_id: m.user_id, role: m.role, created_at: new Date().toISOString(), profiles: { display_name: (db.users.get(m.user_id) ?? {}).email?.split("@")[0] ?? "member" } })));
  }
  if (p === "/rest/v1/business_members" && req.method === "GET") {
    const q = url.searchParams;
    let rows = db.members.filter((m) => m.user_id === u.id);
    if (q.get("business_id")) rows = rows.filter((m) => m.business_id === q.get("business_id").replace("eq.", ""));
    return json(res, 200, q.get("select")?.includes("businesses") ? rows.map((m) => ({ role: m.role, businesses: db.businesses.find((b) => b.id === m.business_id) })) : rows.map((m) => ({ role: m.role })));
  }
  if (p === "/rest/v1/rpc/create_business") { const b = await body(req); const row = { id: randomUUID(), name: b.p_name, slug: b.p_slug, answer_mode: "local_only", onboarding_completed_at: null }; db.businesses.push(row); db.members.push({ business_id: row.id, user_id: u.id, role: "owner" }); return json(res, 200, row); }
  if (p === "/rest/v1/knowledge_sources") {
    if (req.method === "GET") return json(res, 200, db.sources.filter((s) => s.business_id === url.searchParams.get("business_id").replace("eq.", "")));
    if (req.method === "POST") { const b = await body(req); db.sources.push({ status: "pending", parse_status: "pending", version: 1, entry_count: 0, chunk_count: 0, error: null, updated_at: new Date().toISOString(), ...b }); res.writeHead(201); return res.end(); }
    if (req.method === "DELETE") { const id = url.searchParams.get("id").replace("eq.", ""); const gone = db.sources.find((s) => s.id === id); if (ENGINE && gone) { await py("/remove", { business_id: gone.business_id, source_id: id }).catch(() => null); if (gone.storage_path && !gone.storage_path.includes("..")) rmSync(join(DATA, "files", gone.storage_path), { force: true }); } db.sources = db.sources.filter((s) => s.id !== id); res.writeHead(204); return res.end(); }
  }
  if (p === "/rest/v1/businesses" && req.method === "PATCH") { const b = await body(req); const row = db.businesses.find((x) => x.id === url.searchParams.get("id").replace("eq.", "")); Object.assign(row, b); res.writeHead(204); return res.end(); }
  if (p.startsWith("/storage/v1/object/")) {
    const rel = decodeURIComponent(p.slice("/storage/v1/object/".length)).split("/").slice(1).join("/");  // (without the bucket name: the source row keeps the path inside the bucket)
    if (DATA && req.method !== "GET") {
      const chunks = []; for await (const x of req) chunks.push(x);
      try {
        const fd = await new Request(url, { method: "POST", headers: req.headers, body: Buffer.concat(chunks), duplex: "half" }).formData();
        const file = [...fd.values()].find((v) => typeof v !== "string");
        if (file && !rel.includes("..")) { const dest = join(DATA, "files", rel); mkdirSync(dirname(dest), { recursive: true }); writeFileSync(dest, Buffer.from(await file.arrayBuffer())); }
      } catch (e) { return json(res, 400, { message: "the file could not be received" }); }
      return json(res, 200, { Key: rel });
    }
    await body(req); return json(res, 200, { Key: p });
  }
  return json(res, 404, { message: "preview has no such endpoint: " + req.method + " " + p });
}

// A stand-in for the engine's consent routes, with one provider whose terms count as verified (a PREVIEW provider, not a real one) and two that do not: so that both can be looked at.
const PREVIEW = {
  demo: { label: "Demo provider (preview only: not a real provider)", verified: true, text: "Sending your data to Demo provider (preview only)\n\nWhat may be sent:\n  - the question a customer asks\n  - the passages of your own knowledge that were retrieved for that question\n\nThis text is a preview stand-in. A real provider's text comes from the engine service and says what was verified, where and when." },
  cloudflare: { label: "Cloudflare Workers AI", verified: false, text: "This service has NOT verified this provider's terms.\nAgreement is not available for this provider until its terms have been verified." },
  gemini: { label: "Google Gemini API (your own key)", verified: false, text: "This service has NOT verified this provider's terms.\nAgreement is not available for this provider until its terms have been verified." },
};
const versionOf = (k) => "preview." + PREVIEW[k].text.length;
const consentState = (biz) => {
  const mine = db.consents.get(biz) ?? [];
  return { providers: Object.entries(PREVIEW).map(([k, v]) => { const h = mine.filter((x) => x.provider === k); const agreed = h.some((x) => x.status === "granted" && x.disclosure_version === versionOf(k)); return { provider: k, label: v.label, disclosure_version: versionOf(k), terms_verified: v.verified, consentable: v.verified, unverified: v.verified ? [] : ["retention", "training_use"], agreed, in_use: agreed && v.verified, history: h }; }),
    external_inference: { connected: false } };
};
function consent(path, b, res) {
  const k = b.provider, mine = db.consents.get(b.business_id) ?? [];
  if (path === "/v1/ai/consent/get") return json(res, 200, consentState(b.business_id));
  if (!PREVIEW[k]) return json(res, 400, { error: "choose a provider that has a disclosure" });
  if (path === "/v1/ai/disclosure") return json(res, 200, { provider: k, label: PREVIEW[k].label, version: versionOf(k), sha256: "0".repeat(64), text: PREVIEW[k].text, verified: PREVIEW[k].verified, consentable: PREVIEW[k].verified, data_categories: ["question", "evidence_passages"] });
  if (path === "/v1/ai/consent/grant") {
    if (!PREVIEW[k].verified) return json(res, 409, { error: "this provider's terms have not been verified, so it cannot be agreed to yet" });
    if (b.disclosure_version !== versionOf(k)) return json(res, 409, { error: "the disclosure text has changed or was not the one shown; read the current text and agree to that" });
    for (const x of mine) if (x.provider === k && x.status === "granted") Object.assign(x, { status: "revoked", revoked_at: new Date().toISOString(), revoked_by: b.user_id });
    mine.push({ id: randomUUID(), provider: k, status: "granted", disclosure_version: b.disclosure_version, data_categories: ["question", "evidence_passages"], granted_by: b.user_id, granted_at: new Date().toISOString(), revoked_by: null, revoked_at: null });
    db.consents.set(b.business_id, mine);
    return json(res, 200, consentState(b.business_id));
  }
  if (path === "/v1/ai/consent/revoke") { for (const x of mine) if (x.provider === k && x.status === "granted") Object.assign(x, { status: "revoked", revoked_at: new Date().toISOString(), revoked_by: b.user_id }); return json(res, 200, consentState(b.business_id)); }
  return json(res, 404, { error: "not found" });
}

async function engine(req, res, url) {
  const b = await body(req);
  const path = url.pathname.slice("/engine".length);
  if (ENGINE && (path === "/v1/train/start" || path === "/v1/train/status")) {
    try { const r = await py(path.slice(3), { business_id: b.business_id, name: db.businesses.find((x) => x.id === b.business_id)?.name }, 30_000); return json(res, r.status, r.out); } catch { return json(res, 502, { error: "the engine did not answer" }); }
  }
  if (!ENGINE && (path === "/v1/train/start" || path === "/v1/train/status")) return json(res, 409, { error: "training needs the real engine: start Knovent with faqengine app" });
  if (ENGINE && path === "/v1/sources/ingest") {
    const s = db.sources.find((x) => x.id === b.source_id);
    if (!s) return json(res, 404, { error: "no such source" });
    if (!s.storage_path && s.url) {
      try {
        const r = await py("/ingest_url", { business_id: s.business_id, source_id: s.id, url: s.url, whole: s.source_type === "website" }, 120_000);
        if (r.status !== 200) { Object.assign(s, { status: "failed", parse_status: "failed", error: r.out.error ?? "the page could not be read" }); return json(res, r.status, r.out); }
        Object.assign(s, { status: "ready", parse_status: r.out.warnings?.length ? "partial" : "parsed", entry_count: r.out.faqs, chunk_count: r.out.chunks, error: null, updated_at: new Date().toISOString() });
        return json(res, 200, r.out);
      } catch (e) { Object.assign(s, { status: "failed", parse_status: "failed", error: "the engine did not answer" }); return json(res, 502, { error: "the engine did not answer" }); }
    }
    if (!s.storage_path) { Object.assign(s, { status: "failed", parse_status: "failed", error: "this source has no file or address" }); return json(res, 422, { error: s.error }); }
    try {
      const file = join(DATA, "files", s.storage_path);
      const r = await py("/ingest", { business_id: s.business_id, source_id: s.id, filename: s.name, data_b64: readFileSync(file).toString("base64") });
      if (r.status !== 200) { Object.assign(s, { status: "failed", parse_status: "failed", error: r.out.error ?? "the file could not be read" }); return json(res, r.status, r.out); }
      Object.assign(s, { status: "ready", parse_status: r.out.warnings?.length ? "partial" : "parsed", entry_count: r.out.faqs, chunk_count: r.out.chunks, error: null, updated_at: new Date().toISOString() });
      return json(res, 200, r.out);
    } catch (e) { Object.assign(s, { status: "failed", parse_status: "failed", error: "the engine did not answer" }); return json(res, 502, { error: "the engine did not answer" }); }
  }
  if (ENGINE && (path === "/v1/respond" || path === "/v1/local/prepare")) {
    const t0 = Date.now();
    try {
      const r = await py("/respond", { business_id: b.business_id, query: String(b.query ?? ""), model: localModel(b.business_id), style: (db.ai.get(b.business_id)?.answer_style_set ? db.ai.get(b.business_id).answer_style : null) ?? (localModel(b.business_id) ? "chat" : "plain") });
      if (r.status !== 200) return json(res, r.status, r.out);
      const result = r.out;
      db.history.unshift({ id: randomUUID(), business_id: b.business_id, at: new Date().toISOString(), outcome: result.outcome, latency_ms: Date.now() - t0, question: String(b.query ?? ""), execution: result.execution });
      persist();
      return path === "/v1/respond" ? json(res, 200, { result, conversation_id: randomUUID(), message_id: randomUUID() }) : json(res, 200, { done: true, result, conversation_id: randomUUID(), message_id: randomUUID() });
    } catch (e) { return json(res, 502, { error: "the engine did not answer" }); }
  }
  if (path === "/v1/sources/ingest") { const s = db.sources.find((x) => x.id === b.source_id); if (s) Object.assign(s, { status: "ready", parse_status: "parsed", entry_count: 3 }); return json(res, 200, { unchanged: false, faqs: 3, chunks: 0, version: 1 }); }
  if (path === "/v1/respond") {
    const q = String(b.query ?? "").toLowerCase();
    const known = q.includes("hour") || q.includes("open");
    const result = known
      ? { outcome: "faq", answer: "We are open Monday to Saturday, 9am to 6pm.", sources: [{ ref: 1, kind: "faq", id: "hours", source: "faq.csv", location: "What are your opening hours?" }], evidence_class: "direct", retrieval_similarity: 0.93, answer_sufficiency: 1, evidence_support: 1, used_llm: false, llm: { enabled: false, used: false }, verification: null, fallback: null,
          retrieval: { diagnostics: { method: "hybrid", alpha: 0.3, embed_ms: 0.8, search_ms: 0.4, entries: 3 }, candidates: [{ entry_id: "hours", question: "What are your opening hours?", kind: "faq", similarity_score: 0.93, keyword_score: 3.1, combined_score: 1 }], quarantined: [] } }
      : { outcome: "insufficient", answer: null, message: "The information I have does not answer this. Nothing available covers: " + b.query + ".", unanswered_parts: [String(b.query)], evidence_class: "no_evidence", retrieval_similarity: 0.21, answer_sufficiency: 0, evidence_support: 0, llm: { enabled: false, used: false }, retrieval: { diagnostics: { method: "hybrid", alpha: 0.3, entries: 3 }, candidates: [], quarantined: [] } };
    result.execution = { path: "legacy", mode: "deterministic", provider: null, model: null, generated_by: "deterministic", failure_kind: null, fallback: false, inference_attempted: false };
    db.history.unshift({ id: randomUUID(), at: new Date().toISOString(), outcome: result.outcome, latency_ms: 42, question: String(b.query ?? ""), execution: result.execution });
    return json(res, 200, { result, conversation_id: randomUUID(), message_id: randomUUID() });
  }
  if (path === "/v1/ai/settings/get" || path === "/v1/ai/settings/save") {
    const POL = { deterministic_only: { mode: "deterministic" }, local_only: { mode: "local" }, local_free: { mode: "local", fallback_free: true }, local_free_paid: { mode: "local", fallback_free: true, fallback_paid: true }, byok_alternative: { mode: "local", fallback_byok: true }, byok_preferred: { mode: "byok" } };
    const cur = db.ai.get(b.business_id) ?? { mode: "deterministic", platform_provider: null, platform_model: null, fallback: "deterministic", fallback_byok: false, fallback_free: false, fallback_paid: false, paid_cap_usd: null };
    const comps = (db.companions.get(b.business_id) ?? []).filter((c) => !c.revoked), llm = db.llm.get(b.business_id), agreed = (db.consents.get(b.business_id) ?? []).some((x) => x.status === "granted");
    if (path.endsWith("save")) {
      const pre = POL[b.policy]; if (!pre) return json(res, 400, { error: "choose one of: " + Object.keys(POL).join(", ") });
      const next = { ...cur, fallback_byok: false, fallback_free: false, fallback_paid: false, ...pre, fallback: b.fallback ?? cur.fallback, paid_cap_usd: b.paid_cap_usd ?? cur.paid_cap_usd, answer_style: b.answer_style ?? (cur.answer_style_set ? cur.answer_style : undefined), answer_style_set: b.answer_style !== undefined ? true : cur.answer_style_set };
      if (next.mode === "local" && !comps.length) return json(res, 409, { error: "pair a companion first" });
      if (next.mode === "byok" && !llm) return json(res, 409, { error: "set up your provider and model first" });
      if ((next.fallback_free || next.fallback_paid || next.fallback_byok) && !agreed) return json(res, 409, { error: "this step cannot be turned on until your business has agreed to send data to that provider (Settings, AI providers and your data)" });
      if (next.fallback_paid && !next.paid_cap_usd) return json(res, 400, { error: "paid hosted inference needs a monthly spending limit" });
      Object.assign(cur, next); db.ai.set(b.business_id, cur);
    }
    const hosted = { free: { available: true, reason: null, provider: "openrouter", model: "demo-free", label: "Demo free model", price_in_per_m: 0, price_out_per_m: 0 }, paid: { available: true, reason: null, provider: "cloudflare", model: "demo-paid", label: "Demo paid model", price_in_per_m: 1, price_out_per_m: 2 } };
    const step = (name, ok, why, provider = null, model = null) => ({ step: name, mode: name === "free" || name === "paid" ? "platform" : name, provider, model, usable: ok, refusal: ok ? null : why });
    const steps = cur.mode === "deterministic" ? [] : [cur.mode === "local" ? step("local", comps.length > 0, "local:no_companion", "ollama", comps[0]?.model) : step("byok", !!llm && agreed, !llm ? "config:byok_not_configured" : "consent:missing_or_revoked", llm?.provider, llm?.model),
      ...(cur.fallback_byok ? [step("byok", !!llm && agreed, "consent:missing_or_revoked", llm?.provider, llm?.model)] : []), ...(cur.fallback_free ? [step("free", agreed, "consent:missing_or_revoked", "openrouter", "demo-free")] : []), ...(cur.fallback_paid ? [step("paid", agreed, "consent:missing_or_revoked", "cloudflare", "demo-paid")] : [])];
    const name = Object.entries(POL).find(([, p]) => p.mode === cur.mode && ["fallback_byok", "fallback_free", "fallback_paid"].every((f) => !!cur[f] === !!p[f]))?.[0] ?? "custom";
    const first = steps[0];
    return json(res, 200, { settings: cur, policy: name, steps, hosted, route: { mode: cur.mode, usable: cur.mode === "deterministic" || !!first?.usable, refusal: first?.refusal ?? null, provider: first?.provider ?? null, model: first?.model ?? null },
      switched_on_by_the_operator: ["byok", "local", "platform"], platform_models: [], companions: comps });
  }
  if (path === "/v1/usage/get") { const z = { requests: 0, tokens_in: 0, tokens_out: 0, cost: null, failed: 0 }; return json(res, 200, { summary: { period_start: new Date().toISOString().slice(0, 7) + "-01", plan: null, used: { platform: z, byok: z, local: z }, used_free: z, used_paid: z, remaining_platform: null, platform_allowed: false, remaining_free: null, free_allowed: false, spend_cap_usd: null, spend_cap_remaining_usd: null }, by_day: [], per_minute_default: 20 }); }
  if (path === "/v1/history") return json(res, 200, { items: db.history.filter((h) => !h.business_id || h.business_id === b.business_id).slice(0, 50) });
  if (path === "/v1/local/companions/register") { const list = db.companions.get(b.business_id) ?? []; for (const c of list) c.revoked = true; list.push({ id: randomUUID(), label: b.label, platform: b.platform, runtime: b.runtime, model: b.model, fit: b.fit ?? null, paired_at: new Date().toISOString(), last_seen_at: null, revoked: false }); db.companions.set(b.business_id, list); return json(res, 200, { id: list.at(-1).id, companions: list }); }
  if (path === "/v1/local/companions/revoke") { for (const c of db.companions.get(b.business_id) ?? []) c.revoked = true; return json(res, 200, { revoked: 1 }); }
  if (path === "/v1/local/companions/get") return json(res, 200, { companions: db.companions.get(b.business_id) ?? [] });
  if (path === "/v1/local/prepare") return json(res, 200, { done: true, result: { outcome: "faq", answer: "We are open Monday to Saturday, 9am to 6pm. (preview stand-in: no model was asked)", message: "", sources: [], execution: { path: "pipeline", mode: "local", provider: "ollama", model: "preview", generated_by: "deterministic", failure_kind: null, tier: "local", attempts: [] } }, conversation_id: randomUUID(), message_id: randomUUID() });
  if (path.startsWith("/v1/local/")) return json(res, 409, { error: "this business is not set up for local answering" });
  if (path.startsWith("/v1/ai/")) return consent(path, b, res);
  const state = db.llm.get(b.business_id) ?? { provider: "", model: "", base_url: "", enabled: false, has_key: false, capabilities: {} };
  if (path === "/v1/llm/get") return json(res, 200, { ...state, storage: "encrypted on the server (AES-256-GCM)", disclosure: "" });
  if (path === "/v1/llm/validate") return b.api_key === "bad" ? json(res, 200, { ok: false, code: "invalid_key", message: "The provider rejected the API key.", models: [], checked: [] }) : json(res, 200, { ok: true, code: null, message: "The key works. Choose a model.", models: ["demo-small", "demo-large"], checked: ["key"] });
  if (path === "/v1/llm/save") { const next = { provider: b.provider, model: b.model, base_url: b.base_url ?? "", enabled: !!b.enabled, has_key: true, capabilities: b.capabilities ?? {} }; db.llm.set(b.business_id, next); return json(res, 200, { ...next, label: b.provider, storage: "encrypted on the server (AES-256-GCM)" }); }
  if (path === "/v1/llm/remove") { db.llm.delete(b.business_id); return json(res, 200, { provider: "", model: "", enabled: false, has_key: false, capabilities: {} }); }
  return json(res, 404, { error: "not found" });
}

createServer(async (req, res) => {
  const url = new URL(req.url, base);
  res.on("finish", persist);
  try {
    if (url.pathname.startsWith("/supa/")) return await supabase(req, res, url);
    if (url.pathname.startsWith("/companion/")) {  // the page's way to the companion on this computer: same address as the site, so no browser has to allow a cross-address request
      const chunks = []; for await (const x of req) chunks.push(x);
      const headers = { origin: `http://${req.headers.host}`, host: "127.0.0.1:47890" };
      for (const h of ["content-type", "x-companion-key", "x-companion-ts", "x-companion-nonce", "x-companion-sig"]) if (req.headers[h]) headers[h] = req.headers[h];
      try {
        const r = await fetch("http://127.0.0.1:47890" + url.pathname.slice("/companion".length) + url.search, { method: req.method, headers, body: ["GET", "HEAD"].includes(req.method) ? undefined : Buffer.concat(chunks), signal: AbortSignal.timeout(300_000) });
        res.writeHead(r.status, { "content-type": r.headers.get("content-type") ?? "application/json", "cache-control": "no-store" });
        return res.end(Buffer.from(await r.arrayBuffer()));
      } catch { return json(res, 502, { error: "the companion is not running on this computer" }); }
    }
    if (url.pathname.startsWith("/engine/")) return await engine(req, res, url);
    if (routes[url.pathname]) {
      const mod = await import(`../api/_routes/${routes[url.pathname]}.js`);
      const handler = mod.POST ?? mod.GET;
      const raw = req.method === "POST" ? Buffer.concat(await (async () => { const c = []; for await (const x of req) c.push(x); return c; })()) : undefined;
      const out = await handler(new Request(url, { method: req.method, headers: req.headers, body: raw }));
      let body = Buffer.from(await out.arrayBuffer());
      if (url.pathname === "/api/config" && req.headers.host && !/^(127\.0\.0\.1|localhost)(:|$)/.test(req.headers.host)) {  // opened under another name (for example knovent.localhost, to see the hosted-site screens): the browser must reach the stand-in at THAT name
        try { const c = JSON.parse(body.toString()); c.supabaseUrl = `http://${req.headers.host}/supa`; body = Buffer.from(JSON.stringify(c)); } catch {}
      }
      const outHeaders = Object.fromEntries(out.headers); delete outHeaders["content-length"];
      res.writeHead(out.status, outHeaders);
      return res.end(body);
    }
    let file = join(root, "public", url.pathname === "/" ? "index.html" : url.pathname);
    if (!existsSync(file) && existsSync(file + ".html")) file += ".html";
    if (!existsSync(file) || !file.startsWith(join(root, "public"))) { res.writeHead(404, { "content-type": "text/html", ...SECURITY_HEADERS }); return res.end(readFileSync(join(root, "public", "404.html"))); }
    res.writeHead(200, { "content-type": TYPES[extname(file)] ?? "application/octet-stream", "cache-control": "no-cache", ...SECURITY_HEADERS });  // the SAME headers the deployment sends (vercel.json), so the browser enforces the real CSP here too
    res.end(readFileSync(file));
  } catch (e) { console.error(e); res.writeHead(500); res.end("preview error"); }
}).listen(port, "127.0.0.1", () => console.log(`preview on ${base}  (sign in with any email and password)`));
