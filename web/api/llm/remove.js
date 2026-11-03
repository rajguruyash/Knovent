import { ADMIN, route } from "../_lib/route.js";

// Forgets the key and turns the model off. Owners and admins.
export const POST = route({ roles: ADMIN, enginePath: "/v1/llm/remove", perMinute: 10, pick: () => ({}) });
