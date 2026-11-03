import { json } from "../_lib/http.js";

export function GET() {
  return json({ ok: true });
}
