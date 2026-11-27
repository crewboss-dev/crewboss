// THE CHROME (#479) — the ERP's drawer, avatar menu, phone sheets and header behaviour, as its base.html and its admin
// site's admin.js ("THE CHROME", #480) run them, so the three surfaces behave as one. Hand-written, not built; `defer`.
//   - The menu drawer, the ERP's (#458, its base.html "THE MENU DRAWER"): HOVER the button or the rail with a real mouse, at ANY width, to
//     PEEK (the menu slides out over the page, the page does not move; it retracts when the pointer leaves); from 1280px CLICK the button to
//     PIN it (the page moves across, html.nav-pinned .nav-push); click again to unpin. The pin is a per-browser convenience in localStorage
//     ('crewboss-nav-pinned', applied before paint by base.html's inline script). Below 1280px (iPad, phone) there is no pin:
//     a tap on the menu button opens it over the page, and a tap outside, Esc, picking a page or tapping the button again closes it.
//   - --hdr-h and --sbw (the header's height, #page's scrollbar gutter), which chrome.css reads, as the ERP's app.js.
//   - The page name lines up with the content (alignHere, #458), and the project switcher's panel sits under its field
//     (--sw-left / --sw-width, the ERP's placeCmd).
//   - Light / Dark / Auto in the avatar menu ('crewboss-theme'; Auto follows the OS as it changes).
//   - The phone sheets (More, you): open from their button, close on the backdrop, Esc, or a swipe down (dragSheet, the
//     ERP's controller: 1:1 tracking, velocity projection, a spring that inherits the finger's speed).
(function () {
  "use strict";
  var root = document.documentElement;
  var PIN_KEY = "crewboss-nav-pinned";
  var WIDE = window.matchMedia("(min-width: 1280px)");
  var timer = null, peeking = false;   // peeking = the drawer is open because a real mouse is over it, not because of a click or a tap
  function el(id) { return document.getElementById(id); }

  // ── the drawer (the ERP's code) ───────────────────────────────────────────────────────────────────────────────
  // LOCKED only TAKES EFFECT at >= 1280px: the class can stay set on an iPad, where the CSS simply ignores it.
  function pinnedNow() { return root.classList.contains("nav-pinned") && WIDE.matches; }
  function sync() {
    var d = el("nav-drawer"), b = el("nav-burger"), shown = root.classList.contains("nav-open") || pinnedNow();
    if (d) d.setAttribute("aria-hidden", shown ? "false" : "true");
    if (b) {
      b.setAttribute("aria-expanded", shown ? "true" : "false");
      b.setAttribute("aria-pressed", pinnedNow() ? "true" : "false");
      b.title = WIDE.matches ? (pinnedNow() ? "Hide the menu" : "Keep the menu open") : "Menu";
    }
    Array.prototype.forEach.call(document.querySelectorAll("[data-nav-toggle]"), function (t) { t.classList.toggle("is-on", root.classList.contains("nav-open")); });
  }
  function setOpen(open) { clearTimeout(timer); if (!open) peeking = false; root.classList.toggle("nav-open", !!open); sync(); }
  window.toggleNavPin = function (want) {
    var on = (want === undefined) ? !root.classList.contains("nav-pinned") : !!want;
    root.classList.toggle("nav-pinned", on);
    try { localStorage.setItem(PIN_KEY, on ? "1" : "0"); } catch (e) { /* not remembered */ }
    setOpen(false);
    window.dispatchEvent(new Event("resize"));   // the page's width changed: re-measure what depends on it
  };
  // The button: wide = pin / unpin; otherwise = open / close over the page. A click on a hover-peek (narrow) keeps it open, as a click would.
  window.toggleNavMenu = function (want) {
    if (WIDE.matches && want === undefined) { window.toggleNavPin(); return; }
    if (want === undefined && peeking) { peeking = false; clearTimeout(timer); return; }
    setOpen((want === undefined) ? !root.classList.contains("nav-open") : want);
  };
  var burger = el("nav-burger");
  if (burger) burger.addEventListener("click", function () { window.toggleNavMenu(); });
  // HOVER = peek, at ANY width (owner 11-09, #703: a tiled window narrower than 1280px got no peek) and only for a REAL MOUSE: the pointer's own
  // type decides (pointerType === "mouse"), never the (hover: hover) media query, which some Linux browsers misreport. It stays while the pointer
  // is on the button OR the drawer; a short grace on leaving lets the pointer cross the gap. PINNING (the page moves) is the ERP's: from 1280px.
  // A touch never peeks: a tap opens the overlay (the button), and a tap outside, Esc, a page pick or the button again closes it.
  function isMouse(e) { return !!e && e.pointerType === "mouse"; }
  function peekIn(e) { if (!isMouse(e) || pinnedNow()) return; if (root.classList.contains("nav-open") && !peeking) return;
                       clearTimeout(timer); timer = setTimeout(function () { peeking = true; setOpen(true); }, 60); }
  function peekOut(e) { if (!isMouse(e) || pinnedNow() || !peeking) { if (!peeking) clearTimeout(timer); return; }
                        clearTimeout(timer); timer = setTimeout(function () { peeking = false; setOpen(false); }, 220); }
  ["nav-burger", "nav-drawer"].forEach(function (id) {
    var n = el(id); if (!n) return;
    n.addEventListener("pointerenter", peekIn); n.addEventListener("pointerleave", peekOut);
  });
  // Overlay (narrow, or a peek on a touch screen): a tap or click outside closes it, as do Esc and picking a page.
  function closeIfOutside(e) {
    if (!root.classList.contains("nav-open") || pinnedNow()) return;
    var d = el("nav-drawer"), b = el("nav-burger");
    if ((d && d.contains(e.target)) || (b && b.contains(e.target))) return;
    if (e.target.closest && e.target.closest("[data-nav-toggle]")) return;   // the phone bar's Menu tab toggles it itself
    setOpen(false);
  }
  document.addEventListener("mousedown", closeIfOutside, true);
  document.addEventListener("touchstart", closeIfOutside, { capture: true, passive: true });
  document.addEventListener("click", function (e) {   // picking a page closes a floating drawer
    if (e.target.closest && e.target.closest("#nav-list a") && !pinnedNow()) setOpen(false);
  });

  // The phone bar's Menu tab (#771) opens and closes the drawer.
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-nav-toggle]");
    if (!t) return;
    setOpen(!root.classList.contains("nav-open"));
  });
  // The Projects / Accounts sections fold (#771); the state is per person, in localStorage (best effort).
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-sec-toggle]");
    if (!t) return;
    var sec = t.closest(".nav-sec"), closed = sec.classList.toggle("is-closed");
    t.setAttribute("aria-expanded", closed ? "false" : "true");
    try {
      var st = JSON.parse(localStorage.getItem("crewboss-nav-sections") || "{}");
      st[sec.getAttribute("data-sec")] = closed ? 0 : 1;
      localStorage.setItem("crewboss-nav-sections", JSON.stringify(st));
    } catch (err) { /* private mode: the fold just isn't remembered */ }
  });
  // The star on a project (#778): per person, server-side (/all/favourites), so it follows them across devices. The star flips in place everywhere it appears and the
  // menu's Favourites section gains or loses the row without a reload. The hint under an empty section is hidden for good once anything has been starred.
  function favRows() { return document.querySelector("#nav-sec-favourites"); }
  // INSTANT (owner 11-16: "click on the star and it remove from favorite instantly"): the star, the menu row and the empty hint change on the tap;
  // the server is told after, and a refusal puts it all back with a toast.
  function paintFav(slug, starred, name, href) {
    Array.prototype.forEach.call(document.querySelectorAll('[data-fav="' + slug + '"]'), function (x) {
      x.classList.toggle("is-on", starred); x.setAttribute("aria-pressed", starred ? "true" : "false");
    });
    var body = favRows(), sec = document.getElementById("nav-fav-sec");
    if (body) {
      var have = body.querySelector('[data-fav-row="' + slug + '"]');
      if (!starred && have) have.remove();
      if (starred && !have) {
        var src = document.querySelector('.nav-sec[data-sec=accounts] [data-record="' + slug + '"]'), wrap = src && src.closest(".nav-recwrap");
        var row = wrap ? wrap.cloneNode(true) : null;
        if (!row) { row = document.createElement("div"); row.className = "nav-recwrap"; var a = document.createElement("a"); a.className = "nav-rec"; a.href = href; a.setAttribute("data-record", slug); a.textContent = name; row.appendChild(a); }
        row.setAttribute("data-fav-row", slug); row.classList.remove("is-archived");
        var star = row.querySelector("[data-fav]"); if (star) { star.classList.add("is-on"); star.setAttribute("aria-pressed", "true"); }
        var rows = Array.prototype.slice.call(body.querySelectorAll("[data-fav-row]")), before = null;
        rows.forEach(function (r) { if (!before && r.textContent.trim().toLowerCase() > String(name).toLowerCase()) before = r; });
        if (before) body.insertBefore(row, before); else body.insertBefore(row, body.querySelector("[data-fav-hint]"));
      }
      if (sec) sec.classList.toggle("is-empty", !body.querySelector("[data-fav-row]"));
    }
    if (starred) { document.documentElement.classList.add("fav-seen"); try { localStorage.setItem("crewboss-fav-seen", "1"); } catch (err) { /* not remembered */ } }
  }
  var favBusy = {};
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-fav]");
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    var slug = b.getAttribute("data-fav"), name = b.getAttribute("data-name") || slug, href = b.getAttribute("data-href") || "#";
    if (favBusy[slug]) return;                       // one change per project in flight; the next tap waits for it
    var want = !b.classList.contains("is-on");
    paintFav(slug, want, name, href);
    favBusy[slug] = true;
    fetch("/all/favourites", { method: "POST", credentials: "same-origin", headers: { "HX-Request": "true", "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ slug: slug }) })
      .then(function (r) { if (!r.ok) throw new Error("no"); return r.json(); })
      .then(function (d) { if (d.starred !== want) paintFav(slug, d.starred, d.name || name, d.href || href); })
      .catch(function () { paintFav(slug, !want, name, href); if (window.mddToast) window.mddToast("Could not change that favourite.", "error"); })
      .then(function () { favBusy[slug] = false; });
  });
  // An account's fold (#778): its projects hide under it; per account, per person, in localStorage (best effort). A toggle never closes a peek or the overlay
  // (it is a button, not a link, and nothing here calls setOpen).
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-grp-toggle]");
    if (!t) return;
    var grp = t.closest(".nav-grp"), closed = grp.classList.toggle("is-closed");
    t.setAttribute("aria-expanded", closed ? "false" : "true");
    try {
      var st = JSON.parse(localStorage.getItem("crewboss-nav-groups") || "{}");
      st[grp.getAttribute("data-grp")] = closed ? 0 : 1;
      localStorage.setItem("crewboss-nav-groups", JSON.stringify(st));
    } catch (err) { /* private mode: the fold just isn't remembered */ }
  });
  // "Show archived" at a section's foot expands the section with its archived records (#771: archived records are a toggle in the menu, never a page).
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-archived-toggle]");
    if (!t) return;
    var on = t.closest(".nav-sec").classList.toggle("show-archived");
    if (!t.getAttribute("data-label")) t.setAttribute("data-label", t.textContent);
    t.setAttribute("aria-expanded", on ? "true" : "false");
    t.textContent = on ? "Hide archived" : t.getAttribute("data-label");
  });
  if (WIDE.addEventListener) WIDE.addEventListener("change", function () { setOpen(false); });
  sync();

  // You, top right of the header (#786): the menu drops down from the avatar.
  var userBtn = el("user-btn"), userMenu = el("user-menu");
  if (userBtn && userMenu) {
    userBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = userMenu.classList.contains("hidden");
      userMenu.classList.toggle("hidden", !open);
      userBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", function (e) {
      if (e.target.closest && e.target.closest("#me-btn, #me-pop")) return;   // the phone popover is borrowing the menu
      if (!userMenu.classList.contains("hidden") && !userMenu.contains(e.target)) {
        userMenu.classList.add("hidden"); userBtn.setAttribute("aria-expanded", "false");
      }
    });
  }

  // Pre-Crewboss browser keys: read once, write the new key, delete the old. Remove after the 2027-01 release.
  (function () {
    try {
      var o = localStorage.getItem("mdd-theme");
      if (o !== null) { if (localStorage.getItem("crewboss-theme") === null) localStorage.setItem("crewboss-theme", o); localStorage.removeItem("mdd-theme"); }
    } catch (e) { /* private mode */ }
  })();

  // ── Light / Dark / Auto ───────────────────────────────────────────────────────────────────────────────────────
  // 'crewboss-theme' = 'light' | 'dark' | 'system' (absent = 'system'); base.html applies it before paint.
  function themePref() {
    var t = null;
    try { t = localStorage.getItem("crewboss-theme"); } catch (e) { /* private mode */ }
    return t === "light" || t === "dark" ? t : "system";
  }
  var OS_DARK = window.matchMedia("(prefers-color-scheme: dark)");
  function applyTheme() {
    var t = themePref(), dark = t === "dark" || (t === "system" && OS_DARK.matches);
    root.classList.toggle("dark", dark);
    root.classList.toggle("light", !dark);   // the shared canvas follows the OS unless the page says light
  }
  function paintThemeSwitch() {
    var t = themePref();
    document.querySelectorAll(".theme-switch [data-theme]").forEach(function (b) {
      var on = b.getAttribute("data-theme") === t;
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".theme-switch [data-theme]");
    if (!b) return;
    try { localStorage.setItem("crewboss-theme", b.getAttribute("data-theme")); } catch (err) { /* private mode */ }
    applyTheme(); paintThemeSwitch();
  });
  try { OS_DARK.addEventListener("change", function () { if (themePref() === "system") applyTheme(); }); } catch (e) { /* old Safari */ }
  paintThemeSwitch();

  // ── measurements: --hdr-h, --sbw, the page name's alignment, the switcher panel's place ──────────────────────────
  var hdr = document.querySelector("header.chrome-fixed"), page = el("page");
  function measure() {
    // --sbw = #page's scrollbar gutter: the header's fade stops short of it, so the bar runs the full height (#457).
    if (page) root.style.setProperty("--sbw", Math.max(0, page.offsetWidth - page.clientWidth) + "px");
    if (hdr) root.style.setProperty("--hdr-h", hdr.offsetHeight + "px");
  }
  // THE PAGE NAME LINES UP WITH THE CONTENT, in every state (the ERP's #458): measured, not a fixed offset — on a
  // screen wider than the 1920px column the content is centred, so its edge moves with the window and with the lock.
  // Never left of the menu button and back/forward: on a narrow screen it simply follows them.
  function alignHere() {
    var here = el("nav-here"), inner = document.querySelector("#page > div");
    if (!here || !inner) return;
    var prev = here.style.transition; here.style.transition = "none"; here.style.marginLeft = "0px";
    var natural = here.getBoundingClientRect().left;
    var edge = inner.getBoundingClientRect().left + (parseFloat(getComputedStyle(inner).paddingLeft) || 0);
    here.style.marginLeft = Math.max(0, Math.round(edge - natural)) + "px";
    void here.offsetWidth; here.style.transition = prev;
    placeSwitcher();
  }
  // The switcher's panel under its field: the field's box, published for the panel's left / width.
  function placeSwitcher() {
    var f = document.querySelector("[data-jump-field]");
    if (!f) return;
    var r = f.getBoundingClientRect();
    if (!r.width) return;
    root.style.setProperty("--sw-left", Math.round(r.left) + "px");
    root.style.setProperty("--sw-width", Math.round(r.width) + "px");
  }
  function relayout() { measure(); alignHere(); }
  window.addEventListener("resize", relayout);
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(relayout);
    if (hdr) ro.observe(hdr);
    if (page) ro.observe(page);
  }
  relayout();

  // ── the phone sheets ──────────────────────────────────────────────────────────────────────────────────────────
  function sheet(id, open) {
    var s = el(id); if (!s) return;
    s.classList.toggle("is-open", open); s.setAttribute("aria-hidden", open ? "false" : "true");
    syncSheetOpen();
  }
  // html.sheet-open blocks pull-to-refresh (chrome.css). It is ON while ANY sheet is open — a More/.m-sheet OR the task
  // drawer (#601: the drawer never set it, so a flick down on its grab bar reloaded the page) — and is always recomputed
  // from both, so closing one never clears it while the other is still open.
  function syncSheetOpen() {
    var d = window.Alpine && Alpine.store && Alpine.store("drawer");
    root.classList.toggle("sheet-open", !!(d && d.open) || !!document.querySelector(".m-sheet.is-open"));
  }
  function watchDrawer() {
    if (!(window.Alpine && Alpine.effect && Alpine.store("drawer")) || watchDrawer.done) return;
    watchDrawer.done = true;
    Alpine.effect(function () { Alpine.store("drawer").open; syncSheetOpen(); });   // re-runs on every open/close
  }
  document.addEventListener("alpine:initialized", watchDrawer);
  watchDrawer();
  document.addEventListener("click", function (e) {
    var o = e.target.closest && e.target.closest("[data-sheet-open]");
    if (o) {
      var id = o.getAttribute("data-sheet-open"), s = el(id);
      document.querySelectorAll(".m-sheet.is-open").forEach(function (x) { if (x.id !== id) sheet(x.id, false); });
      sheet(id, !(s && s.classList.contains("is-open")));   // the More button closes it again, as in the ERP
      return;
    }
    var c = e.target.closest && e.target.closest("[data-sheet-close]");
    if (c) { var m = c.closest(".m-sheet"); if (m) sheet(m.id, false); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    document.querySelectorAll(".m-sheet.is-open").forEach(function (x) { sheet(x.id, false); });
    if (root.classList.contains("nav-open")) setOpen(false);
  });

  // ── YOU, on a phone: the avatar's popover (the ERP's crewMenu.openMe / closeMe). The rows ARE the drawer's #user-menu, moved into
  // the bubble while it is open and back after; the caret points at the avatar; a tap away, Esc, a link or a theme icon closes it.
  var um = el("user-menu"), uw = el("user-wrap"), mp = el("me-panel");
  window.crewMenu = {
    openMe: function () {
      var pop = el("me-pop"), btn = el("me-btn");
      if (!pop || !um || !mp || pop.classList.contains("is-open")) return;
      mp.appendChild(um); um.classList.remove("hidden", "menu-out");
      var r = btn.getBoundingClientRect();
      pop.style.setProperty("--me-top", Math.round(r.bottom + 10) + "px");
      pop.style.setProperty("--me-caret", Math.round(r.left + r.width / 2 - 8) + "px");   // the bubble starts 8px from the edge
      pop.classList.add("is-open"); pop.setAttribute("aria-hidden", "false"); btn.setAttribute("aria-expanded", "true");
    },
    closeMe: function () {
      var pop = el("me-pop"); if (!pop || !pop.classList.contains("is-open")) return;
      pop.classList.remove("is-open"); pop.setAttribute("aria-hidden", "true");
      var btn = el("me-btn"); if (btn) btn.setAttribute("aria-expanded", "false");
      if (um && uw) { um.classList.add("hidden"); uw.appendChild(um); }
    }
  };
  if (mp) mp.addEventListener("click", function (e) { if (e.target.closest && e.target.closest("a, [data-theme]")) window.crewMenu.closeMe(); });
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!(t && t.closest && t.closest("#me-panel, #me-btn, #me-pop"))) window.crewMenu.closeMe();
  }, true);
  window.addEventListener("pageshow", function (e) { if (e.persisted) window.crewMenu.closeMe(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") window.crewMenu.closeMe(); });

  // SWIPE DOWN TO CLOSE (the ERP's #464): app.js's dragSheet() there, copied here — Apple's projection and a
  // critically-ish damped spring integrated on rAF, so a sheet can be grabbed mid-animation and inherits velocity.
  function project(v, decel) { decel = decel || 0.998; return (v / 1000) * decel / (1 - decel); }
  function rubberband(overshoot, dim, c) { c = c || 0.55; return (overshoot * dim * c) / (dim + c * Math.abs(overshoot)); }
  function spring(from, to, v0, onUpdate, onDone) {
    var k = 200, c = 26, x = from, v = v0, last = null, raf = null, dead = false;
    function step(t) {
      if (dead) return;
      if (last === null) last = t;
      var dt = Math.min((t - last) / 1000, 0.064); last = t;
      v += (-k * (x - to) - c * v) * dt;
      x += v * dt;
      if (Math.abs(x - to) < 0.5 && Math.abs(v) < 30) { onUpdate(to); if (onDone) onDone(); return; }
      onUpdate(x);
      raf = requestAnimationFrame(step);
    }
    raf = requestAnimationFrame(step);
    return { cancel: function () { dead = true; if (raf) cancelAnimationFrame(raf); }, x: function () { return x; } };
  }
  var isPhone = function () { return window.matchMedia("(max-width:767px)").matches; };
  var reduced = function () { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; };
  function dragSheet(panel, opts) {
    if (!panel || panel.__dragWired) return; panel.__dragWired = true;
    var anim = null, dragging = false, pending = false, startY = 0, y = 0, h = 1, pid = null, hist = [], SLOP = 6;
    var backdrop = opts.backdrop || null;
    function paint(v) {
      panel.style.transform = v ? "translateY(" + v + "px)" : "";
      if (backdrop) backdrop.style.opacity = String(Math.max(0, 1 - (v / h)));
    }
    function release() { panel.style.transform = ""; panel.classList.remove("sheet-dragging"); if (backdrop) backdrop.style.opacity = ""; }
    // A drag starts from the handle, or from content already at its top and pulled DOWN; never from a control.
    function mayStart(e) {
      if (!isPhone() || e.pointerType === "mouse") return false;
      if (e.target.closest("input,textarea,select,button,a,[data-combo-opt],[contenteditable],[data-no-sheet-drag]")) return false;
      if (e.target.closest("[data-sheet-grab]")) return "now";
      // EVERY scroller between the finger and the sheet must be at its top (ERP #543): content scrolls first, the sheet
      // moves only once all of it is at the top.
      for (var n = e.target; n && n !== panel && n.nodeType === 1; n = n.parentElement) {
        if (n.scrollTop > 0) return false;
      }
      return "maybe";
    }
    function begin(e, cur) {
      h = panel.offsetHeight || 1; dragging = true; pid = e.pointerId; startY = e.clientY - cur; y = cur;
      hist = [[e.timeStamp, e.clientY]]; panel.classList.add("sheet-dragging");
      try { panel.setPointerCapture(pid); } catch (err) { /* ignore */ }
    }
    panel.addEventListener("pointerdown", function (e) {
      var m = mayStart(e); if (!m) return;
      var cur = anim ? anim.x() : 0;
      if (anim) { anim.cancel(); anim = null; }
      if (m === "now") { begin(e, cur); return; }
      pending = true; pid = e.pointerId; startY = e.clientY; y = cur;
    });
    panel.addEventListener("pointermove", function (e) {
      if (e.pointerId !== pid) return;
      if (pending) {
        var dy = e.clientY - startY;
        if (Math.abs(dy) < SLOP) return;
        if (dy < 0) { pending = false; pid = null; return; }   // upward = the scroller's gesture
        pending = false; begin(e, y); startY = e.clientY - y;
      }
      if (!dragging) return;
      var raw = e.clientY - startY;
      y = raw >= 0 ? raw : -rubberband(-raw, h);
      hist.push([e.timeStamp, e.clientY]); if (hist.length > 6) hist.shift();
      paint(y); e.preventDefault();
    });
    function end(e) {
      if (pending && (!e || e.pointerId === pid)) { pending = false; pid = null; }
      if (!dragging || (e && e.pointerId !== pid)) return;
      dragging = false;
      try { panel.releasePointerCapture(pid); } catch (err) { /* ignore */ }
      var v = 0, first = hist[0], last = hist[hist.length - 1];
      if (first && last && last[0] > first[0]) v = (last[1] - first[1]) / ((last[0] - first[0]) / 1000);
      // The ERP's rule (#543): a short fast flick closes however little it travelled, an upward flick never does; a slow
      // drag is arbitrated by where it would land (past a third of the sheet it goes).
      var dismiss = v >= 500 ? true : v <= -500 ? false : (y + project(v)) > h / 3;
      if (reduced()) { release(); if (dismiss) opts.onDismiss(); return; }
      if (dismiss) anim = spring(y, h, v, paint, function () { anim = null; release(); opts.onDismiss(); });
      else anim = spring(y, 0, v, paint, function () { anim = null; release(); });
    }
    panel.addEventListener("pointerup", end);
    panel.addEventListener("pointercancel", end);
  }
  document.querySelectorAll(".m-sheet").forEach(function (s) {
    dragSheet(s.querySelector(".m-panel"), { backdrop: s.querySelector(".m-backdrop"), onDismiss: function () { sheet(s.id, false); } });
  });
  // Any other sheet (the task drawer on a phone): [data-drag-sheet] panels say "sheet-dismiss" and their owner closes.
  document.querySelectorAll("[data-drag-sheet]").forEach(function (p) {
    dragSheet(p, { onDismiss: function () { p.dispatchEvent(new CustomEvent("sheet-dismiss", { bubbles: true })); } });
  });
  window.dragSheet = dragSheet;
  // THE PHONE MENU (owner 11-15): a bottom sheet there (css/shell.css), so it swipes down to close like the others.
  dragSheet(el("nav-drawer"), { backdrop: el("nav-scrim"), onDismiss: function () { window.toggleNavMenu(false); } });

  // THE ON-SCREEN KEYBOARD (owner 11-15): while it is up on a phone, html.kb-open hides the bottom bar (css/shell.css) to give the page
  // the room. "Up" = a text field has focus AND the visual viewport is well short of its tallest height in this orientation (a hardware
  // keyboard shrinks nothing, so the bar stays). Browsers without visualViewport keep the bar.
  (function () {
    var vv = window.visualViewport; if (!vv) return;
    var tallest = 0, orient = null, NO_KB = /^(checkbox|radio|button|submit|reset|range|color|file|image|hidden)$/i;
    function typing(n) {
      if (!n) return false;
      if (n.isContentEditable) return true;
      if (n.tagName === "TEXTAREA") return !n.readOnly;
      if (n.tagName === "INPUT") return !n.readOnly && !NO_KB.test(n.type || "text");
      return false;
    }
    function check() {
      var o = window.innerWidth > window.innerHeight ? "l" : "p";
      if (o !== orient) { orient = o; tallest = 0; }
      var h = vv.height, t = typing(document.activeElement);
      if (!t || h > tallest) tallest = Math.max(tallest, h);
      var up = isPhone() && t && tallest - h > 120;
      root.classList.toggle("kb-open", up);
      // Where the browser lays the keyboard OVER the page (iOS Safari; interactive-widget=resizes-content already shrinks the page on
      // Android Chrome, so this is ~0 there), --kb-h lifts the fixed drawers (the assistant's) above it.
      var over = up ? Math.max(0, Math.round(window.innerHeight - h - vv.offsetTop)) : 0;
      root.style.setProperty("--kb-h", over + "px");
    }
    vv.addEventListener("resize", check); vv.addEventListener("scroll", check);
    document.addEventListener("focusin", function () { setTimeout(check, 50); });
    document.addEventListener("focusout", function () { setTimeout(check, 50); });
    window.addEventListener("orientationchange", function () { setTimeout(check, 300); });
    check();
  })();
})();
