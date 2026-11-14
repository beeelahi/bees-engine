// Bee's Dashboard service worker: keeps the last good copy of the page so the site still opens when the network or
// the visitor's DNS stumbles (it then shows the saved copy and the page refreshes its data in the background).
// Same-origin GET only; nothing cross-origin (sign-in, chart library) is touched. The data files are cached by the
// page itself (Cache Storage "bee-data-v1").
// A saved copy is only a stand-in: the network fetch keeps going, and when the fresh page arrives it replaces the
// saved copy and the open page is told to reload once, so a slow connection never leaves anyone on an old version.
// Changing SHELL drops every saved copy (v2: copies saved before 10 Nov 2026 were stale on slow connections).
const SHELL = "bee-shell-v2";
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
  const net = fetch(r, { cache: "no-cache" });   // revalidate: an unchanged page comes back as a tiny 304
  e.respondWith((async () => {
    const c = await caches.open(SHELL);
    let late = false;
    const timer = new Promise(res => setTimeout(() => { late = true; res(null); }, 8000));
    try {
      const n = await Promise.race([net, timer]);
      if (n) { if (n.ok) await c.put("/", n.clone()); return n; }
    } catch (err) { /* offline: fall through to the saved copy */ }
    const saved = await c.match("/");
    if (!saved) return net;                         // nothing saved: wait for the network after all
    if (late) e.waitUntil(net.then(async n => {      // slow, not offline: finish the fetch, then refresh the page once
      if (!n.ok) return;
      await c.put("/", n.clone());
      for (const cl of await self.clients.matchAll({ type: "window" })) cl.postMessage({ type: "bee-shell-updated" });
    }).catch(() => {}));
    return saved;
  })());
});
