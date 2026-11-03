import { READ, route } from "../../_lib/route.js";
import { providerField } from "../../_lib/consent_fields.js";

// The exact disclosure text for one provider, its version and whether its terms are verified. Any member may read it; it is the text an agreement is bound to.
export const POST = route({ roles: READ, enginePath: "/v1/ai/disclosure", perMinute: 30, pick: (b) => ({ provider: providerField(b) }) });
