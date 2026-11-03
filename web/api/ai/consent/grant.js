import { ADMIN, route } from "../../_lib/route.js";
import { grantFields } from "../../_lib/consent_fields.js";

// Records the business's agreement to send its data to one provider, under the disclosure version that was shown. Owners and admins only; the engine service refuses a provider whose terms
// are unverified and a version that is not the current text, and records the signed-in person as the one who agreed.
export const POST = route({ roles: ADMIN, enginePath: "/v1/ai/consent/grant", perMinute: 10, pick: grantFields });
