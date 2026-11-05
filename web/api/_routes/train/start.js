import { WRITE, route } from "../../_lib/route.js";

// The local one-command app trains MiniLM on its own files. The hosted engine does not fine-tune: here start and status both MEASURE how well the business's knowledge is found (hosted/measure.py).
export const POST = route({ roles: WRITE, enginePath: "/v1/train/start", timeoutMs: 30_000, perMinute: 30, pick: () => ({}) });
