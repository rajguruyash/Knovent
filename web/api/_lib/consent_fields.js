import { HttpError } from "./http.js";

// A provider is named by a short key (cloudflare, gemini ...): never an address, never a key. The engine service checks it against the providers that have a disclosure.
const PROVIDER = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const VERSION = /^[0-9A-Za-z._-]{1,80}$/;

/** @param {Record<string, any>} b */
export function providerField(b) {
  if (typeof b.provider !== "string" || !PROVIDER.test(b.provider)) throw new HttpError(400, "Choose a provider.");
  return b.provider;
}

/**
 * What an agreement can carry from the browser: the provider, and the version of the disclosure text that was SHOWN. Nothing else is forwarded (no "agreed" flag, no role, no user, no business
 * other than the one the person was checked against), so the page cannot say that someone consented: the engine service records who the signed-in person is and compares the version with the
 * current text itself.
 * @param {Record<string, any>} b
 */
export function grantFields(b) {
  if (typeof b.disclosure_version !== "string" || !VERSION.test(b.disclosure_version)) throw new HttpError(400, "The disclosure version is not valid.");
  return { provider: providerField(b), disclosure_version: b.disclosure_version };
}
