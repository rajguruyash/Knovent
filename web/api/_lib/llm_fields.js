import { HttpError } from "./http.js";

const PROVIDERS = ["openai", "anthropic", "gemini", "openrouter", "openai_compatible"];

/** The model-settings fields that may be forwarded, checked. The key is forwarded once and never echoed. @param {Record<string, any>} b */
export function llmFields(b) {
  if (!PROVIDERS.includes(b.provider)) throw new HttpError(400, "Choose a supported provider.");
  for (const k of ["model", "base_url", "api_key"]) if (b[k] !== undefined && (typeof b[k] !== "string" || b[k].length > 400)) throw new HttpError(400, "A field is not valid.");
  if (b.enabled !== undefined && typeof b.enabled !== "boolean") throw new HttpError(400, "A field is not valid.");
  if (b.capabilities !== undefined && (typeof b.capabilities !== "object" || b.capabilities === null || Object.values(b.capabilities).some((v) => typeof v !== "boolean"))) throw new HttpError(400, "A field is not valid.");
  return { provider: b.provider, model: b.model, base_url: b.base_url, api_key: b.api_key, enabled: b.enabled, capabilities: b.capabilities };
}
