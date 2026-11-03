// One function builds every authenticated endpoint, so that none of them can forget a step: method, configuration, sign-in, role in THIS business, input whitelist, rate limit, engine call.
import { ADMIN, READ, WRITE, authenticate, requireRole } from "./auth.js";
import { readConfig } from "./config.js";
import { callEngine } from "./engine.js";
import { HttpError, failure, json, rateLimit, readJson, requireMethod } from "./http.js";

export { ADMIN, READ, WRITE };

/**
 * @param {{ roles: string[], enginePath: string, timeoutMs?: number, perMinute?: number,
 *           pick: (body: Record<string, any>) => Record<string, unknown> }} spec
 * `pick` names the fields that may reach the engine: nothing else in the body is forwarded.
 */
export function route(spec) {
  /**
   * @param {Request} request
   * @param {{ config?: import("./config.js").Config, fetchImpl?: typeof fetch }} [deps] for tests; in production both come from the environment
   */
  return async function handler(request, deps = {}) {
    try {
      requireMethod(request, "POST");
      const config = deps.config ?? readConfig();
      const fetchImpl = deps.fetchImpl ?? fetch;
      const who = await authenticate(request, config, fetchImpl);
      rateLimit(`${who.userId}:${spec.enginePath}`, spec.perMinute ?? 30);
      const body = await readJson(request);
      await requireRole(who, body.business_id, spec.roles, config, fetchImpl);
      const data = await callEngine(config, spec.enginePath, { ...spec.pick(body), business_id: body.business_id, user_id: who.userId }, { timeoutMs: spec.timeoutMs, fetchImpl });
      return json(data);
    } catch (e) {
      return failure(e);
    }
  };
}

/** @param {unknown} v @param {number} max */
export function text(v, max) {
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "string" || v.length > max) throw new HttpError(400, "A field is not valid.");
  return v;
}
