// LOCAL FIRST on the page (public/app.js), run for real in the sandbox: the first-run choice, the .env drop, the automatic local setup, the model list, and how an answer says which step made it.
// What they prove: nothing is downloaded before a confirmation, a dropped .env file gives up only the one provider key chosen (never uploaded, never kept, never shown), a file of secrets is never
// uploaded as knowledge, and the page says which step answered and why the earlier ones did not.
import assert from "node:assert/strict";
import { test } from "node:test";
import { BIZ, page } from "./_page.js";

const plain = (x) => JSON.parse(JSON.stringify(x));
const KEY_OPENAI = "sk-proj-AAAAAAAAAAAAAAAA1234";
const KEY_OR = "sk-or-v1-BBBBBBBBBBBBBBBB5678";
const stub = (text, size) => ({ size: size ?? text.length, text: async () => text });

// ---------------------------------------------------------------- reading a .env file
test("a .env file gives up only the provider keys it holds, whatever its formatting, and nothing else", () => {
  const p = page();
  const parse = (t) => plain(p.run(`parseEnvText(${JSON.stringify(t)})`));
  const env = [`﻿# keys for the app`, `DATABASE_URL=postgres://user:hunter2-password@db.example/app`, `export OPENAI_API_KEY="${KEY_OPENAI}"`, `ANTHROPIC_API_KEY='sk-ant-CCCCCCCCCCCCCCCC9999'  `, `OPENROUTER_API_KEY=${KEY_OR} # the router`,
    `STRIPE_SECRET_KEY=sk_live_DDDDDDDDDDDDDDDD`, `GOOGLE_API_KEY=AIzaEEEEEEEEEEEEEEEE`, `GEMINI_API_KEY=AIzaFFFFFFFFFFFFFFFF`, ``, `NOT A LINE`, `OPENAI_API_KEY_BACKUP=sk-nope-GGGGGGGGGGGG`].join("\r\n");
  const r = parse(env);
  assert.deepEqual(Object.fromEntries(r.found.map((f) => [f.provider, f.key])), { openai: KEY_OPENAI, anthropic: "sk-ant-CCCCCCCCCCCCCCCC9999", openrouter: KEY_OR, gemini: "AIzaFFFFFFFFFFFFFFFF" }); // the later Gemini name wins; every key as written, unquoted
  assert.ok(!JSON.stringify(r).includes("hunter2") && !JSON.stringify(r).includes("sk_live") && !JSON.stringify(r).includes("sk-nope")); // the database password, another service's key and a lookalike name are not read
  assert.equal(r.skipped, 4);
  for (const t of ["OPENAI_API_KEY=", "OPENAI_API_KEY=short", `OPENAI_API_KEY=${"x".repeat(401)}`, "OPENAI_API_KEY=has a space inside", "# OPENAI_API_KEY=sk-commented-out-0000", "openai_api_key=sk-lowercase-name-0000"]) assert.deepEqual(parse(t).found, [], t);
  assert.deepEqual(parse("A".repeat(70000)), { found: [], skipped: 0, tooBig: true });
});

test("dropping a .env file with one provider's key fills the form with that key, shows only its last four characters, and keeps nothing else", async () => {
  const p = page();
  const el = p.run("llmForm()");
  const input = el.all((e) => e.tag === "input" && e.attrs.type === "file")[0];
  input.files = [stub(`DB_PASSWORD=hunter2-hunter2\nOPENAI_API_KEY=${KEY_OPENAI}\n`)];
  await input.fire("change");
  await p.flush();
  const F = p.state().form;
  assert.equal(F.provider, "openai"); assert.equal(F.key, KEY_OPENAI);
  assert.match(F.msg.text, /Using the OpenAI key from the file \(••••1234\)\. Nothing else in the file was kept/);
  assert.ok(!F.msg.text.includes(KEY_OPENAI) && !JSON.stringify(F).includes("hunter2")); // the message and the form never carry the rest of the file
  assert.equal(p.run("JSON.stringify(ENV_HELD)"), "{}"); // the held keys were dropped as soon as one was used
  assert.deepEqual(p.fetches, []); // the file was read in the browser: nothing was uploaded, nothing was sent
});

