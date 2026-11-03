import { ADMIN, route } from "../../_lib/route.js";
import { providerField } from "../../_lib/consent_fields.js";

// Withdraws the agreement. Owners and admins only. Always possible: whether the provider's terms are verified makes no difference to the right to stop.
export const POST = route({ roles: ADMIN, enginePath: "/v1/ai/consent/revoke", perMinute: 10, pick: (b) => ({ provider: providerField(b) }) });
