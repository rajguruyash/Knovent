// A page for tests: public/app.js run for real in a sandbox with a small stand-in for the DOM and for fetch. Shared by the UI tests (not a test itself).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

export const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "public", "app.js"), "utf8");
export const BIZ = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

export class El {
  constructor(tag) { this.nodeType = 1; this.tag = tag; this.attrs = {}; this.children = []; this.handlers = {}; this.className = ""; this.checked = false; this.disabled = false; this.value = ""; }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "checked") this.checked = true; if (k === "disabled") this.disabled = true; if (k === "value") this.value = v; }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
  get textContent() { return this.children.map((c) => (c.nodeType === 3 ? c.data : c.textContent)).join(""); }
  fire(type) { if (this.disabled && type === "click") return Promise.resolve(); return Promise.all((this.handlers[type] || []).map((f) => f({ target: this }))); }
  all(pred, out = []) { if (pred(this)) out.push(this); for (const c of this.children) if (c.nodeType === 1) c.all(pred, out); return out; }
  buttons(label) { return this.all((e) => e.tag === "button" && e.textContent === label); }
  has(text) { return this.textContent.includes(text); }
}

/** A page with the script loaded; `fetches` is every call it made, `reply(path, body)` answers them, `extra` adds globals (a Companion, say). */
export function page(reply = () => [200, {}], extra = {}) {
  const fetches = [];
  const els = {};
  const replaced = [];
  const ctx = vm.createContext({
    document: { createElement: (t) => new El(t), createTextNode: (d) => ({ nodeType: 3, data: String(d) }), getElementById: (id) => (els[id] ||= new El("div")) },
    window: {}, location: { search: "", hash: "", origin: "https://app.example", pathname: "/app" }, history: { replaceState: (...a) => replaced.push(a) }, localStorage: { getItem: () => null, setItem() {} },
    URLSearchParams, console, setTimeout, crypto: { randomUUID: () => "x" }, confirm: () => true, Date, JSON, Number, Math, Object, Array, String, Set, Promise, Error, RegExp, encodeURIComponent,
    Companion: { hello: async () => ({ present: false }), paired: async () => false, status: async () => ({}), recommend: async () => ({}), cancel: async () => ({}) },
    fetch: async (url, init) => {
      const body = init?.body ? JSON.parse(init.body) : null;
      fetches.push({ url, body, headers: init?.headers });
      if (url === "/api/config") return { ok: false, status: 503, json: async () => ({ error: "not configured" }) };
      const [status, data] = reply(url, body);
      return { ok: status < 400, status, json: async () => data };
    },
    ...extra,
  });
  vm.runInContext(source, ctx);
  const run = (code) => vm.runInContext(code, ctx);
  run("render = () => {}; S.session = { access_token: 'tok', user: { id: 'u-1', email: 'owner@example.com' } }; S.current = { id: '" + BIZ + "', role: 'owner', name: 'Acme' }; S.team = { members: [], invitations: [] };");
  fetches.length = 0;
  const set = (name, value) => { ctx.__v = value; run(`S.${name} = __v`); };
  return { run, set, ctx, fetches, replaced, els, flush: () => new Promise((r) => setTimeout(r, 0)), state: () => run("S") };
}
