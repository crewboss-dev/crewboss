// Page swap (#816): open a Crewboss page in the MAIN AREA without a document load, so the assistant (CA) drawer is never touched: the same node,
// the same conversation, the same scroll. Used by the assistant's record links (window.crewbossNav(url, {fromCA: true})) and by Back / Forward
// over the entries it pushed. Anything it can't do safely (another origin, an error, a login page, a page that needs its own <head> scripts or
// styles) falls back to a normal navigation, and the assistant restores itself from its saved state.
// After every swap it dispatches "crewboss:page" on document ({url, from, fromCA, popped}); page scripts that bind at load listen for it.
(function () {
  "use strict";
  var html = document.documentElement;
  var seq = 0;

  function inner(doc) { return doc.getElementById("page-inner"); }
  function hrefs(doc) {
    var out = {};
    Array.prototype.forEach.call(doc.head.querySelectorAll('link[rel="stylesheet"][href], script[src]'), function (n) {
      out[n.getAttribute("href") || n.getAttribute("src")] = 1;
    });
    return out;
  }
  function fallback(url, popped) { if (popped) location.reload(); else location.assign(url); return false; }

  // Re-run a swapped page's own inline scripts (innerHTML leaves them inert). A script waiting for DOMContentLoaded would wait forever, so
  // for the moment it runs, a "DOMContentLoaded" listener is called as soon as it is added.
  function runScripts(root) {
    var add = document.addEventListener;
    document.addEventListener = function (type, fn, opt) {
      if (type === "DOMContentLoaded" && typeof fn === "function") { setTimeout(function () { try { fn.call(document, new Event("DOMContentLoaded")); } catch (e) { console.error(e); } }, 0); return; }
      return add.call(document, type, fn, opt);
    };
    try {
      Array.prototype.forEach.call(root.querySelectorAll("script"), function (old) {
        var s = document.createElement("script");
        Array.prototype.forEach.call(old.attributes, function (a) { s.setAttribute(a.name, a.value); });
        s.text = old.textContent;
        old.parentNode.replaceChild(s, old);
      });
    } finally { document.addEventListener = add; }
  }

  function swapIn(doc, finalUrl, opts) {
    var box = inner(document), fresh = inner(doc);
    var wide = window.matchMedia && window.matchMedia("(max-width: 767px)").matches;
    if (wide && opts.fromCA && window.crewbossCA) window.crewbossCA.minimise();     // a phone: the sheet steps aside (its state stays in memory)
    var from = location.pathname;
    try { var st = window.Alpine && window.Alpine.store("drawer"); if (st) st.open = false; } catch (e) {}
    box.innerHTML = fresh.innerHTML;
    document.title = doc.title || document.title;
    var here = document.getElementById("nav-here"), there = doc.getElementById("nav-here");
    if (here && there) here.textContent = there.textContent;
    var base = doc.body.getAttribute("data-base") || "";
    document.body.setAttribute("data-base", base);
    var nl = document.getElementById("nav-list"), nn = doc.getElementById("nav-list");      // the menu and its highlighted row, every swap (same project or not)
    if (nl && nn) nl.replaceWith(nn);
    if (window.Alpine && window.Alpine.initTree) { try { window.Alpine.initTree(box); } catch (e) {} }
    if (window.htmx) window.htmx.process(box);
    runScripts(box);
    var pg = document.getElementById("page"); if (pg) pg.scrollTop = 0;
    return from;
  }

  function go(url, opts) {
    opts = opts || {};
    var u;
    try { u = new URL(url, location.href); } catch (e) { return Promise.resolve(false); }
    if (u.origin !== location.origin || !inner(document)) return Promise.resolve(fallback(u.href));
    return fetch(u.href, { credentials: "same-origin", headers: { "Accept": "text/html", "X-Crewboss-Swap": "1" } }).then(function (r) {
      var ct = r.headers.get("content-type") || "";
      var fu = new URL(r.url || u.href, location.href);
      if (!r.ok || ct.indexOf("text/html") < 0 || fu.origin !== location.origin || /^\/(auth|account)\b/.test(fu.pathname)) return fallback(u.href, opts.popped);
      return r.text().then(function (text) {
        var doc = new DOMParser().parseFromString(text, "text/html");
        var have = hrefs(document), need = hrefs(doc);
        for (var k in need) if (!have[k]) return fallback(u.href);                   // the page brings its own head scripts/styles: let it load properly
        // Only pages built to be swapped in (a record's full page, data-swappable): a list or board binds its scripts at load, so it gets a real load.
        if (!inner(doc) || !inner(doc).querySelector("[data-swappable]")) return fallback(u.href, opts.popped);
        if (!opts.popped) {                                                          // tag the entry we leave, so Back to it is ours to handle
          if (!(history.state && history.state.cb)) history.replaceState(Object.assign({}, history.state, { cb: ++seq }), "");
        }
        var from = swapIn(doc, fu.href, opts);
        if (!opts.popped) history.pushState({ cb: ++seq, ca: !!opts.fromCA }, "", fu.pathname + fu.search + fu.hash);
        document.dispatchEvent(new CustomEvent("crewboss:page", { detail: { url: fu.href, from: from, fromCA: !!opts.fromCA, popped: !!opts.popped } }));
        return true;
      });
    }).catch(function () { return fallback(u.href, opts.popped); });
  }
  window.crewbossNav = go;

  window.addEventListener("popstate", function (e) {
    if (sheetIsOpen()) return;                                                      // Back that closes the task sheet: the page under it stays
    if (!(e.state && e.state.cb)) return;                                           // not an entry of ours (or only a #hash change)
    go(location.href, { popped: true, fromCA: !!e.state.ca });
  });
  function sheetIsOpen() { try { var st = window.Alpine && window.Alpine.store("drawer"); return !!(st && st.open); } catch (e) { return false; } }

  // THE TASK SHEET (owner 11-18: "Tasks should always open full page, with an exit to go back to previous page. So a full page window on top
  // of the current window"; partials/task_drawer_shell.html). Every list, board, plan and Focus card loads its task into the sheet. The sheet
  // puts the task's own URL in the address bar (a history entry marked {sheet}), so a refresh or a copied link shows the task, its writes
  // answer with the full-page layout, and the assistant opened over it starts on that task. Back, Esc, the exit button or a swipe down all
  // close it the same way: Back over our entry, and the page underneath is exactly as it was (never reloaded).
  function sheetStore() { try { return window.Alpine && window.Alpine.store("drawer"); } catch (e) { return null; } }
  var titleUnder = null;                                                            // the page's own tab title while a sheet shows the task's
  function sheetShut() {
    var st = sheetStore(); if (st) st.open = false;
    if (titleUnder !== null) { document.title = titleUnder; titleUnder = null; }
  }
  window.crewbossCloseTask = function () {
    if (history.state && history.state.sheet) history.back();                       // popstate below shuts it
    else sheetShut();
  };
  window.addEventListener("popstate", function (e) {
    var st = sheetStore();
    if (e.state && e.state.sheet && !e.state.cb) {                                  // Forward onto a task sheet again: reopen it (an entry a
                                                                                    // page swap left from is the full page's: the swap handles it)
      if (window.htmx) window.htmx.ajax("GET", e.state.sheet, { target: "#drawer-content", swap: "innerHTML" }).then(function () { var s = sheetStore(); if (s) s.open = true; });
      return;
    }
    if (st && st.open) sheetShut();
  });

  // On a task's OWN page (a link, a refresh) its "← Project" link is the exit too: back to the page you came from when that was ours.
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[data-back-link]");
    if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    var ref = null; try { ref = document.referrer ? new URL(document.referrer) : null; } catch (x) {}
    if ((history.state && history.state.cb) || (ref && ref.origin === location.origin && history.length > 1)) { e.preventDefault(); history.back(); }
  });

  // The fragment carries [data-full-url]: in the sheet, it names the task (the bar and heading) and puts its URL in the address bar.
  document.addEventListener("htmx:afterSwap", function (e) {
    var t = e.detail && e.detail.target;
    if (!t || t.id !== "drawer-content") return;
    var m = t.querySelector("[data-full-url]");
    var sh = t.closest("[data-task-sheet]");
    if (m && sh) {
      var full = m.getAttribute("data-full-url"), no = sh.querySelector("[data-task-sheet-no]"), ttl = sh.querySelector("[data-task-sheet-title]");
      if (no) no.textContent = "Task #" + m.getAttribute("data-full-no");
      if (ttl) ttl.textContent = m.getAttribute("data-full-title") || "";
      var sb = sh.querySelector(".task-sheet-body"); if (sb && location.pathname !== full) sb.scrollTop = 0;
      if (location.pathname !== full) {
        if (history.state && history.state.sheet) history.replaceState({ sheet: full + "/panel" }, "", full);   // one task to the next: one entry
        else history.pushState({ sheet: full + "/panel" }, "", full);
        if (titleUnder === null) titleUnder = document.title;
        document.title = "#" + m.getAttribute("data-full-no") + " " + (m.getAttribute("data-full-title") || "") + " — Crewboss";
      }
      var s2 = sheetStore(); if (s2 && !s2.open) s2.open = true;
      return;
    }
    // On the task's FULL PAGE (its heading sits outside the refreshed part) a saved title updates the heading and the tab title too.
    var pg = t.closest("[data-ca-page]"), h = pg && pg.querySelector("[data-record-title]");
    if (m && h && m.getAttribute("data-full-title")) {
      h.textContent = m.getAttribute("data-full-title");
      document.title = "#" + m.getAttribute("data-full-no") + " " + m.getAttribute("data-full-title") + " — Crewboss";
      pg.setAttribute("data-ca-title", m.getAttribute("data-full-title"));
    }
  });
})();
