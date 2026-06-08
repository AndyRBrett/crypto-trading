// Service worker: keep the app instant + offline-capable, but don't get stuck
// on a stale version. Data (state.json) and the page itself (HTML) are
// network-first — fresh when online, last-known copy when offline. Static
// assets (icons, manifest) stay cache-first. Bump CACHE to force a refresh.
const CACHE = "cryptobot-v3";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Network-first for live data AND the page itself, so new versions load when
  // online; fall back to the cached copy (or the app shell) when offline.
  const networkFirst =
    url.pathname.endsWith("state.json") ||
    req.mode === "navigate" ||
    url.pathname.endsWith("index.html") ||
    url.pathname.endsWith("/");

  if (networkFirst) {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return resp;
        })
        .catch(() => caches.match(req).then((r) => r || caches.match("./index.html")))
    );
    return;
  }

  // Static assets: cache-first, fall back to network.
  event.respondWith(caches.match(req).then((r) => r || fetch(req)));
});

// --- Web Push ---

self.addEventListener("push", (event) => {
  let data = { title: "CryptoBot", body: "" };
  try { data = event.data.json(); } catch (_) {}
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "./icon-192.png",
      badge: "./icon-192.png",
      requireInteraction: false,
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window" }).then((wins) => {
      const match = wins.find((w) => w.url.includes("index.html") || w.url.endsWith("/"));
      return match ? match.focus() : clients.openWindow("./");
    })
  );
});
