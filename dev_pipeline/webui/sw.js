// Do not cache API responses or queue mutations: approvals always use current server state.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  let message = {title: 'Pipeline', body: '案件の状態が更新されました。', url: '/'};
  try { if (event.data) message = {...message, ...event.data.json()}; } catch (_) {}
  event.waitUntil(self.registration.showNotification(message.title, {
    body: message.body, icon: '/icon-192.png', tag: message.id || 'pipeline', data: {url: message.url}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const candidate = new URL(event.notification.data?.url || '/', self.location.origin);
  const target = candidate.origin === self.location.origin ? candidate.href : self.location.origin;
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async windows => {
    const window = windows.find(client => new URL(client.url).origin === self.location.origin);
    if (window) { await window.navigate(target); return window.focus(); }
    return self.clients.openWindow(target);
  }));
});
