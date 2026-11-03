import { ADMIN, route } from "../../_lib/route.js";
import { companionFields } from "../../_lib/ai_fields.js";

// Records that a companion was paired in the browser. The pairing secret never comes here. Owners and admins.
export const POST = route({ roles: ADMIN, enginePath: "/v1/local/companions/register", perMinute: 10, pick: companionFields });
