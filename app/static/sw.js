/* =========================================================================
   sw.js — service worker for the FH6 telemetry PWA.
   Caches the static app shell for offline load. NEVER caches API or
   WebSocket traffic (those are network-only / passthrough).
   ========================================================================= */
const CACHE = "fh6-shell-v1";

// App shell: pages + static assets. Kept minimal and versioned.
const SHELL = [
  "/",
  "/sessions",
  "/analysis",
  "/compare",
  "/debug",
  "/static/styles.css",
  "/static/app.js",
  "/static/dashboard.js",
  "/static/icon.svg",
  "/manifest.webmanifest"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // addAll fails hard if any request 404s; add individually and ignore misses.
      Promise.all(SHELL.map((url) =>
        cache.add(new Request(url, { cache: "reload" })).catch(() => {})
      ))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle same-origin GET. Everything else goes straight to network.
  if (req.method !== "GET" || url.origin !== self.location.origin) return;

  // NEVER cache API responses, health, debug, downloads, or WebSocket upgrades.
  if (
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/ws") ||
    url.pathname === "/health" ||
    url.pathname.endsWith(".csv")
  ) {
    return; // default network handling
  }

  // App shell strategy: network-first for navigations (fresh pages when online),
  // falling back to cache; cache-first for static assets.
  const isNavigation = req.mode === "navigate";
  const isStatic = url.pathname.startsWith("/static/") ||
                   url.pathname === "/manifest.webmanifest";

  if (isNavigation) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(req).then((m) => m || caches.match("/")))
    );
    return;
  }

  if (isStatic) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        });
      })
    );
  }
});
