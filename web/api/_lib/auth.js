// Who is asking, and may they do this in that business? Two checks, both against Supabase with the PERSON'S OWN token:
//   1. /auth/v1/user  confirms the token is real and unexpired and gives the user id (the one authoritative check; the token is never decoded locally and trusted)
//   2. business_members, read with the person's token, so row level security answers: a person can read a membership row only for a business they belong to
// The engine service then checks the role again in the database. A business id from the browser is never trusted on its own.
import { HttpError, isUuid } from "./http.js";

/** @typedef {import("./config.js").Config} Config */
/** @typedef {typeof fetch} Fetch */

/** @param {Request} request */
export function bearer(request) {
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+([A-Za-z0-9._~+/-]+=*)$/.exec(header);
  if (!match) throw new HttpError(401, "Sign in first.");
  return match[1];
}

/**
 * @param {Request} request
 * @param {Config} config
 * @param {Fetch} [fetchImpl]
 * @returns {Promise<{ userId: string, token: string }>}
 */
export async function authenticate(request, config, fetchImpl = fetch) {
  const token = bearer(request);
  let res;
  try {
    res = await fetchImpl(`${config.supabaseUrl}/auth/v1/user`, { headers: { apikey: config.anonKey, authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(8000) });
  } catch {
    throw new HttpError(503, "Sign-in is not reachable right now.");
  }
  if (res.status === 401 || res.status === 403) throw new HttpError(401, "Your session has expired. Sign in again.");
  if (!res.ok) throw new HttpError(503, "Sign-in is not reachable right now.");
  const user = /** @type {any} */ (await res.json());
  if (!isUuid(user?.id)) throw new HttpError(401, "Sign in first.");
  return { userId: user.id, token };
}

/**
 * The person's role in a business, or a refusal. 403 is the same whether the business does not exist or the person is not in it.
 * @param {{ userId: string, token: string }} who
 * @param {unknown} businessId
 * @param {string[]} allowed
 * @param {Config} config
 * @param {Fetch} [fetchImpl]
 */
export async function requireRole(who, businessId, allowed, config, fetchImpl = fetch) {
  if (!isUuid(businessId)) throw new HttpError(400, "That is not a business id.");
  const url = `${config.supabaseUrl}/rest/v1/business_members?business_id=eq.${businessId}&user_id=eq.${who.userId}&select=role`;
  let res;
  try {
    res = await fetchImpl(url, { headers: { apikey: config.anonKey, authorization: `Bearer ${who.token}`, accept: "application/json" }, signal: AbortSignal.timeout(8000) });
  } catch {
    throw new HttpError(503, "The database is not reachable right now.");
  }
  if (!res.ok) throw new HttpError(res.status === 401 ? 401 : 503, res.status === 401 ? "Your session has expired. Sign in again." : "The database is not reachable right now.");
  const rows = /** @type {{ role: string }[]} */ (await res.json());
  const role = rows[0]?.role;
  if (!role) throw new HttpError(403, "You do not have access to this business.");
  if (!allowed.includes(role)) throw new HttpError(403, "Your role does not allow this.");
  return role;
}

export const READ = ["owner", "admin", "editor", "viewer"];
export const WRITE = ["owner", "admin", "editor"];
export const ADMIN = ["owner", "admin"];
