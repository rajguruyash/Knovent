import { READ, route, text } from "./_lib/route.js";

// Ask a business's knowledge engine a question. Any member may.
export const POST = route({
  roles: READ,
  enginePath: "/v1/respond",
  timeoutMs: 120_000,
  perMinute: 60,
  pick: (b) => ({ query: text(b.query, 1000) ?? "", conversation_id: text(b.conversation_id, 40) }),
});
