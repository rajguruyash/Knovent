import { READ, route } from "../../_lib/route.js";

// How a business answers (deterministic, platform, its own provider, its own computer), whether that is usable right now and why not, and what can be chosen. Any member may read.
export const POST = route({ roles: READ, enginePath: "/v1/ai/settings/get", perMinute: 60, pick: () => ({}) });
