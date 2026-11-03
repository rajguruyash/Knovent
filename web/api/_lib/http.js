// Small helpers shared by every function: JSON answers with safe headers, strict input reading, and a best-effort rate limit.

export const MAX_BODY_BYTES = 256 * 1024; // far below Vercel's 4.5 MB limit; questions and settings are small
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** @param {unknown} v */
export const isUuid = (v) => typeof v === "string" && UUID.test(v);

export class HttpError extends Error {
  /**
   * @param {number} status
   * @param {string} message
   * @param {Record<string, string>} [headers]
   */
  constructor(status, message, headers = {}) {
    super(message);
    this.status = status;
    this.headers = headers;
  }
}

/**
 * @param {unknown} body
 * @param {number} [status]
 * @param {Record<string, string>} [extra]
 */
export function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff", ...extra },
  });
}

/**
 * The JSON object in a request body, refusing anything else or anything large.
 * @param {Request} request
 * @returns {Promise<Record<string, any>>}
 */
export async function readJson(request) {
  const type = request.headers.get("content-type") ?? "";
  if (!type.includes("application/json")) throw new HttpError(415, "Send JSON.");
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_BODY_BYTES) throw new HttpError(413, "That request is too large.");
  const text = await request.text();
  if (new TextEncoder().encode(text).length > MAX_BODY_BYTES) throw new HttpError(413, "That request is too large.");
  try {
    const value = JSON.parse(text);
    if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("not an object");
    return value;
  } catch {
    throw new HttpError(400, "The request is not a JSON object.");
  }
}

/** @param {Request} request @param {string} method */
export function requireMethod(request, method) {
  if (request.method !== method) throw new HttpError(405, `Use ${method}.`, { allow: method });
}

/**
 * A fixed-window limit per key, kept in the memory of one function instance. Serverless instances come and go, so this slows a single client down; it is not a quota.
 * For a real limit use Vercel's firewall rate limiting (see docs/vercel-deployment.md); the engine service also limits each business.
 * @param {string} key
 * @param {number} limit
 * @param {number} [windowMs]
 * @param {number} [now]
 */
export function rateLimit(key, limit, windowMs = 60_000, now = Date.now()) {
  const hit = buckets.get(key);
  if (!hit || now - hit.start >= windowMs) {
    if (buckets.size > 5000) buckets.clear();
    buckets.set(key, { start: now, n: 1 });
    return;
  }
  hit.n += 1;
  if (hit.n > limit) throw new HttpError(429, "Too many requests. Try again in a moment.", { "retry-after": String(Math.max(1, Math.ceil((windowMs - (now - hit.start)) / 1000))) });
}
/** @type {Map<string, { start: number, n: number }>} */
const buckets = new Map();
export function resetRateLimits() {
  buckets.clear();
}

/**
 * Turns any thrown value into a response: our own errors as they are, everything else as a plain message, with the detail kept in the server log (never the request, never a header).
 * @param {unknown} error
 */
export function failure(error) {
  if (error instanceof HttpError) return json({ error: error.message }, error.status, error.headers);
  console.error("api error:", error instanceof Error ? error.name : typeof error, error instanceof Error && "missing" in error ? `missing settings: ${/** @type {any} */ (error).missing.join(", ")}` : "");
  return json({ error: "Something went wrong on the server." }, 500);
}
