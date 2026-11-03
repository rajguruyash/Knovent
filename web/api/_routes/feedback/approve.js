import { WRITE, route } from "../../_lib/route.js";
import { approveFields } from "../../_lib/ai_fields.js";

// "This question is answered by this FAQ": stored as one more way of asking it, searched from the next question on. Owners, admins and editors.
export const POST = route({ roles: WRITE, enginePath: "/v1/feedback/approve", timeoutMs: 20_000, perMinute: 30, pick: approveFields });