test("a file with several providers' keys asks which one, names them by their last four characters only, and discards the others", async () => {
  const p = page();
  const input = p.run("llmForm()").all((e) => e.tag === "input" && e.attrs.type === "file")[0];
  input.files = [stub(`OPENAI_API_KEY=${KEY_OPENAI}\nOPENROUTER_API_KEY=${KEY_OR}\n`)];
  await input.fire("change");
  await p.flush();
  assert.equal(p.state().form.key, ""); // nothing is chosen for the person
  const el = p.run("llmForm()");
  assert.deepEqual(plain(p.state().form.envFound), [{ provider: "openai", mask: "••••1234" }, { provider: "openrouter", mask: "••••5678" }]);
  assert.ok(!el.textContent.includes(KEY_OPENAI) && !el.textContent.includes(KEY_OR));
  await el.buttons("OpenRouter (••••5678)")[0].fire("click");
  const F = p.state().form;
  assert.equal(F.provider, "openrouter"); assert.equal(F.key, KEY_OR); assert.equal(F.envFound, null);
  assert.equal(p.run("JSON.stringify(ENV_HELD)"), "{}"); // the OpenAI key from the same file is gone
});

test("a file with no provider key, a file that is too large, and a file that cannot be read keep nothing and say so", async () => {
  for (const [file, words] of [[stub("DATABASE_URL=postgres://x\nPORT=3000\n"), /No key for OpenAI, Anthropic, Google Gemini or OpenRouter .* Nothing from it was kept/], [stub("x", 70000), /too large to be a \.env file/],
                                [{ size: 10, text: async () => { throw new Error("boom"); } }, /could not be read/]]) {
    const p = page();
    const input = p.run("llmForm()").all((e) => e.tag === "input" && e.attrs.type === "file")[0];
    input.files = [file];
    await input.fire("change");
    await p.flush();
    assert.match(p.state().form.msg.text, words);
    assert.equal(p.state().form.key, ""); assert.equal(p.run("JSON.stringify(ENV_HELD)"), "{}");
  }
});

test("a file of secrets is never uploaded as knowledge", async () => {
  const p = page();
  const touched = [];
  p.state().sb = { from: () => ({ insert: async () => { touched.push("insert"); return { error: null }; }, select: () => ({ eq: () => ({ order: () => ({ limit: async () => ({ data: [], error: null }), then: (f) => f({ data: [], error: null }) }) }) }) }), storage: { from: () => ({ upload: async () => { touched.push("upload"); return { error: null }; } }) } };
  p.set("sources", []);
  const el = p.run("knowledgeTab(true)");
  const input = el.all((e) => e.tag === "input" && e.attrs.type === "file")[0];
  for (const name of [".env", "prod.env", ".env.local", "server.pem", "id_rsa", "credentials", "secrets.json", "notes.key"]) {
    input.files = [{ name, size: 100 }];
    await input.fire("change");
    await p.flush();
    assert.match(p.state().msg.text, /looks like a file of secrets .* not uploaded/, name);
  }
  assert.deepEqual(p.fetches.filter((f) => f.url === "/api/sources/ingest"), []); assert.deepEqual(touched, []); // nothing reached storage, the database or the engine
});

// ---------------------------------------------------------------- the first-run choice
const SB = { from: () => ({ update: () => ({ eq: async () => ({ error: null }) }) }) };
test("the first-run choice offers this computer first, an API service second and no AI third, and nothing is saved until one is chosen", async () => {
  const p = page();
  p.run("S.sb = " + JSON.stringify(null)); p.state().sb = SB;
  const el = p.run("onboardingPanel()");
  const buttons = el.all((e) => e.tag === "button" && /^choice/.test(e.className)).map((b) => b.children[0].textContent);
  assert.deepEqual(buttons, ["On my computer (recommended)", "Through an API service", "No AI for now"]);
  assert.match(el.textContent, /Your questions and documents stay here; no account or key is needed/);
  assert.match(el.textContent, /only after you agree/);
  assert.deepEqual(p.fetches, []);
  await el.all((e) => e.tag === "button" && /^choice/.test(e.className))[2].fire("click");
  await p.flush();
  assert.equal(p.state().form.choice, "none"); // choosing is not saving: a confirmation page with Back comes first
  assert.deepEqual(p.fetches, []);
  const confirm = p.run("onboardingPanel()");
  assert.ok(confirm.buttons("← Back").length === 1);
  await confirm.buttons("Continue without AI")[0].fire("click");
  await p.flush();
  assert.match(p.state().msg.text, /No AI for now: answers come from the deterministic engine, and nothing is sent anywhere/);
});

