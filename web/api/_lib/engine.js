// Calls the engine service (the always-on Python process) with the shared secret. The secret, the service's address and its error details never reach the browser.
import { HttpError } from "./http.js";

/** @typedef {import("./config.js").Config} Config */

/**
 * @param {Config} config
 * @param {string} path
 * @param {Record<string, unknown>} body
 * @param {{ timeoutMs?: number, fetchImpl?: typeof fetch }} [options]
 */
export async function callEngine(config, path, body, { timeoutMs = 25_000, fetchImpl = fetch } = {}) {
  let res;
  try {
    res = await fetchImpl(`${config.engineUrl}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${config.engineSecret}` },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (e) {
    const timedOut = e instanceof Error && (e.name === "TimeoutError" || e.name === "AbortError");
    throw new HttpError(504, timedOut ? "The knowledge engine took too long to answer." : "The knowledge engine is not available right now.");
  }
  /** @type {any} */
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* not JSON */
  }
  if (res.ok) return data;
  const message = typeof data?.error === "string" ? data.error : "";
  if (res.status === 429) throw new HttpError(429, message || "Too many questions in a minute.", { "retry-after": res.headers.get("retry-after") ?? "30" });
  if ([400, 403, 404, 409, 410, 413, 422].includes(res.status)) throw new HttpError(res.status, message || "The request was refused.");
  if (res.status === 401) throw new HttpError(502, "The knowledge engine is not configured correctly."); // our own secret was refused: a setup problem, not the person's
  throw new HttpError(502, "The knowledge engine is not available right now.");
}
