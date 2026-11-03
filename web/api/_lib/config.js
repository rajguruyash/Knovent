// Settings come from environment variables set in Vercel (Project Settings > Environment Variables). Nothing here is ever sent to a browser except what `publicConfig` returns.
// SUPABASE_ANON_KEY is public by design (it is safe only because row level security guards the data). ENGINE_SERVICE_SECRET and ENGINE_URL are server-only.

/** @typedef {{ supabaseUrl: string, anonKey: string, engineUrl: string, engineSecret: string }} Config */

/**
 * @param {Record<string, string | undefined>} [env]
 * @returns {Config}
 */
export function readConfig(env = process.env) {
  const missing = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "ENGINE_URL", "ENGINE_SERVICE_SECRET"].filter((k) => !env[k]);
  if (missing.length) throw new ConfigError(missing);
  return {
    supabaseUrl: /** @type {string} */ (env.SUPABASE_URL).replace(/\/+$/, ""),
    anonKey: /** @type {string} */ (env.SUPABASE_ANON_KEY),
    engineUrl: /** @type {string} */ (env.ENGINE_URL).replace(/\/+$/, ""),
    engineSecret: /** @type {string} */ (env.ENGINE_SERVICE_SECRET),
  };
}

export class ConfigError extends Error {
  /** @param {string[]} missing */
  constructor(missing) {
    super("not configured");
    this.missing = missing; // names only; logged, never sent to the caller
  }
}

/** The only settings a browser may be given. */
export function publicConfig(env = process.env) {
  /** @param {unknown} u */
  const https = (u) => (typeof u === "string" && /^https:\/\/[^\s"'<>]+$/.test(u) ? u : "");
  /** @type {Record<string, any>} */
  let manifest = {};
  try { manifest = JSON.parse(env.COMPANION_DOWNLOADS || "{}"); } catch { manifest = {}; }  // {"macos-arm64": "https://...", "windows-x64": "https://..."}: where each installer is published
  /** @type {Record<string, string>} */
  const downloads = {};
  for (const k of ["macos-arm64", "macos-x64", "windows-x64"]) if (https(manifest[k])) downloads[k] = manifest[k];  // only systems an installer is built for; https only
  const legacy = https(env.COMPANION_DOWNLOAD_URL ?? "");
  if (legacy && !downloads["macos-arm64"]) downloads["macos-arm64"] = legacy;
  return { supabaseUrl: env.SUPABASE_URL ?? "", anonKey: env.SUPABASE_ANON_KEY ?? "", companionDownloadUrl: downloads["macos-arm64"] ?? "", companionDownloads: downloads };
}