test("choosing this computer explains the steps, and 'Use my computer for AI' waits until a model is set up and then saves local only", async () => {
  const saved = { policy: "local_only", settings: { mode: "local" }, steps: [{ step: "local", usable: true }], companions: [{ id: "c", revoked: false, fit: "comfortable" }] };
  const p = page((url) => (url === "/api/ai/settings/save" ? [200, saved] : [404, {}]));
  p.state().sb = SB;
  p.state().form.choice = "local";
  p.set("ai", { companions: [], steps: [], settings: {}, switched_on_by_the_operator: ["local"] });
  let el = p.run("onboardingPanel()");
  assert.match(el.textContent, /looks for the companion on this computer.*authorized on this computer.*one click downloads the recommended small, fast model/);
  assert.match(el.textContent, /Outside fallbacks \(free or paid hosted AI, your own provider account\) are off/);
  const use = () => p.run("onboardingPanel()").buttons("Use my computer for AI")[0];
  assert.equal(use().disabled, true);
  p.set("ai", { companions: [{ id: "c", revoked: false, fit: "not_advised" }], steps: [], settings: {} });
  assert.equal(use().disabled, true); // a computer that said it cannot run the model comfortably does not complete the setup
  p.set("ai", { companions: [{ id: "c", revoked: false, fit: "comfortable" }], steps: [], settings: {} });
  assert.equal(use().disabled, false);
  await use().fire("click");
  await p.flush();
  assert.deepEqual(p.fetches.at(-1).body, { business_id: BIZ, policy: "local_only" }); // local only: no hosted step, no spending limit
  assert.match(p.state().msg.text, /AI will run on your own computer\. Nothing is sent to any other company/);
});

// ---------------------------------------------------------------- automatic local setup, and the models on this computer
const HW = { os: "macOS", os_version: "15", arch: "arm64", cpu: "Apple M2", cores: 8, ram_gb: 8, gpus: [] };
const REC = { models: [{ id: "granite3.3-2b", ollama: "granite3.3:2b", params_b: 2.0, speed: "fast", verdict: "comfortable", why: "needs about 2.3 GB of the 8.0 GB it would run in" },
                       { id: "llama3.2-1b", ollama: "llama3.2:1b", params_b: 1.0, speed: "fastest", verdict: "comfortable", why: "needs about 1.7 GB" },
                       { id: "qwen3-8b", ollama: "qwen3:8b", params_b: 8.0, speed: "capable", verdict: "not_advised", why: "it would swap or fail" }],
              limits: [], suggested: { fastest: "llama3.2-1b", fast: "granite3.3-2b" }, start_with: "granite3.3-2b" };
function paired(over = {}) {
  const calls = [];
  const companion = {
    hello: async () => ({ present: true, version: "0.1.0" }), paired: async () => true, status: async () => ({ hardware: HW, runtimes: { ollama: { running: true, installed: true, models: [] } } }), recommend: async () => REC,
    installed: async () => ({ runtimes: { ollama: { models: [] } }, selected: null }), health: async () => ({ state: "no_model_selected", ok: false, stats: {} }), storage: async () => ({ free_gb: 40, models_gb: 0 }),
    provision: async (b, m) => { calls.push(["provision", m]); return { job: "j1", model: "granite3.3:2b", already_installed: false, fit: "comfortable" }; }, pullStatus: async () => ({ state: "done", done: 100, total: 100 }),
    update: async (b, rt, m) => { calls.push(["update", m]); return { job: "j2" }; }, remove: async (b, rt, m) => { calls.push(["remove", m]); return {}; }, select: async (b, rt, m, d) => { calls.push(["select", m, d]); return {}; },
    cancel: async () => ({}), ...over };
  const p = page((url) => (url === "/api/ai/settings/get" ? [200, { settings: {}, steps: [], companions: [{ id: "c", revoked: false }] }] : [200, {}]), { Companion: companion });
  p.set("chello", { present: true, version: "0.1.0" });
  p.set("cstatus", { hardware: HW, runtimes: { ollama: { running: true, installed: true, models: [] } }, recommend: REC, installed: { runtimes: { ollama: { models: [] } }, selected: null }, health: { state: "no_model_selected", ok: false, stats: {} }, storage: { free_gb: 40, models_gb: 0 } });
  p.set("ai", { companions: [], steps: [], settings: {}, switched_on_by_the_operator: ["local"] });
  return { p, calls };
}

