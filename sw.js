// Opportunities Hub service worker: app shell cached, listings network-first so they are always fresh when online.
const CACHE = "oh-v1";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return; // API calls, fonts, CDNs: straight to network
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) { const copy = r.clone(); const key = u.pathname.includes("/data/") ? new Request(u.origin + u.pathname) : e.request; caches.open(CACHE).then(c => c.put(key, copy)); }
      return r;
    }).catch(() => caches.match(u.pathname.includes("/data/") ? new Request(u.origin + u.pathname) : e.request).then(r => r || caches.match("index.html")))
  );
});
