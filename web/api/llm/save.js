import { ADMIN, route } from "../_lib/route.js";
import { llmFields } from "../_lib/llm_fields.js";

// Saves the provider, model and (encrypted, on the engine service) key; turns the model on or off. Owners and admins.
export const POST = route({ roles: ADMIN, enginePath: "/v1/llm/save", timeoutMs: 40_000, perMinute: 10, pick: llmFields });
