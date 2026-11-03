import { HttpError, isUuid } from "./http.js";

// What the browser may hand to the engine for the answer-mode, usage, history and local-mode functions, each checked for shape. The engine service validates the VALUES (a mode that is switched on, a model that was
// released, a companion that exists); nothing here is trusted to mean what it says, and nothing the browser sends can name a person, a role or a business other than the one it was checked against.
const MODES = ["deterministic", "platform", "byok", "local"];
const FALLBACKS = ["deterministic", "none"];
const POLICIES = ["deterministic_only", "local_only", "local_free", "local_free_paid", "byok_preferred", "byok_alternative"];
const FLAGS = ["fallback_byok", "fallback_free", "fallback_paid"];
const FITS = ["comfortable", "possible", "not_advised", "unknown"];
const ANSWER_STYLES = ["plain", "chat"];
const KEY = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const MODEL = /^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$/;
const FAILURES = ["network", "timeout", "not_found", "server", "cancelled", "not_connected", "auth", "bad_request"];
const MAX_LOCAL_TEXT = 150_000;

/** @param {Record<string, any>} b */
export function settingsFields(b) {
  // either a named choice from the setup screen (`policy`), or a first step (`mode`); the steps after it are on or off, and the paid step has the business's own monthly spending limit
  if (b.policy !== undefined && !POLICIES.includes(b.policy)) throw new HttpError(400, "Choose how this business answers.");
  if (b.policy === undefined && !MODES.includes(b.mode)) throw new HttpError(400, "Choose how this business answers.");
  if (b.policy !== undefined && b.mode !== undefined && !MODES.includes(b.mode)) throw new HttpError(400, "Choose how this business answers.");
  for (const f of FLAGS) if (b[f] !== undefined && typeof b[f] !== "boolean") throw new HttpError(400, "A field is not valid.");
  if (b.paid_cap_usd !== undefined && (typeof b.paid_cap_usd !== "number" || !Number.isFinite(b.paid_cap_usd) || b.paid_cap_usd <= 0 || b.paid_cap_usd > 1_000_000)) throw new HttpError(400, "A field is not valid.");
  if (b.fallback !== undefined && !FALLBACKS.includes(b.fallback)) throw new HttpError(400, "A field is not valid.");
  for (const k of ["platform_provider", "platform_model"]) if (b[k] !== undefined && (typeof b[k] !== "string" || !(k === "platform_provider" ? KEY : MODEL).test(b[k]))) throw new HttpError(400, "A field is not valid.");
  if (b.answer_style !== undefined && !ANSWER_STYLES.includes(b.answer_style)) throw new HttpError(400, "A field is not valid.");
  return { answer_style: b.answer_style, policy: b.policy, mode: b.mode, fallback: b.fallback, platform_provider: b.platform_provider, platform_model: b.platform_model, fallback_byok: b.fallback_byok, fallback_free: b.fallback_free,
    fallback_paid: b.fallback_paid, paid_cap_usd: b.paid_cap_usd };
}

/** @param {Record<string, any>} b */
export function historyFields(b) {
  if (b.limit !== undefined && (!Number.isInteger(b.limit) || b.limit < 1 || b.limit > 200)) throw new HttpError(400, "A field is not valid.");
  if (b.before !== undefined && (typeof b.before !== "string" || b.before.length > 40 || !/^[0-9T:.+\-Z ]+$/.test(b.before))) throw new HttpError(400, "A field is not valid.");
  return { limit: b.limit, before: b.before };
}

/** The question for phase one of a local-mode answer. @param {Record<string, any>} b */
export function localPrepareFields(b) {
  if (typeof b.query !== "string" || b.query.length < 1 || b.query.length > 1000) throw new HttpError(400, "A field is not valid.");
  if (b.conversation_id !== undefined && (typeof b.conversation_id !== "string" || b.conversation_id.length > 40)) throw new HttpError(400, "A field is not valid.");
  return { query: b.query, conversation_id: b.conversation_id };
}

/** What the person's own companion returned, for phase two. It is checked here for shape only: the engine verifies it against the evidence it kept. @param {Record<string, any>} b */
export function localCompleteFields(b) {
  if (typeof b.request_id !== "string" || !/^[0-9a-f]{48}$/.test(b.request_id)) throw new HttpError(400, "A field is not valid.");
  if (b.text !== undefined && b.text !== null && (typeof b.text !== "string" || b.text.length > MAX_LOCAL_TEXT)) throw new HttpError(400, "A field is not valid.");
  if (b.failure !== undefined && b.failure !== null && !FAILURES.includes(b.failure)) throw new HttpError(400, "A field is not valid.");
  for (const k of ["tokens_in", "tokens_out"]) if (b[k] !== undefined && (!Number.isInteger(b[k]) || b[k] < 0 || b[k] > 10_000_000)) throw new HttpError(400, "A field is not valid.");
  return { request_id: b.request_id, text: b.text ?? undefined, failure: b.failure ?? undefined, tokens_in: b.tokens_in, tokens_out: b.tokens_out };
}

/** A companion the business pairs: what it is, never a secret (the pairing secret stays in the person's browser and on their machine). @param {Record<string, any>} b */
export function companionFields(b) {
  if (typeof b.label !== "string" || b.label.trim().length < 1 || b.label.length > 80 || /[\u0000-\u001f\u007f]/.test(b.label)) throw new HttpError(400, "Give the companion a short name.");
  if (b.platform !== undefined && (typeof b.platform !== "string" || b.platform.length > 80 || /[\u0000-\u001f\u007f]/.test(b.platform))) throw new HttpError(400, "A field is not valid.");
  if (!["ollama", "llamacpp"].includes(b.runtime) || typeof b.model !== "string" || !MODEL.test(b.model)) throw new HttpError(400, "Say which runtime and model it runs.");
  if (b.fit !== undefined && !FITS.includes(b.fit)) throw new HttpError(400, "A field is not valid.");
  return { label: b.label.trim(), platform: b.platform, runtime: b.runtime, model: b.model, fit: b.fit };
}

/** @param {Record<string, any>} b */
export function companionIdField(b) {
  if (!isUuid(b.companion_id)) throw new HttpError(400, "That is not a companion id.");
  return { companion_id: b.companion_id };
}

/** @param {Record<string, any>} b */
export function approveFields(b) {
  if (!isUuid(b.faq_id)) throw new HttpError(400, "Choose the FAQ this question belongs to.");
  if (typeof b.question !== "string" || !b.question.trim() || b.question.length > 300) throw new HttpError(400, "A question must be 1 to 300 characters.");
  return { faq_id: b.faq_id, question: b.question };
}

/** @param {Record<string, any>} b */
export function learnedIdField(b) {
  if (!isUuid(b.learned_id)) throw new HttpError(400, "A field is not valid.");
  return { learned_id: b.learned_id };
}
