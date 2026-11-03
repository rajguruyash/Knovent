import { READ, route } from "../../../_lib/route.js";

// The companions this business has paired (names and what they run; never a secret). Any member.
export const POST = route({ roles: READ, enginePath: "/v1/local/companions/get", perMinute: 60, pick: () => ({}) });
