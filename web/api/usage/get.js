import { ADMIN, route } from "../_lib/route.js";

// The plan, this month's use per mode, what is left of each limit, and the last thirty days by day. Owners and admins.
export const POST = route({ roles: ADMIN, enginePath: "/v1/usage/get", perMinute: 30, pick: () => ({}) });