test("nothing is downloaded until the person has seen the model, its size and the free space and ticked the box; then one click sets everything up", async () => {
  const { p, calls } = paired();
  let el = p.run("localPanel(true)");
  assert.match(el.textContent, /Set up local AI for me\. Recommended for this computer: granite3\.3-2b \(fast\)\. It will be downloaded from Ollama’s own library to this computer: about 1\.3 GB, and 40 GB is free/);
  assert.match(el.textContent, /nothing is sent to anyone/);
  const go = () => p.run("localPanel(true)").buttons("Download granite3.3-2b (about 1.3 GB) and set up")[0];
  assert.deepEqual(calls, []); // seeing the model and its size is not downloading it
  assert.equal(go().disabled, false);
  await go().fire("click"); // the one click is the confirmation
  await p.flush();
  await new Promise((r) => setTimeout(r, 1700)); // (the page checks the download every 1.5 s)
  await p.flush();
  assert.deepEqual(calls.filter((c) => c[0] === "provision"), [["provision", "granite3.3-2b"]]);
  const register = p.fetches.find((f) => f.url === "/api/local/companions/register");
  assert.deepEqual(register.body, { business_id: BIZ, label: "owner’s computer", platform: "macOS arm64", runtime: "ollama", model: "granite3.3:2b", fit: "comfortable" }); // the model and the companion's own verdict: nothing secret
  assert.match(p.state().msg.text, /granite3\.3:2b is downloaded and ready\. Local AI is set up/);
});

test("when Ollama is not running the page says how to start it and offers no download", () => {
  const { p } = paired();
  p.set("cstatus", { hardware: HW, runtimes: { ollama: { running: false, installed: true, models: [] } }, recommend: REC, installed: null, health: { state: "runtime_not_running", ok: false, stats: {} }, storage: null });
  const t = p.run("localPanel(true)").textContent;
  assert.match(t, /Ollama, the program that runs the model, is not running on this computer\. Install it from https:\/\/ollama\.com\/download/);
  assert.match(t, /The companion does not install software/);
  assert.ok(!t.includes("and set up") && /Ollama is not running\. Start it, then press Refresh/.test(t));
});

test("several small, fast models are offered with their speed and fit, one is marked as the place to start, and a model that does not fit has no download button", () => {
  const { p } = paired();
  const el = p.run("localPanel(true)");
  const rows = el.all((e) => e.tag === "tr").slice(1).map((r) => r.children.map((c) => c.textContent));
  assert.deepEqual(rows.map((r) => r[0].replace(/start here|good .* pick/, "")), ["granite3.3-2b", "llama3.2-1b", "qwen3-8b"]);
  assert.match(rows[0][0], /start here/); assert.match(rows[1][0], /good fastest pick/);
  assert.deepEqual(rows.map((r) => r[1]), ["fast", "fastest", "capable"]);
  assert.deepEqual(rows.map((r) => /Download/.test(r[4])), [true, true, false]);
});

