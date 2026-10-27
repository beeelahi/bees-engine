// Browser smoke test of the dashboard (CI): opens every tab, section and chart view of a synthetic data package
// (tools/site_smoke_data.py) on a desktop and a phone screen, and fails on any JavaScript error, an empty view,
// or a page wider than the screen.   node tools/site_smoke.js SITE_DIR
const http = require("http"), fs = require("fs"), path = require("path");
const { chromium } = require("playwright");
const dir = path.resolve(process.argv[2] || "site");
const TYPES = { ".html": "text/html", ".json": "application/json", ".js": "text/javascript" };
const server = http.createServer((q, r) => { const u = decodeURIComponent(q.url.split("?")[0]); const f = path.join(dir, u === "/" ? "index.html" : u);
  if (!f.startsWith(dir) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { r.writeHead(404); return r.end(); }
  r.writeHead(200, { "Content-Type": TYPES[path.extname(f)] || "application/octet-stream" }); fs.createReadStream(f).pipe(r); });
const ROUTES = ["overview", "highlights", "highlights/shortlist", "highlights/calls", "highlights/breakouts", "highlights/patterns", "highlights/moves",
  "highlights/ideas", "highlights/channels", "highlights/bots", "bot", "bot/each", "bot/record", "bot/fade", "signals", "signals/library", "screener", "watchlist",
  "listings", "lab", "pair/BTCUSDT/1d", "pair/BTCUSDT/1w", "pair/BTCUSDT/1M", "pair/BTCUSDT/4h", "pair/BTCUSDT/1h", "pair/ZIGUSDT/1d", "me",
  "top50", "record", "library", "shortlist"];
(async () => {
  await new Promise(ok => server.listen(0, "127.0.0.1", ok));
  const base = "http://127.0.0.1:" + server.address().port + "/";
  const b = await chromium.launch(); const fails = [];
  for (const vp of [{ width: 1300, height: 900, n: "desktop" }, { width: 390, height: 844, n: "phone" }]) {
    const p = await b.newPage({ viewport: vp }); let errs = [];
    p.on("pageerror", e => errs.push("pageerror: " + e.message));
    p.on("console", m => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errs.push("console: " + m.text()); });
    await p.goto(base + "#overview", { waitUntil: "networkidle" }); await p.waitForTimeout(800);
    for (const r of ROUTES) {
      errs = [];
      await p.evaluate(h => { location.hash = h; }, r); await p.waitForTimeout(r.startsWith("pair") ? 900 : 450);
      const st = await p.evaluate(() => { const v = [...document.querySelectorAll("section.view")].filter(s => !s.hidden);
        return { views: v.length, text: v[0] ? v[0].innerText.trim().length : 0, over: document.documentElement.scrollWidth - window.innerWidth }; });
      const bad = [...errs]; if (st.views !== 1) bad.push(`${st.views} views visible`); if (st.text < 40) bad.push("view is empty"); if (st.over > 2) bad.push(`page ${st.over}px wider than the screen`);
      console.log(`${vp.n.padEnd(8)} #${r.padEnd(24)} ${bad.length ? "FAIL " + bad.join(" | ") : "ok"}`);
      if (bad.length) fails.push(`${vp.n} #${r}: ${bad.join(" | ")}`);
    }
    await p.close();
  }
  await b.close(); server.close();
  if (fails.length) { console.error(`\n${fails.length} problem(s):\n` + fails.join("\n")); process.exit(1); }
  console.log("\nsite smoke test passed");
})().catch(e => { console.error(e); process.exit(1); });
