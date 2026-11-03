import { READ, route } from "./_lib/route.js";
import { historyFields } from "./_lib/ai_fields.js";

// The latest answers of the business, each with who generated it (the deterministic engine, a model, which provider, in which mode). Any member.
export const POST = route({ roles: READ, enginePath: "/v1/history", perMinute: 60, pick: historyFields });
