import { WRITE, route, text } from "../../_lib/route.js";

// Read an uploaded file or a web page into a business's knowledge (the file is already in Storage; the person's own upload is what Storage's policies allowed). Editors and above.
export const POST = route({ roles: WRITE, enginePath: "/v1/sources/ingest", timeoutMs: 120_000, perMinute: 10, pick: (b) => ({ source_id: text(b.source_id, 40) }) });
