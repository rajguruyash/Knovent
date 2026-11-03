import { WRITE, route } from "../_lib/route.js";

// Train the business's MiniLM on its own files / read how that is going (the local one-command app only; the hosted engine does not train yet).
export const POST = route({ roles: WRITE, enginePath: "/v1/train/start", timeoutMs: 30_000, perMinute: 30, pick: () => ({}) });
