// Parses every inline <script> of the dashboard page, so a syntax error can never reach the live site.
const fs = require("fs");
const page = fs.readFileSync(process.argv[2], "utf8");
const scripts = [...page.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
let bad = 0;
scripts.forEach((s, i) => { try { new Function(s); } catch (e) { bad++; console.error(`script ${i}: ${e.message}`); } });
const ext = [...page.matchAll(/<script[^>]*\bsrc="([^"]+)"[^>]*>/g)];
ext.forEach(m => { if (!/integrity="sha384-/.test(m[0])) { bad++; console.error("external script without SRI: " + m[1]); } });
if (bad) process.exit(1);
console.log(`ok: ${scripts.length} inline script(s), ${ext.length} external script(s) pinned with SRI`);
