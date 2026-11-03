import { ADMIN, route } from "../../_lib/route.js";
import { settingsFields } from "../../_lib/ai_fields.js";

// Chooses how the business answers. Owners and admins. The engine service checks the choice against what exists (a mode the operator switched on, a model that was released, a provider the business set up).
export const POST = route({ roles: ADMIN, enginePath: "/v1/ai/settings/save", perMinute: 10, pick: settingsFields });
