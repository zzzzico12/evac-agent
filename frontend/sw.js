// オフラインでも、保存済みの避難計画・避難所データ・見たことのある地図を表示できるようにする
const VERSION = 'evac-v18';
const SHELL = [
  './',
  'index.html',
  'app.js',
  'style.css',
  'manifest.webmanifest',
  'icons/icon-192.png',
  'icons/icon-512.png',
  'data/shelters.json',
  'data/alerts.json',
  'data/cities.json',
  'data/rivers.json',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js',
];
const TILE_HOSTS = ['cyberjapandata.gsi.go.jp', 'disaportaldata.gsi.go.jp'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // エージェントの呼び出し（POST）はキャッシュしない
  const url = new URL(req.url);

  // 地図タイル：キャッシュ優先（見たことのある範囲はオフラインでも表示できる）
  if (TILE_HOSTS.includes(url.hostname)) {
    event.respondWith(
      caches.open(`${VERSION}-tiles`).then(async (cache) => {
        const hit = await cache.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res.ok) cache.put(req, res.clone());
        return res;
      }),
    );
    return;
  }

  // 同じオリジンの画面・データ・config.json：ネット優先、つながらなければキャッシュ
  if (url.origin === self.location.origin || url.hostname === 'cdnjs.cloudflare.com' || url.hostname.endsWith('googleapis.com') || url.hostname.endsWith('gstatic.com')) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          // 返す前に複製しておく（返したあとは本文が読み終わっていて複製できない）
          if (res.ok) {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req).then((hit) => hit || caches.match('index.html'))),
    );
  }
});
