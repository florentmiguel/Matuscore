// VITI Sens — Service Worker
// Stratégie : Cache First pour les ressources statiques

const CACHE_NAME = 'vitisens-v1';
const ASSETS = [
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-annotation/3.0.1/chartjs-plugin-annotation.min.js'
];

// Installation : mise en cache des ressources
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      console.log('[SW] Mise en cache initiale');
      return cache.addAll(ASSETS.filter(url => !url.startsWith('http') || url.includes('cdnjs')));
    }).catch(e => console.log('[SW] Cache partiel:', e))
  );
  self.skipWaiting();
});

// Activation : nettoyer les anciens caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch : Cache First, fallback réseau
self.addEventListener('fetch', event => {
  // Ne pas intercepter les requêtes API externes
  if (event.request.url.includes('/api/') || event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(response => {
        // Mettre en cache les nouvelles ressources valides
        if (response && response.status === 200 && response.type !== 'opaque') {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => {
        // Hors ligne : retourner la page principale si disponible
        if (event.request.destination === 'document') {
          return caches.match('./index.html');
        }
      });
    })
  );
});