test("the models on this computer show their size, which is in use, the space they take, and can be updated, switched or removed (not the one in use)", async () => {
  const { p, calls } = paired();
  p.set("cstatus", { ...plain(p.state().cstatus), installed: { selected: { runtime: "ollama", model: "llama3.2:1b" }, runtimes: { ollama: { models: [{ name: "llama3.2:1b", size_bytes: 1_300_000_000, selected: true }, { name: "granite3.3:2b", size_bytes: 1_500_000_000, selected: false }] } } },
    storage: { free_gb: 38.2, models_gb: 2.6 }, health: { state: "ready", ok: true, loaded: true, stats: { questions: 3, failures: 1, last_seconds: 4.2 } } });
  const el = p.run("localPanel(true)");
  assert.match(el.textContent, /Models take 2\.6 GB; 38\.2 GB free/);
  assert.match(el.textContent, /Ready: the model is installed and can answer\. 3 questions answered since the companion started, 1 failed; the last took 4\.2 s/);
  const rows = el.all((e) => e.tag === "tr").filter((r) => /llama3.2:1b|granite3.3:2b/.test(r.children[0].textContent) && r.children.length === 4);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((r) => r.children.slice(0, 3).map((c) => c.textContent)), [["llama3.2:1b", "1.2 GB", "in use"], ["granite3.3:2b", "1.4 GB", ""]]);
  assert.deepEqual(rows[0].all((e) => e.tag === "button").map((b) => b.textContent), ["Update"]); // the one in use cannot be removed or "used" again
  assert.deepEqual(rows[1].all((e) => e.tag === "button").map((b) => b.textContent), ["Use", "Update", "Remove"]);
  await rows[1].buttons("Remove")[0].fire("click");
  await p.flush();
  assert.deepEqual(calls.filter((c) => c[0] === "remove"), [["remove", "granite3.3:2b"]]);
});

// ---------------------------------------------------------------- which step answered
const EX = (o) => ({ path: "pipeline", mode: "platform", provider: "openrouter", model: "free-small", generated_by: "model", failure_kind: null, tier: "free", ...o });
test("an answer from a later step says which step answered and why the earlier ones did not", () => {
  const p = page();
  const ex = EX({ fallback_from: { step: "local", provider: "ollama", reason: "provider:timeout" }, attempts: [{ step: "local", provider: "ollama", outcome: "unavailable", reason: "provider:timeout" }, { step: "free", provider: "openrouter", outcome: "answered", reason: null }] });
  const t = p.run(`whoAnswered(${JSON.stringify(ex)})`);
  assert.match(t, /A language model at OpenRouter \(free-small\) \(free hosted ai\)\. The question and the passages found were sent to OpenRouter, a third party/);
  assert.match(t, /Steps tried: Your own computer: could not answer — the provider did not answer \(timeout\) → Free hosted AI \(OpenRouter\): answered\./);
  const refused = EX({ generated_by: "deterministic_fallback", failure_kind: "provider:timeout", mode: "local", provider: "ollama", tier: "local", attempts: [{ step: "local", provider: "ollama", outcome: "unavailable", reason: "provider:timeout" }, { step: "free", provider: "openrouter", outcome: "refused", reason: "consent:missing_or_revoked" }] });
  const r = p.run(`whoAnswered(${JSON.stringify(refused)})`);
  assert.match(r, /not a model’s answer/); assert.match(r, /Free hosted AI \(OpenRouter\): not used — this business has not agreed to send data to that provider/);
  assert.equal(p.run(`whoAnswered(${JSON.stringify(EX({ mode: "local", provider: "ollama", tier: "local", attempts: [{ step: "local", outcome: "answered" }] }))})`).startsWith("A model on your own computer"), true); // one step: no "steps tried" line
});

test("the history says which step answered", () => {
  const p = page();
  p.set("history", { items: [{ created_at: "2026-11-02T10:00:00+00:00", question: "q", outcome: "grounded", execution: EX({}) }, { created_at: "2026-11-02T10:01:00+00:00", question: "q2", outcome: "grounded", execution: EX({ tier: "paid", provider: "cloudflare", model: "paid-small" }) }], done: true, loading: false });
  const t = p.run("historyTab()").textContent;
  assert.match(t, /Free hosted AI · OpenRouter free-small/); assert.match(t, /Paid hosted AI · Cloudflare Workers AI paid-small/);
});

test("the API functions pass the new setup fields, shaped, and nothing else", async () => {
  const { settingsFields, companionFields } = await import("../api/_lib/ai_fields.js");
  assert.deepEqual(plain(settingsFields({ policy: "local_free_paid", paid_cap_usd: 5, fallback: "none", evil: 1, user_id: "x", role: "owner" })),
    { policy: "local_free_paid", fallback: "none", paid_cap_usd: 5 }); // (what is not named is not sent)
  for (const bad of [{ policy: "cloud_first" }, {}, { policy: "local_only", fallback_free: "yes" }, { policy: "local_only", paid_cap_usd: 0 }, { policy: "local_only", paid_cap_usd: -1 }, { policy: "local_only", paid_cap_usd: 1e9 }, { policy: "local_only", paid_cap_usd: "5" }, { policy: "local_only", paid_cap_usd: NaN }]) {
    assert.throws(() => settingsFields(bad), /Choose how this business answers|A field is not valid/, JSON.stringify(bad));
  }
  assert.equal(companionFields({ label: "x", runtime: "ollama", model: "granite3.3:2b", fit: "comfortable" }).fit, "comfortable");
  assert.throws(() => companionFields({ label: "x", runtime: "ollama", model: "granite3.3:2b", fit: "great" }), /A field is not valid/);
});

