// Notifications (#487; owner 11-09: no panel in the account menu, the browser's own prompt and settings handle it).
// Registers the service worker (/sw.js, scope "/") on every page, so the installed app and the browser can show
// pushes, then keeps this device subscribed with no buttons of ours:
//   - permission "granted": make sure the server knows this device; if not (none held, or one the server pruned or
//     made with an old key), subscribe FRESH with the server's key and store it;
//   - permission "default": on the person's first click anywhere (browsers only prompt from a gesture; iPhone needs
//     one), ask ONCE per device ('crewboss-push-asked'); if they allow, subscribe as above;
//   - permission "denied" or push unsupported: nothing. Turning it back on lives in the browser's / phone's settings.
// The server decides what to push (app/push.py); this file never sends anything.
(function () {
  var supported = ("serviceWorker" in navigator) && ("PushManager" in window) && ("Notification" in window);
  if (!supported) return;
  var regPromise = navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () { return null; });
  var ASKED = "crewboss-push-asked";

  function b64ToBytes(s) {
    var pad = "=".repeat((4 - (s.length % 4)) % 4);
    var raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; });
  }
  function asked() { try { return localStorage.getItem(ASKED) === "1"; } catch (e) { return false; } }
  function markAsked() { try { localStorage.setItem(ASKED, "1"); } catch (e) { /* private mode */ } }

  // Subscribe fresh with the server's key, unless the server already has this browser's subscription.
  function ensureSubscribed() {
    return regPromise.then(function (reg) {
      if (!reg) return null;
      return navigator.serviceWorker.ready.then(function () { return reg.pushManager.getSubscription(); })
        .then(function (sub) {
          var q = sub ? "?endpoint=" + encodeURIComponent(sub.endpoint) : "";
          return getJSON("/push/status" + q).then(function (st) {
            if (!st || !st.enabled || (sub && st.this_device)) return null;   // server off, or already known
            return fetch("/push/key", { credentials: "same-origin" }).then(function (r) {
              if (!r.ok) return null;
              return r.text().then(function (key) {
                return (sub ? sub.unsubscribe() : Promise.resolve())
                  .then(function () {
                    return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key.trim()) });
                  })
                  .then(function (fresh) {
                    return fetch("/push/subscribe", {
                      method: "POST", credentials: "same-origin",
                      headers: { "Content-Type": "application/json" }, body: JSON.stringify(fresh.toJSON())
                    });
                  });
              });
            });
          });
        });
    }).catch(function () { /* best effort: the next page load tries again */ });
  }

  if (Notification.permission === "granted") { ensureSubscribed(); return; }
  if (Notification.permission !== "default" || asked()) return;

  // Ask from the first click, and only when the server can actually send (no prompt for nothing).
  getJSON("/push/status").then(function (st) {
    if (!st || !st.enabled) return;
    function onFirstClick() {
      document.removeEventListener("click", onFirstClick, true);
      markAsked();
      Notification.requestPermission().then(function (perm) { if (perm === "granted") ensureSubscribed(); });
    }
    document.addEventListener("click", onFirstClick, true);
  }).catch(function () {});
})();
