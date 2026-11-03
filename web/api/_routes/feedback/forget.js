import { WRITE, route } from "../../_lib/route.js";
import { learnedIdField } from "../../_lib/ai_fields.js";

// Takes one learned question back out of retrieval. Owners, admins and editors.
export const POST = route({ roles: WRITE, enginePath: "/v1/feedback/forget", perMinute: 30, pick: learnedIdField });
