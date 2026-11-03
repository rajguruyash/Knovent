// What is served to a browser, and how it is configured. Run: npm test
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const walk = (dir) => readdirSync(dir).flatMap((f) => (statSync(join(dir, f)).isDirectory() ? walk(join(dir, f)) : [join(dir, f)]));

test("nothing served to a browser contains a secret or names one", () => {
  const files = walk(join(root, "public")).filter((f) => !f.includes("/vendor/"));
  assert.ok(files.length > 0, "there are no public files to check");
  const forbidden = [/service[_-]?role/i, /ENGINE_SERVICE_SECRET/, /ENGINE_URL/, /LLM_CREDENTIAL/, /DATABASE_URL/, /sk-[A-Za-z0-9_-]{16,}/, /sk-ant-/, /AIza[0-9A-Za-z_-]{30,}/, /eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\./];
  for (const f of files) {
    const text = readFileSync(f, "utf8");
    for (const rx of forbidden) assert.ok(!rx.test(text), `${f} matches ${rx}`);
  }
});

test("the page sends no inline script, loads no third-party script, and never stores a key", () => {
  for (const f of walk(join(root, "public")).filter((f) => f.endsWith(".html"))) {
    const html = readFileSync(f, "utf8");
    assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>[^<]/i.test(html), `${f} has an inline script`);
    for (const m of html.matchAll(/<script[^>]*\bsrc="([^"]+)"/gi)) assert.ok(m[1].startsWith("/"), `${f} loads a script from ${m[1]}`);
    assert.ok(!/\bon[a-z]+\s*=\s*"/i.test(html), `${f} has an inline event handler`);
  }
  for (const f of walk(join(root, "public")).filter((f) => f.endsWith(".js") && !f.includes("/vendor/"))) {
    const js = readFileSync(f, "utf8");
    assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(/.test(js), `${f} writes markup from strings`);
    assert.ok(!/(localStorage|sessionStorage)[^;\n]*(api_?key|apiKey)/i.test(js), `${f} stores an API key in the browser`);
  }
});

test("vercel.json sets the security headers and long limits only where they are needed", () => {
  const v = JSON.parse(readFileSync(join(root, "vercel.json"), "utf8"));
  const all = Object.fromEntries(v.headers.find((h) => h.source === "/(.*)").headers.map((h) => [h.key, h.value]));
  for (const k of ["Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Content-Security-Policy", "Permissions-Policy"]) assert.ok(all[k], `${k} is missing`);
  assert.match(all["Content-Security-Policy"], /script-src 'self'(?!.*unsafe)/);
  assert.match(all["Content-Security-Policy"], /frame-ancestors 'none'/);
  assert.ok(!/unsafe-inline|unsafe-eval/.test(all["Content-Security-Policy"].replace(/style-src[^;]*;/, "")));
  assert.ok(v.headers.some((h) => h.source === "/api/(.*)" && h.headers.some((x) => x.key === "Cache-Control" && x.value === "no-store")));
  assert.ok(v.functions["api/sources/ingest.js"].maxDuration <= 300); // inside Vercel's limit on every plan
});

test("the API functions import nothing but their own helpers (no run-time dependencies)", () => {
  for (const f of walk(join(root, "api"))) {
    for (const m of readFileSync(f, "utf8").matchAll(/^import .* from "([^"]+)"/gm)) assert.ok(m[1].startsWith("."), `${f} imports ${m[1]}`);
  }
  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  assert.deepEqual(Object.keys(pkg.dependencies), ["@supabase/supabase-js"]); // used by the browser app only, copied into public/vendor by the build
});

test("every class the page puts on a wide table's wrapper has a style that makes it scroll inside itself, so a phone's page never scrolls sideways", () => {
  const css = readFileSync(join(root, "public", "site.css"), "utf8");
  const js = readFileSync(join(root, "public", "app.js"), "utf8");
  assert.ok(/h\('div', \{ class: 'scroll' \}, h\('table'/.test(js), "the page wraps its tables in .scroll");
  assert.match(css, /\.scroll\s*\{[^}]*overflow-x:\s*auto/);
});

test("nothing the page serves carries an inline style attribute, and the script never sets one as an attribute: the CSP forbids both, so every style is a class or goes through the style API", () => {
  for (const f of ["index.html", "app.html"]) assert.ok(!/\sstyle\s*=/i.test(readFileSync(join(root, "public", f), "utf8")), `${f} has an inline style attribute`);
  const js = readFileSync(join(root, "public", "app.js"), "utf8");
  assert.match(js, /k === 'style' && el\.style\) el\.style\.cssText = v/); // style goes through the style API
  assert.ok(!/setAttribute\(\s*['"]style['"]/.test(js));
  const vercel = JSON.parse(readFileSync(join(root, "vercel.json"), "utf8"));
  const csp = vercel.headers.flatMap((h) => h.headers).find((h) => h.key === "Content-Security-Policy").value;
  assert.match(csp, /style-src 'self' https:\/\/fonts\.googleapis\.com/);
  assert.ok(!/unsafe-inline/.test(csp));
});

test("the stylesheet and the pages load nothing from another site (fonts, images, scripts, styles)", () => {
  const css = readFileSync(join(root, "public", "site.css"), "utf8");
  assert.ok(!/@import|url\(\s*['"]?https?:/i.test(css));
  for (const f of ["index.html", "app.html"]) {
    const html = readFileSync(join(root, "public", f), "utf8");
    const other = [...html.matchAll(/(?:src|href)=["'](https?:)?\/\/[^"']+/gi)].map((m) => m[0]).filter((u) => !/fonts\.(googleapis|gstatic)\.com/.test(u)); // the one typeface (Geist) comes from Google Fonts, which the CSP names
    assert.deepEqual(other, [], `${f} refers to another site`);
  }
});
