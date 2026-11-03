// ONE serverless function for the whole API. Vercel's free plan allows twelve functions per deployment and this API has twenty three endpoints, so the endpoints live in `_routes/` (the underscore
// keeps them from being deployed one by one) and this file hands each request to the same handler it would have reached before. Addresses, methods, checks and behaviour are unchanged: every
// handler still builds its own response, and `route()` still runs method, configuration, sign-in, role, input and rate checks for every authenticated endpoint.
import { json } from "./_lib/http.js";
import * as r0 from "./_routes/ai/consent/get.js";
import * as r1 from "./_routes/ai/consent/grant.js";
import * as r2 from "./_routes/ai/consent/revoke.js";
import * as r3 from "./_routes/ai/disclosure.js";
import * as r4 from "./_routes/ai/settings/get.js";
import * as r5 from "./_routes/ai/settings/save.js";
import * as r6 from "./_routes/config.js";
import * as r7 from "./_routes/health.js";
import * as r8 from "./_routes/history.js";
import * as r9 from "./_routes/llm/get.js";
import * as r10 from "./_routes/llm/remove.js";
import * as r11 from "./_routes/llm/save.js";
import * as r12 from "./_routes/llm/validate.js";
import * as r13 from "./_routes/local/companions/get.js";
import * as r14 from "./_routes/local/companions/register.js";
import * as r15 from "./_routes/local/companions/revoke.js";
import * as r16 from "./_routes/local/complete.js";
import * as r17 from "./_routes/local/prepare.js";
import * as r18 from "./_routes/respond.js";
import * as r19 from "./_routes/sources/ingest.js";
import * as r20 from "./_routes/train/start.js";
import * as r21 from "./_routes/train/status.js";
import * as r22 from "./_routes/usage/get.js";

/** @type {Record<string, Record<string, any>>} */
const ROUTES = {
  "/api/ai/consent/get": r0,
  "/api/ai/consent/grant": r1,
  "/api/ai/consent/revoke": r2,
  "/api/ai/disclosure": r3,
  "/api/ai/settings/get": r4,
  "/api/ai/settings/save": r5,
  "/api/config": r6,
  "/api/health": r7,
  "/api/history": r8,
  "/api/llm/get": r9,
  "/api/llm/remove": r10,
  "/api/llm/save": r11,
  "/api/llm/validate": r12,
  "/api/local/companions/get": r13,
  "/api/local/companions/register": r14,
  "/api/local/companions/revoke": r15,
  "/api/local/complete": r16,
  "/api/local/prepare": r17,
  "/api/respond": r18,
  "/api/sources/ingest": r19,
  "/api/train/start": r20,
  "/api/train/status": r21,
  "/api/usage/get": r22,
};

/** @param {Request} request */
async function dispatch(request) {
  const path = new URL(request.url).pathname.replace(/\/+$/, "");
  const mod = Object.hasOwn(ROUTES, path) ? ROUTES[path] : undefined;
  if (!mod) return json({ error: "Not found." }, 404);
  const handler = mod[request.method];
  if (typeof handler !== "function") return json({ error: "That method is not allowed here." }, 405);
  return handler(request);
}

export const GET = dispatch;
export const POST = dispatch;
