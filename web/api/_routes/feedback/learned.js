import { READ, route } from "../../_lib/route.js";

// The questions people approved while testing, newest first. Any member.
export const POST = route({ roles: READ, enginePath: "/v1/feedback/learned", perMinute: 30, pick: () => ({}) });
