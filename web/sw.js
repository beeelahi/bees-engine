// Bee's Dashboard service worker: keeps the last good copy of the page so the site still opens when the network or
// the visitor's DNS stumbles (it then shows the saved copy and the page refreshes its data in the background).
// Same-origin GET only; nothing cross-origin (sign-in, chart library) is touched. The data files are cached by the
// page itself (Cache Storage "bee-data-v1").
const SHELL = "bee-shell-v1";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k.startsWith("bee-shell-") && k !== SHELL) await caches.delete(k);
  await self.clients.claim();
})()));
self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET" || r.mode !== "navigate") return;
  const u = new URL(r.url);
  if (u.origin !== self.location.origin || !(u.pathname === "/" || u.pathname.endsWith("/index.html"))) return;
  e.respondWith((async () => {
    const c = await caches.open(SHELL);
    try {
      const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 8000);
      const n = await fetch(r, { cache: "no-store", signal: ctl.signal });
      clearTimeout(t);
      if (n.ok) await c.put("/", n.clone());
      return n;
    } catch (err) {
      return (await c.match("/")) || Response.error();
    }
  })());
});
