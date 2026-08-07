// sw.js — minimal service worker.
//
// Two jobs:
// 1. Caches the app shell (HTML/CSS/JS/icons) so the app still opens if
//    the network drops mid-demo -- it just won't have fresh data or a
//    working FixIt Bot until the connection comes back.
// 2. Registering a service worker is one of the signals iOS/Android
//    use to treat this as a "real" installable app rather than just a
//    bookmark.
//
// It deliberately does NOT try to cache /api/* responses -- those need
// to always hit the real server for fresh data.

const CACHE_NAME = "fixitflow-shell-v1";
const SHELL_FILES = [
  "/",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      return (
        cached ||
        fetch(event.request).then((response) => {
          if (event.request.method === "GET" && url.origin === location.origin) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
      );
    })
  );
});