test("the settings page does not break when the engine is older than the page and sends no free/paid split", () => {
  const p = page();
  const u = (n) => ({ requests: n, tokens_in: 0, tokens_out: 0, cost: null, failed: 0 });
  p.set("usage", { summary: { period_start: "2026-11-01", plan: null, used: { platform: u(2), byok: u(1), local: u(0) }, remaining_platform: null, platform_allowed: false }, by_day: [] }); // (the shape before migration 13)
  const t = p.run("usagePanel(true)").textContent;
  assert.match(t, /Free hosted AI/); assert.match(t, /Paid hosted AI/); assert.match(t, /Your own AI provider/);
  p.set("ai", { settings: { mode: "local", fallback: "deterministic" }, route: { mode: "local", usable: true }, switched_on_by_the_operator: ["local"], companions: [] }); // no policy, no steps, no hosted
  assert.match(p.run("answerModePanel(true)").textContent, /Now: a custom set of steps/);
});

test("after 'Change AI setup', choosing this computer (or no AI) and continuing leaves the wizard; it does not stay open doing nothing", async () => {
  const saved = { policy: "local_only", settings: { mode: "local" }, steps: [{ step: "local", usable: true }], companions: [{ id: "c", revoked: false, fit: "comfortable" }] };
  const p = page((url) => (url === "/api/ai/settings/save" ? [200, saved] : [404, {}]));
  p.state().sb = SB;
  p.run("S.current.onboarding_completed_at = '2026-11-01T00:00:00Z'"); // the business finished setup once (first with "No AI for now")
  for (const route of ["local", "none"]) {
    p.state().reonboard = true; p.state().form.choice = route; // "Change AI setup" -> a choice
    p.set("ai", { companions: [{ id: "c", revoked: false, fit: "comfortable" }], steps: [], settings: {} });
    const el = p.run("onboardingPanel()");
    await (route === "local" ? el.buttons("Use my computer for AI")[0] : el.buttons("Continue without AI")[0]).fire("click");
    await p.flush();
    assert.equal(p.state().reonboard, false, route + ": the wizard is closed");
    assert.equal(p.state().form.choice, null, route + ": the choice is cleared");
    assert.ok(p.state().msg && p.state().msg.kind === "good", route + ": the person is told what happened");
  }
});

test("on the hosted site the next step is 'Measure', and files that were listed but not read say how to finish", async () => {
  const p = page();
  p.ctx.location.hostname = "knovent-web.vercel.app"; p.ctx.location.protocol = "https:";
  p.state().tab = "knowledge";
  p.set("sources", [{ id: "s1", name: "faq.csv", status: "pending", source_type: "faq_table" }]);
  let foot = p.run("journeyFooter(true)");
  assert.match(foot.textContent, /listed but not read yet.*Read again/);
  const go = () => foot.buttons("Measure my knowledge →")[0];
  assert.equal(go().disabled, true); // nothing is read yet: it cannot be pressed, and the line above says why
  p.set("sources", [{ id: "s1", name: "faq.csv", status: "ready", source_type: "faq_table" }]);
  foot = p.run("journeyFooter(true)");
  assert.match(foot.textContent, /1 source is ready\. Next, measure how well questions find their answers/);
  assert.equal(go().disabled, false);
  assert.match(p.run("trainingTab(true)").textContent, /How well your knowledge is found.*It measures; it does not change the model/);
  p.ctx.location.hostname = "127.0.0.1"; p.ctx.location.protocol = "http:"; // the local app still trains
  p.set("sources", [{ id: "s1", name: "faq.csv", status: "ready", source_type: "faq_table" }]);
  assert.match(p.run("journeyFooter(true)").textContent, /Train on my knowledge/);
});
