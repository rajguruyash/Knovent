import { READ, route } from "../../_lib/route.js";

// Where a business stands with every provider: terms verified or not, agreed to the current text or not, and the history. Any member may read.
export const POST = route({ roles: READ, enginePath: "/v1/ai/consent/get", perMinute: 60, pick: () => ({}) });
