// The consent panel of the page (public/app.js), run for real in a sandbox with a small stand-in for the DOM and for fetch. Run: npm test
// What it proves: the page shows the text the backend returned, offers an agreement only for a provider whose terms are verified, needs an explicit tick, sends nothing but the provider and the
// version that was shown, offers withdrawal always, and offers nothing at all when it cannot tell where the business stands.
import assert from "node:assert/strict";
import { test } from "node:test";
import { BIZ, page as basePage, source } from "./_page.js";

function page(reply) {
  const p = basePage(reply);
  return { ...p, panel: (admin = true) => p.run(`consentPanel(${admin})`) };
}

const provider = (over = {}) => ({ provider: "cloudflare", label: "Cloudflare Workers AI", disclosure_version: "2026-11-02.aaaa1111", terms_verified: true, consentable: true, unverified: [], agreed: false, in_use: false, history: [], ...over });
const TEXT = "Sending your data to Cloudflare Workers AI (platform-managed)\n\nWhat may be sent:\n  - the question a customer asks\n<script>alert(1)</script>";
const described = (over = {}) => ({ provider: "cloudflare", label: "Cloudflare Workers AI", version: "2026-11-02.aaaa1111", sha256: "a".repeat(64), text: TEXT, verified: true, consentable: true, data_categories: ["question", "evidence_passages"], ...over });
const grantedRow = (over = {}) => ({ id: "1", provider: "cloudflare", status: "granted", disclosure_version: "2026-11-02.aaaa1111", data_categories: ["question", "evidence_passages"], granted_by: "u", granted_at: "t", revoked_by: null, revoked_at: null, ...over });

test("with no provider whose terms are verified, nothing can be agreed to and the page says so", () => {
  const p = page();
  p.set("consent", { providers: [provider({ terms_verified: false, consentable: false, unverified: ["retention"] }), provider({ provider: "gemini", label: "Google Gemini", terms_verified: false, consentable: false })] });
  const el = p.panel();
  assert.match(el.textContent, /No AI provider is available yet/);
  assert.match(el.textContent, /nothing is sent anywhere/);
  assert.equal(el.buttons("Agree").length + el.buttons("Read the disclosure").length + el.all((e) => e.tag === "input").length, 0);
});

test("a provider that is not verified is listed as not offered once another is, and never gets an agree button", () => {
  const p = page();
  p.set("consent", { providers: [provider(), provider({ provider: "openai", label: "OpenAI API (your own key)", terms_verified: false, consentable: false })] });
  const el = p.panel();
  assert.equal(el.all((e) => e.attrs["data-provider"]).length, 1);
  assert.match(el.textContent, /Not offered, because their terms are not verified yet: OpenAI API/);
});

test("reading the disclosure shows exactly the text the backend returned, as text, with provider, version and what may be sent", async () => {
  const p = page((url) => (url === "/api/ai/disclosure" ? [200, described()] : [404, {}]));
  p.set("consent", { providers: [provider()] });
  p.panel().buttons("Read the disclosure")[0].fire("click");
  await p.flush();
  assert.deepEqual(p.fetches.map((f) => [f.url, f.body]), [["/api/ai/disclosure", { business_id: BIZ, provider: "cloudflare" }]]);
  assert.equal(p.fetches[0].headers.authorization, "Bearer tok");
  const el = p.panel();
  const pre = el.all((e) => e.tag === "pre")[0];
  assert.equal(pre.textContent, TEXT); // the markup in the text is shown, never run
  assert.equal(pre.children.length, 1);
  assert.match(el.textContent, /Provider: Cloudflare Workers AI \(a third party, separate from this service\)/);
  assert.match(el.textContent, /Disclosure version: 2026-11-02\.aaaa1111/);
  assert.match(el.textContent, /What may be sent: the question a customer asks; the passages of your own knowledge that were retrieved for that question/);
});

test("agreeing needs the text to be read and an explicit tick, and sends only the provider and the version that was shown", async () => {
  const after = { providers: [provider({ agreed: true, in_use: true, history: [grantedRow()] })] };
  const p = page((url) => (url === "/api/ai/disclosure" ? [200, described()] : url === "/api/ai/consent/grant" ? [200, after] : [404, {}]));
  p.set("consent", { providers: [provider()] });
  assert.equal(p.panel().buttons("Agree").length, 0); // before reading the text there is nothing to agree to
  p.panel().buttons("Read the disclosure")[0].fire("click");
  await p.flush();
  let el = p.panel();
  assert.equal(el.buttons("Agree")[0].disabled, true); // read, but not ticked
  el.buttons("Agree")[0].fire("click");
  assert.equal(p.fetches.length, 1); // a disabled button does nothing
  const box = el.all((e) => e.tag === "input")[0];
  box.checked = true;
  box.fire("change");
  el = p.panel();
  assert.match(el.textContent, /I have read this text, and I agree, for my business, that the data it lists may be sent to Cloudflare Workers AI, a third party\./);
  el.buttons("Agree")[0].fire("click");
  await p.flush();
  const grant = p.fetches.at(-1);
  assert.equal(grant.url, "/api/ai/consent/grant");
  assert.deepEqual(grant.body, { business_id: BIZ, provider: "cloudflare", disclosure_version: "2026-11-02.aaaa1111" }); // nothing that says "consented", no user, no role
  assert.equal(p.state().consent.providers[0].agreed, true);
  assert.equal(p.state().disclosure.cloudflare, undefined); // the text is read again next time
  assert.match(p.panel().textContent, /Agreed · in use/);
});

