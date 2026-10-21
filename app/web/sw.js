/* Kumanda servis çalışanı: uygulama kabuğunu önbelleğe alır (ağ yoksa/yavaşsa açılır). Canlı akış ve API'ye dokunmaz. */
const CACHE = 'kumanda-shell-v1';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())
));
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (/\/(stream\.mjpg|crop\.jpg|screen\.jpg|screen\.bmp|ws)$/.test(url.pathname)) return;
  if (req.mode !== 'navigate' && !/\.(png|svg|json|webmanifest)$/.test(url.pathname)) return;
  // Ağ öncelikli (güncel sürüm), ağ yoksa önbellekten
  e.respondWith(
    fetch(req).then(res => {
      if (res && res.ok) { const c = res.clone(); caches.open(CACHE).then(ch => ch.put(req.mode === 'navigate' ? url.pathname : req, c)).catch(() => {}); }
      return res;
    }).catch(() => caches.match(req.mode === 'navigate' ? url.pathname : req).then(r => r || caches.match(url.pathname)))
  );
});
