// Crewboss service worker (#487): phone notifications for estate problems, sent by app/push.py. Served from /sw.js
// (scope = the whole dashboard, never cached). No fetch handler: the dashboard is live data behind NetBird, nothing is
// cached offline, and #394's sign-in redirect must reach the browser untouched (the ERP's 2026-07-14 lesson).
// Take over at once, so an updated worker never waits for every tab to close.
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });

self.addEventListener('push', function (e) {
  var data = {};
  try { data = e.data ? e.data.json() : {}; } catch (_) { data = { body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(data.title || 'Crewboss', {
    body: data.body || '',
    data: { url: data.url || '/' },
    // The small app mark; no big `icon` (it only repeats the logo and steals text space).
    badge: '/static/brand/crewboss/badge-96.png',  // small mark (3 dots), 96 px; the OS uses only its alpha
    // The server sends a tag per notification GROUP (mdd-estate-<id>): a new group alerts (renotify), a problem joining
    // it or the group's all-clear replaces it in place quietly. Without one, a unique tag: a fixed tag would make every
    // push silently replace the last ("delivered but nothing shows", the ERP's lesson).
    tag: data.tag || ('mdd-' + Date.now()),
    renotify: data.renotify !== false,
    silent: data.silent === true
  }));
});

// Focus an open dashboard window and take it to the target, else open one. The url is ABSOLUTE (app/push.py), so a
// worker still registered on an old hostname opens the current one.
function openTo(url) {
  var want = new URL(url, self.location.origin);
  return clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (wins) {
    for (var i = 0; i < wins.length; i++) {
      var w = wins[i];
      if (new URL(w.url).origin === want.origin && 'focus' in w) {
        return w.focus().then(function (fw) {
          fw = fw || w;
          var here = new URL(fw.url);
          if (fw.navigate && here.pathname + here.search !== want.pathname + want.search) {
            // navigate() rejects on a window this worker doesn't control (loaded before it activated): open instead.
            return fw.navigate(want.href).catch(function () { return clients.openWindow(want.href); });
          }
          return fw;
        });
      }
    }
    return clients.openWindow(want.href);
  });
}

self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  e.waitUntil(openTo((e.notification.data && e.notification.data.url) || '/'));
});