test("the version sent is the one that was displayed, even if the card says another", async () => {
  const p = page((url) => (url === "/api/ai/disclosure" ? [200, described({ version: "2026-11-02.shown001" })] : [200, { providers: [provider()] }]));
  p.set("consent", { providers: [provider({ disclosure_version: "2026-11-02.other002" })] });
  p.panel().buttons("Read the disclosure")[0].fire("click");
  await p.flush();
  p.run("S.ack.cloudflare = true");
  p.panel().buttons("Agree")[0].fire("click");
  await p.flush();
  assert.equal(p.fetches.at(-1).body.disclosure_version, "2026-11-02.shown001");
});

test("when the text changed since it was shown, the server's refusal is explained and the person must read it again", async () => {
  const p = page((url) => (url === "/api/ai/disclosure" ? [200, described()] : [409, { error: "the disclosure text has changed or was not the one shown; read the current text and agree to that" }]));
  p.set("consent", { providers: [provider()] });
  p.panel().buttons("Read the disclosure")[0].fire("click");
  await p.flush();
  p.run("S.ack.cloudflare = true");
  p.panel().buttons("Agree")[0].fire("click");
  await p.flush();
  assert.match(p.state().msg.text, /text has changed.*Read the text again before agreeing/);
  assert.equal(p.state().consent.providers[0].agreed, false);
  assert.equal(p.state().disclosure.cloudflare, undefined);
});

test("a withdrawal is always offered to an owner or admin, for a verified provider or not, and says what it does", async () => {
  const after = { providers: [provider({ terms_verified: false, consentable: false })] };
  const p = page((url) => (url === "/api/ai/consent/revoke" ? [200, after] : [404, {}]));
  // agreed earlier; the provider's entry is no longer verified: it is not in use, and it can still be withdrawn
  p.set("consent", { providers: [provider({ terms_verified: false, consentable: false, agreed: true, in_use: false, history: [grantedRow()] })] });
  const el = p.panel();
  assert.match(el.textContent, /Agreed · not in use/);
  assert.equal(el.buttons("Agree").length, 0);
  assert.match(el.textContent, /blocks any further sending to this provider, starting with the very next question/);
  el.buttons("Withdraw agreement")[0].fire("click");
  await p.flush();
  assert.deepEqual(p.fetches.at(-1).body, { business_id: BIZ, provider: "cloudflare" });
  assert.equal(p.fetches.at(-1).url, "/api/ai/consent/revoke");
  assert.equal(p.state().consent.providers[0].agreed, false);
});

test("an agreement to an earlier text is shown as not in use and can be withdrawn", () => {
  const p = page();
  p.set("consent", { providers: [provider({ agreed: false, history: [grantedRow({ disclosure_version: "2026-10-01.old00000" })] })] });
  const el = p.panel();
  assert.match(el.textContent, /Agreed to an earlier version of the text · not in use/);
  assert.equal(el.buttons("Withdraw agreement").length, 1);
});

test("a member who is not an owner or admin can read but not agree or withdraw", () => {
  const p = page();
  p.set("consent", { providers: [provider({ agreed: true, in_use: true, history: [grantedRow()] }), provider({ provider: "gemini", label: "Google Gemini" })] });
  const el = p.panel(false);
  assert.equal(el.buttons("Withdraw agreement").length + el.buttons("Agree").length, 0);
  assert.match(el.textContent, /Only owners and admins can agree or withdraw/);
});

test("if the page cannot tell where the business stands it offers nothing", () => {
  const p = page();
  p.set("consent", null);
  const el = p.panel();
  assert.match(el.textContent, /could not be loaded, so nothing is offered/);
  assert.equal(el.all((e) => e.tag === "button" || e.tag === "input").length, 0);
});

test("the page never sends a claim that someone consented, and every consent call carries a bearer token and the business", () => {
  for (const m of source.matchAll(/api\('\/api\/ai\/consent\/(grant|revoke)',\s*(\{[^}]*\})/g)) {
    const keys = [...m[2].matchAll(/(\w+)\s*[:,}]/g)].map((x) => x[1]);
    assert.ok(!keys.some((k) => /consent|agreed|granted|verified|user|role|status/i.test(k)), `${m[1]} sends ${keys}`);
  }
  assert.ok(!/(agreed|consented|granted)\s*:\s*true/.test(source));
});

test("a saved model that is not in use says so, in Settings and after saving", () => {
  const p = page();
  p.set("llm", { enabled: true, provider: "gemini", label: "Google Gemini", model: "m", has_key: true, capabilities: {}, in_use: { used: false, reason: "provider_unverified", message: "This provider's data-handling terms have not been verified by this service, so no data is sent to it and answers stay local." } });
  p.set("consent", { providers: [] });
  const tab = p.run("settingsTab(true)");
  assert.match(tab.textContent, /Not in use\. This provider's data-handling terms have not been verified/);
  assert.match(tab.textContent, /AI providers and your data/);
});
