import { READ, route } from "../../_lib/route.js";

// The language-model settings of a business (never the key). Any member may read.
export const POST = route({ roles: READ, enginePath: "/v1/llm/get", pick: () => ({}) });
