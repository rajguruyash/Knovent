import { READ, route } from "../../_lib/route.js";
import { localCompleteFields } from "../../_lib/ai_fields.js";

// Phase two: what the companion returned (or why it could not). The engine treats it as untrusted text and verifies every claim against the evidence it kept for the request.
export const POST = route({ roles: READ, enginePath: "/v1/local/complete", timeoutMs: 40_000, perMinute: 60, pick: localCompleteFields });
