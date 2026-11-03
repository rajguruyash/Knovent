// Copies the one browser library the app needs (Supabase's client) out of node_modules into public/vendor, so the page loads it from its own address (the security policy allows scripts
// from 'self' only) and nothing is fetched from a third-party CDN at run time.
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const from = join(root, "node_modules", "@supabase", "supabase-js", "dist", "umd", "supabase.js");
if (!existsSync(from)) {
  console.error("Run `npm install` first: @supabase/supabase-js is missing.");
  process.exit(1);
}
mkdirSync(join(root, "public", "vendor"), { recursive: true });
copyFileSync(from, join(root, "public", "vendor", "supabase.js"));
console.log("copied supabase.js to public/vendor");
