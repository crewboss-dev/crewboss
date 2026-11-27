// The first-run how-to tour (#938). Hit Next and it shows you the next real button, outlined, with a short card beside it.
//
// WHAT IT SHOWS comes from the server (GET /all/tour: app/tour_steps.yaml, filled in with the person's own project and task), WHERE THEY ARE is
// saved there too (POST /all/tour), so it starts by itself on a person's first page, resumes where they left off, and follows them to another device.
// "How-to" in the profile menu ([data-tour-restart]) starts it again.
//
// Keyboard: Tab stays inside the card, Enter presses the button, Esc closes the tour. The card is a dialog; each step is announced through an aria-live
// region. Reduced motion, light and dark follow css/tour.css. On a phone the card is a bottom sheet (a top one when its target is down there) and
// swipes down to close (chrome.js dragSheet).
(function () {
  "use strict";
  var root = document.getElementById("tour");
  if (!root || window.top !== window) return;
  var shield = root.querySelector("[data-tour-shield]");
  var ring = root.querySelector("[data-tour-ring]"), card = root.querySelector("[data-tour-card]");
  var q = function (n) { return root.querySelector("[data-tour-" + n + "]"); };
  var steps = [], idx = 0, active = false, opened = { nav: false, profile: false }, lastFocus = null, target = null, raf = 0, tracking = 0;

  function api(method, body) {
    var o = { method: method, keepalive: true, credentials: "same-origin", headers: { "HX-Request": "true" }, cache: "no-store" };
    if (body) { o.headers["Content-Type"] = "application/x-www-form-urlencoded"; o.body = new URLSearchParams(body); }
    return fetch("/all/tour", o).then(function (r) { if (!r.ok) throw new Error("tour " + r.status); return r.json(); });
  }
  function isPhone() { return window.matchMedia("(max-width:767px)").matches; }
  function visible(e) {
    if (!e || !e.getClientRects().length) return false;
    var r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== "hidden";
  }
  // Only ever a path on THIS site: "/x" yes; "//host", "https://host", a backslash form, "javascript:" no. (The server builds these too; this is the second lock.)
  function safePath(u) { return typeof u === "string" && /^\/(?![\/\\])/.test(u) && !/[\u0000-\u001f\u007f]/.test(u); }
  function navigate(u) { if (safePath(u)) { location.assign(u); return true; } return false; }
  function onPage(s) { return !s.page || new RegExp(s.page).test(location.pathname); }

  // ── opening what a step points into: the left menu, the profile menu, a closed <details> ──────────────────────────
  function profileBtn() { var m = document.getElementById("me-btn"); return m && visible(m) ? m : document.getElementById("user-btn"); }
  function openProfile() {
    var um = document.getElementById("user-menu"); if (!um) return;
    if (visible(um)) return;
    var b = profileBtn(); if (!b) return;
    if (b.id === "me-btn" && window.crewMenu) window.crewMenu.openMe(); else b.click();
    opened.profile = true;
  }
  function closeProfile() {
    var um = document.getElementById("user-menu");
    if (!um) return;
    if (window.crewMenu) window.crewMenu.closeMe();
    var ub = document.getElementById("user-btn");
    if (ub && ub.getAttribute("aria-expanded") === "true") { um.classList.add("hidden"); ub.setAttribute("aria-expanded", "false"); }
    opened.profile = false;
  }
  function openNav() {
    var d = document.getElementById("nav-drawer");
    if (d && d.getAttribute("aria-hidden") === "false") return;
    if (window.toggleNavMenu) { window.toggleNavMenu(true); opened.nav = true; }
  }
  function closeNav() { if (opened.nav && window.toggleNavMenu) window.toggleNavMenu(false); opened.nav = false; }
  function apply(s) {
    if (s.open !== "nav") closeNav();
    if (s.open !== "profile") closeProfile();
    if (s.open === "nav") openNav();
    if (s.open === "profile") openProfile();
    if (s.open === "details" && s.target) {
      Array.prototype.forEach.call(document.querySelectorAll(s.target), function (e) { var d = e.closest("details"); if (d && !d.open) d.open = true; });
    }
  }
  function find(s) {
    if (!s.target || !onPage(s)) return null;
    var all = document.querySelectorAll(s.target);
    for (var i = 0; i < all.length; i++) if (visible(all[i])) return all[i];
    return null;
  }

  // ── drawing ───────────────────────────────────────────────────────────────────────────────────────────────────────
  function layout() {
    raf = 0;
    if (!active) return;
    var vw = window.innerWidth, vh = window.innerHeight, pad = 12, phone = isPhone();
    if (!target || !document.contains(target)) target = find(steps[idx]);
    if (!target || !visible(target)) {
      target = null; ring.hidden = true; shield.style.clipPath = ""; root.classList.add("is-plain");
      card.classList.remove("is-top");
      if (!phone) { card.style.left = Math.max(pad, (vw - card.offsetWidth) / 2) + "px"; card.style.top = Math.max(pad, (vh - card.offsetHeight) / 2) + "px"; }
      return;
    }
    root.classList.remove("is-plain");
    var r = target.getBoundingClientRect(), g = 5;
    var x1 = Math.max(4, r.left - g), y1 = Math.max(4, r.top - g), x2 = Math.min(vw - 4, r.right + g), y2 = Math.min(vh - 4, r.bottom + g);
    ring.hidden = false;
    // an interactive step leaves a hole in the shield over its target, so the real button can be pressed. The hole is the target's OWN rect,
    // not the padded outline box: a click just beside a small control must hit the shield, not the control's neighbour.
    var hx1 = Math.max(0, r.left), hy1 = Math.max(0, r.top), hx2 = Math.min(vw, r.right), hy2 = Math.min(vh, r.bottom);
    shield.style.clipPath = steps[idx].interactive ? "polygon(evenodd, 0 0, 100% 0, 100% 100%, 0 100%, 0 0, " + hx1 + "px " + hy1 + "px, " + hx1 + "px " + hy2 + "px, " + hx2 + "px " + hy2 + "px, " + hx2 + "px " + hy1 + "px, " + hx1 + "px " + hy1 + "px)" : "";
    ring.style.left = x1 + "px"; ring.style.top = y1 + "px"; ring.style.width = (x2 - x1) + "px"; ring.style.height = (y2 - y1) + "px";
    var cw = card.offsetWidth, ch = card.offsetHeight;
    if (phone) {   // a bottom sheet, flipped to the top when what it points at would be under it
      card.style.left = card.style.top = "";
      card.classList.toggle("is-top", y2 > vh - ch - 8 && y1 > ch);
      return;
    }
    var x, y;
    if (y2 + pad + ch <= vh - pad) { y = y2 + pad; x = x1; }                         // under it
    else if (y1 - pad - ch >= pad) { y = y1 - pad - ch; x = x1; }                    // over it
    else if (x2 + pad + cw <= vw - pad) { x = x2 + pad; y = y1; }                    // to its right
    else if (x1 - pad - cw >= pad) { x = x1 - pad - cw; y = y1; }                    // to its left
    else { x = vw - cw - pad; y = vh - ch - pad; }                                   // a big target: the corner
    card.style.left = Math.min(Math.max(pad, x), vw - cw - pad) + "px";
    card.style.top = Math.min(Math.max(pad, y), vh - ch - pad) + "px";
  }
  function relayout() { if (!raf) raf = requestAnimationFrame(layout); }

  function show(i) {
    idx = i;
    var s = steps[i], last = i === steps.length - 1;
    apply(s);
    target = find(s);
    if (target) {
      var r = target.getBoundingClientRect();
      if (r.top < 48 || r.bottom > window.innerHeight - 8) target.scrollIntoView({ block: r.height > window.innerHeight * .6 ? "start" : "center", inline: "nearest" });
    }
    q("count").textContent = "Step " + (i + 1) + " of " + steps.length;
    q("title").textContent = s.title;
    q("text").textContent = s.text;
    var note = q("note"), away = s.go && safePath(s.go) && !onPage(s);
    note.hidden = true; note.textContent = "";
    if (away) {
      note.hidden = false; note.appendChild(document.createTextNode("This one is on another page. "));
      var go = document.createElement("button"); go.type = "button"; go.setAttribute("data-tour-act", "go"); go.textContent = "Show me";
      note.appendChild(go);
    } else if (!target && s.else_text) { note.hidden = false; note.textContent = s.else_text; }
    var doBtn = root.querySelector("[data-tour-act=do]");
    doBtn.hidden = !(s.interactive && target); doBtn.textContent = s.do || "";
    root.querySelector("[data-tour-act=back]").hidden = i === 0;
    root.querySelector("[data-tour-act=skip]").hidden = last;
    root.querySelector("[data-tour-act=next]").textContent = last ? "Done" : "Next";
    q("live").textContent = "Step " + (i + 1) + " of " + steps.length + ": " + s.title + ". " + s.text;
    card.style.animation = "none"; void card.offsetWidth; card.style.animation = "";    // replay the entrance for each step
    layout();
    card.focus({ preventScroll: true });
    track();
  }
  // While the tour is open, follow the target frame by frame: the menus a step opens slide in, the page scrolls, fonts and images shift things. One
  // rect read per frame, and a redraw only when something moved.
  function track() {
    var last = "", gen = ++tracking, lookups = 0;
    (function frame() {
      if (!active || gen !== tracking) return;
      var r = target && document.contains(target) ? target.getBoundingClientRect() : null;
      var sig = [r ? [r.left, r.top, r.width, r.height].join() : "", card.offsetHeight, window.innerWidth, window.innerHeight].join("|");
      if (sig !== last || (!target && steps[idx].target && lookups++ < 120)) { last = sig; layout(); }   // a target not drawn yet is looked for again for ~2s
      requestAnimationFrame(frame);
    })();
  }
  // ── moving ────────────────────────────────────────────────────────────────────────────────────────────────────────
  function save(action, step) {
    var b = { action: action }; if (step) b.step = step;
    return api("POST", b).catch(function () { /* progress is a convenience: a failed save never stops the tour */ });
  }
  function go(i) {
    var s = steps[i];
    save("step", s.id);
    if (s.go && !onPage(s) && safePath(s.go)) {
      active = false;                                                                  // the next page's tour.js picks it up at this step
      navigate(s.go);
      return;
    }
    show(i);
  }
  function stop() {
    active = false; root.hidden = true; ring.hidden = true; target = null;
    closeNav(); closeProfile();
    if (lastFocus && document.contains(lastFocus) && lastFocus.focus) { try { lastFocus.focus({ preventScroll: true }); } catch (e) { /* gone */ } }
  }
  function start(i) {
    if (!steps.length) return;
    lastFocus = document.activeElement;
    active = true; root.hidden = false; root.classList.remove("is-plain");
    show(Math.min(Math.max(i, 0), steps.length - 1));
  }
  function act(name) {
    if (name === "next") { if (idx === steps.length - 1) { save("finish", steps[idx].id); stop(); } else go(idx + 1); }
    else if (name === "back") { if (idx > 0) go(idx - 1); }
    else if (name === "skip") { save("skip", steps[idx].id); stop(); }
    else if (name === "go") { navigate(steps[idx].go); }
    else if (name === "do") { if (target) target.click(); }                           // the real button; the press handler below steps the tour aside
  }

  // The tour's own clicks and keys are handled at the window's CAPTURE phase and stopped there, so the page's "click outside closes the menu" handlers
  // (the left menu, the profile menu) never see a press on the card and the menus a step opened stay open.
  ["click", "mousedown", "touchstart"].forEach(function (t) {
    window.addEventListener(t, function (e) {
      if (!active || !root.contains(e.target)) return;
      e.stopPropagation();
      if (t !== "click") return;
      var b = e.target.closest && e.target.closest("[data-tour-act]");
      if (b) act(b.getAttribute("data-tour-act"));
    }, { capture: true, passive: t === "touchstart" });
  });
  // Pressing the real thing an interactive step points at (its own click, or the card's "do" button): save that the next step is reached, then step aside while
  // the person uses it. A page that follows (the new project) resumes the tour at that next step; if they back out of the dialog the tour comes back here.
  var HANDOFF = "crewboss-tour-handoff";   // set when the person pressed the real thing: the page that follows carries on at the next step, even if it is on another page
  var dialogSel = '[role=dialog]:not([data-tour-card]), form[action="/projects"]';
  function anyDialog() { var all = document.querySelectorAll(dialogSel); for (var i = 0; i < all.length; i++) if (visible(all[i])) return true; return false; }
  window.addEventListener("click", function (e) {
    if (!active || !target || !steps[idx].interactive || root.contains(e.target) || !target.contains(e.target)) return;
    var here = idx, next = steps[Math.min(here + 1, steps.length - 1)];
    save("step", next.id);
    try { sessionStorage.setItem(HANDOFF, next.id); } catch (err) { /* private mode: the card offers "Show me" instead */ }
    active = false; root.hidden = true; ring.hidden = true;
    closeNav();                                                    // a left menu the tour opened would sit over the dialog
    var seen = false, n = 0, iv = setInterval(function () {
      var open = anyDialog();
      if (open) seen = true;
      if ((seen && !open) || ++n > 1200) {
        clearInterval(iv);
        if (seen && !active) { try { sessionStorage.removeItem(HANDOFF); } catch (err) { /* none */ } save("step", steps[here].id); active = true; root.hidden = false; show(here); }
      }
    }, 300);
  }, true);
  window.addEventListener("keydown", function (e) {
    if (!active) return;
    // "/" and Ctrl/Cmd+K open the page's search under the dim: not while the tour is open
    if (e.key === "/" || ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K"))) { e.preventDefault(); e.stopPropagation(); return; }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); act("skip"); return; }
    if (e.key !== "Tab") return;
    var f = Array.prototype.filter.call(card.querySelectorAll("button"), function (b) { return !b.hidden && b.offsetParent !== null; });
    if (!f.length) return;
    var first = f[0], lastEl = f[f.length - 1], cur = document.activeElement;
    if (e.shiftKey && (cur === first || cur === card || !card.contains(cur))) { e.preventDefault(); lastEl.focus(); }
    else if (!e.shiftKey && (cur === lastEl || !card.contains(cur))) { e.preventDefault(); first.focus(); }
  }, true);
  document.addEventListener("focusin", function (e) { if (active && !root.contains(e.target)) card.focus({ preventScroll: true }); });
  window.addEventListener("resize", relayout);
  window.addEventListener("scroll", relayout, true);
  document.addEventListener("htmx:afterSettle", relayout);
  document.addEventListener("transitionend", relayout, true);
  if (window.dragSheet) window.dragSheet(card, { onDismiss: function () { card.style.transform = ""; act("skip"); } });

  // How-to, in the profile menu: begin again from the first step.
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-tour-restart]");
    if (!b || active) return;
    e.preventDefault();
    closeProfile();
    api("POST", { action: "restart" }).then(function (d) { steps = d.steps; start(0); }).catch(function () { if (window.mddToast) window.mddToast("Could not start the how-to.", "error"); });
  });

  // First page of a person's session (and every page after, until they finish or skip): ask where they are.
  function boot() {
    api("GET").then(function (d) {
      steps = d.steps || [];
      if (d.status !== "new" && d.status !== "active") return;
      var i = 0;
      for (var k = 0; k < steps.length; k++) if (steps[k].id === d.step) i = k;
      var handed = null;
      try { handed = sessionStorage.getItem(HANDOFF); sessionStorage.removeItem(HANDOFF); } catch (err) { /* none */ }
      if (handed && handed === steps[i].id && steps[i].go && !onPage(steps[i]) && safePath(steps[i].go)) { navigate(steps[i].go); return; }   // they did the step: take them to the next one
      start(i);
    }).catch(function () { /* not signed in, or offline: no tour */ });
  }
  if (document.readyState === "complete") boot(); else window.addEventListener("load", boot);
})();
