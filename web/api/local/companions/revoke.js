import { ADMIN, route } from "../../_lib/route.js";
import { companionIdField } from "../../_lib/ai_fields.js";

// Stops the business using a companion. Owners and admins. (The browser also removes the pairing from the companion itself.)
export const POST = route({ roles: ADMIN, enginePath: "/v1/local/companions/revoke", perMinute: 10, pick: companionIdField });
