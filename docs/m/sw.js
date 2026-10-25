/* 서비스 워커: 앱 껍데기를 저장해 두고(오프라인에서도 열림), PC가 보낸 '새 알림 있음' 푸시를 받으면
 * PC에 알림 내용을 물어 보여 준다. 연결 정보는 앱이 IndexedDB에 넣어 둔 것을 쓴다. */
const SHELL = 'co-shell-v12';
const FILES = ['./', 'index.html', 'app.css', 'app.js', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;  // PC·ntfy 요청은 건드리지 않는다
  // 먼저 네트워크(새 버전), 안 되면 저장해 둔 것
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone();
    caches.open(SHELL).then(c => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request, { ignoreSearch: true })));
});

function idbGet(k) {
  return new Promise(res => {
    const r = indexedDB.open('claude-office', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('kv');
    r.onerror = () => res(null);
    r.onsuccess = () => {
      try {
        const g = r.result.transaction('kv').objectStore('kv').get(k);
        g.onsuccess = () => res(g.result);
        g.onerror = () => res(null);
      } catch (e) { res(null); }
    };
  });
}
function idbPut(k, v) {
  return new Promise(res => {
    const r = indexedDB.open('claude-office', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('kv');
    r.onerror = () => res();
    r.onsuccess = () => { const tx = r.result.transaction('kv', 'readwrite'); tx.objectStore('kv').put(v, k); tx.oncomplete = res; tx.onerror = res; };
  });
}

self.addEventListener('push', e => {
  e.waitUntil((async () => {
    const conf = await idbGet('conf');
    const since = (await idbGet('alertSince')) || 0;
    let shown = 0;
    if (conf && conf.u && conf.k) {
      try {
        const r = await fetch(conf.u + '/api/alerts?since=' + since, { headers: { Authorization: 'Bearer ' + conf.k }, cache: 'no-store' });
        const d = await r.json();
        let last = since;
        for (const a of (d.alerts || []).slice(-3)) {
          await self.registration.showNotification(a.title || 'Claude Office', {
            body: a.body || '', tag: 'co-' + (a.sid || a.id), icon: 'icon-192.png', badge: 'icon-192.png', data: { sid: a.sid },
          });
          last = Math.max(last, a.ts);
          shown++;
        }
        await idbPut('alertSince', last);
      } catch (err) { /* 아래 기본 알림 */ }
    }
    if (!shown) await self.registration.showNotification('Claude Office', { body: '🔔', tag: 'co-generic', icon: 'icon-192.png' });
  })());
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    for (const c of cs) if ('focus' in c) return c.focus();
    return self.clients.openWindow('./');
  }));
});
