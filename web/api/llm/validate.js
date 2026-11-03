import { ADMIN, route } from "../_lib/route.js";
import { llmFields } from "../_lib/llm_fields.js";

// Checks a provider, key and model without saving anything. Owners and admins.
export const POST = route({ roles: ADMIN, enginePath: "/v1/llm/validate", timeoutMs: 40_000, perMinute: 10, pick: llmFields });
