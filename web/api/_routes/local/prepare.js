import { READ, route } from "../../_lib/route.js";
import { localPrepareFields } from "../../_lib/ai_fields.js";

// Phase one of a question in local mode: the engine runs retrieval and validation and returns what the person's own companion must be given. Nothing is sent to any provider.
export const POST = route({ roles: READ, enginePath: "/v1/local/prepare", timeoutMs: 120_000, perMinute: 60, pick: localPrepareFields });
