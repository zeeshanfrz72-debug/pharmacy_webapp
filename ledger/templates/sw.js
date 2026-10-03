const CACHE_NAME = 'pharmacy-ledger-static-v9';

self.addEventListener('install', event => {
  // Precache the original font even when its first page request finishes before
  // this worker controls the page. Failure must not prevent English controls.
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.add('/static/ledger/fonts/JameelNooriNastaleeq.ttf'))
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(names => Promise.all(
      names.filter(name => name.startsWith('pharmacy-ledger-') && name !== CACHE_NAME)
        .map(name => caches.delete(name))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET' || request.mode === 'navigate') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/static/')) return;

  event.respondWith(
    caches.open(CACHE_NAME).then(async cache => {
      const cached = await cache.match(request);
      if (cached) return cached;

      const response = await fetch(request);
      if (response.ok && response.type === 'basic') {
        await cache.put(request, response.clone());
      }
      return response;
    })
  );
});
