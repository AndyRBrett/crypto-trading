// Service worker: cache the app shell so the dashboard opens instantly and
// works offline, but always fetch fresh data (state.json) from the network
// when available, falling back to the last cached copy when offline.
const CACHE = "cryptobot-v1";
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
  const url = new URL(event.request.url);

  // Data: network-first, fall back to cache (so the phone shows last-known state offline).
  if (url.pathname.endsWith("state.json")) {
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
          return resp;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // App shell: cache-first, fall back to network.
  event.respondWith(caches.match(event.request).then((r) => r || fetch(event.request)));
});
