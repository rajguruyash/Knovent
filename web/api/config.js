import { publicConfig } from "./_lib/config.js";
import { json } from "./_lib/http.js";

// What the browser needs to talk to Supabase: the project address and the PUBLIC anon key (safe because row level security guards the data). Nothing else is ever returned here.
export function GET() {
  const c = publicConfig();
  if (!c.supabaseUrl || !c.anonKey) return json({ error: "This deployment is not configured yet." }, 503);
  return json(c, 200, { "cache-control": "public, max-age=300" });
}
