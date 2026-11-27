// THE RECORD TAB BAR's editing (#773; markup partials/record_head.html, edits app/routers/tabs_pages.py). Only rendered for people who may manage the project.
//   - "..." on a tab opens a menu: Set as default, Rename (inline), Move tab left/right, Remove. Esc or a click outside closes it; arrows move; Enter picks.
//   - "+" opens the catalogue: pick a view on the left, see its preview, "Add to navigation". It never opens a page itself.
// Every edit is a POST to /<slug>/tabs/<action>; success reloads the page (the bar is server-rendered), a refusal is a toast. Plain script, no Alpine.
(function () {
  "use strict";
  // Idempotent (#816): the bar can be swapped in by the page swap (js/pagenav.js, event "crewboss:page"), so the document-level listeners are
  // bound ONCE and read whichever bar is current; the bar's own listeners are bound by init(), once per bar element.
  var bar = null, slug = "", head = null, menu = null, cat = null;
  var cur = null, opener = null;

  function toast(msg) {
    if (window.mddToast) window.mddToast(String(msg).slice(0, 200), "error");
  }
  function post(action, key, value) {
    var body = new URLSearchParams({ key: key, value: value || "" });
    return fetch("/" + slug + "/tabs/" + action, { method: "POST", credentials: "same-origin", body: body,
      headers: { "HX-Request": "true", "Content-Type": "application/x-www-form-urlencoded" } })
      .then(function (r) { return r.ok ? r.json() : r.text().then(function (t) { throw new Error(t || "That did not work"); }); })
      .then(function () { window.location.reload(); })
      .catch(function (e) { toast(e.message || "That did not work"); });
  }
  function place(el, anchor, alignRight) {
    var h = head.getBoundingClientRect(), a = anchor.getBoundingClientRect();
    el.style.top = (a.bottom - h.top + 4) + "px";
    var left = a.left - h.left;
    var w = el.offsetWidth;
    el.style.left = Math.max(0, Math.min(left, h.width - w)) + "px";
  }
  function closeMenu(refocus) {
    if (menu.hidden) return;
    menu.hidden = true;
    if (opener) { opener.setAttribute("aria-expanded", "false"); if (refocus) opener.focus(); }
    cur = null;
  }
  function closeCat(refocus) {
    if (cat.hidden) return;
    cat.hidden = true;
    var add = bar.querySelector("[data-tab-add]");
    if (add) add.setAttribute("aria-expanded", "false");
    if (refocus && add) add.focus();
  }
  function items() { return Array.prototype.slice.call(menu.querySelectorAll("button:not([disabled])")); }

  function openMenu(btn) {
    closeCat(false); closeMenu(false);
    var tab = btn.closest("[data-tab]"), tabs = Array.prototype.slice.call(bar.querySelectorAll("[data-tab]:has([data-tab-more])"));
    cur = tab.getAttribute("data-tab"); opener = btn;
    var i = tabs.indexOf(tab);
    menu.querySelector("[data-act=left]").disabled = i <= 0;
    menu.querySelector("[data-act=right]").disabled = i >= tabs.length - 1;
    var def = menu.querySelector("[data-act=default]");
    def.disabled = tab.hasAttribute("data-default");
    def.textContent = tab.hasAttribute("data-default") ? "Default tab" : "Set as default";
    menu.hidden = false; btn.setAttribute("aria-expanded", "true");
    place(menu, btn);
    var first = items()[0]; if (first) first.focus();
  }

  function rename() {
    var tab = bar.querySelector('[data-tab="' + cur + '"]'), span = tab.querySelector("[data-tab-label]"), key = cur;
    closeMenu(false);
    var input = document.createElement("input");
    input.type = "text"; input.className = "rtab-rename"; input.maxLength = 24; input.value = tab.getAttribute("data-label");
    input.setAttribute("aria-label", "Tab name"); input.setAttribute("data-rename", key);
    span.replaceWith(input); input.focus(); input.select();
    var done = false;
    function finish(save) {
      if (done) return; done = true;
      if (save && input.value.trim() !== tab.getAttribute("data-label")) post("rename", key, input.value.trim());
      else input.replaceWith(span);
    }
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
    input.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); });
  }

  function bind() {
  bar.addEventListener("click", function (e) {
    var more = e.target.closest("[data-tab-more]");
    if (more) { e.preventDefault(); if (!menu.hidden && opener === more) closeMenu(true); else openMenu(more); return; }
    var add = e.target.closest("[data-tab-add]");
    if (add) {
      e.preventDefault();
      if (!cat.hidden) { closeCat(true); return; }
      closeMenu(false); cat.hidden = false; add.setAttribute("aria-expanded", "true"); place(cat, add);
      var first = cat.querySelector("[data-cat]"); if (first) select(first.getAttribute("data-cat")); if (first) first.focus();
    }
  });
  menu.addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]"); if (!b || b.disabled) return;
    var act = b.getAttribute("data-act"), key = cur;
    if (act === "rename") rename(); else post(act, key);
  });
  menu.addEventListener("keydown", function (e) {
    var list = items(), i = list.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[i <= 0 ? list.length - 1 : i - 1].focus(); }
    else if (e.key === "Tab") closeMenu(false);
  });

  function select(key) {
    Array.prototype.forEach.call(cat.querySelectorAll("[data-cat]"), function (b) { b.classList.toggle("is-on", b.getAttribute("data-cat") === key); });
    Array.prototype.forEach.call(cat.querySelectorAll("[data-cat-pane]"), function (p) { p.hidden = p.getAttribute("data-cat-pane") !== key; });
  }
  cat.addEventListener("mouseover", function (e) { var b = e.target.closest("[data-cat]"); if (b) select(b.getAttribute("data-cat")); });
  cat.addEventListener("focusin", function (e) { var b = e.target.closest("[data-cat]"); if (b) select(b.getAttribute("data-cat")); });
  cat.addEventListener("click", function (e) {
    var b = e.target.closest("[data-cat]"); if (b) { e.preventDefault(); select(b.getAttribute("data-cat")); return; }   // the catalogue never opens a page
    var go = e.target.closest("[data-cat-add]"); if (go) post("add", go.getAttribute("data-cat-add"));
  });
  cat.addEventListener("keydown", function (e) {
    var list = Array.prototype.slice.call(cat.querySelectorAll("[data-cat]")), i = list.indexOf(document.activeElement);
    if (i < 0) return;
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[i <= 0 ? list.length - 1 : i - 1].focus(); }
  });

  // a long press on a tab (touch) opens its menu too
  var timer = null;
  bar.addEventListener("touchstart", function (e) {
    var tab = e.target.closest("[data-tab]"), more = tab && tab.querySelector("[data-tab-more]");
    if (!more) return;
    timer = setTimeout(function () { openMenu(more); }, 550);
  }, { passive: true });
  ["touchend", "touchmove", "touchcancel"].forEach(function (n) { bar.addEventListener(n, function () { clearTimeout(timer); }, { passive: true }); });
  }
  function init() {
    var b = document.querySelector("[data-record-pills][data-manage='1']");
    if (!b) { bar = menu = cat = head = null; return; }
    bar = b; slug = bar.getAttribute("data-tabs"); head = bar.closest("[data-record-head]");
    menu = head.querySelector("[data-tab-menu]"); cat = head.querySelector("[data-tab-cat]"); cur = null; opener = null;
    if (!bar._tabsBound) { bar._tabsBound = true; bind(); }
  }
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || !menu) return;
    if (!menu.hidden) { e.preventDefault(); closeMenu(true); } else if (!cat.hidden) { e.preventDefault(); closeCat(true); }
  });
  document.addEventListener("click", function (e) {
    if (!menu) return;
    if (!menu.hidden && !menu.contains(e.target) && !e.target.closest("[data-tab-more]")) closeMenu(false);
    if (!cat.hidden && !cat.contains(e.target) && !e.target.closest("[data-tab-add]")) closeCat(false);
  });
  document.addEventListener("crewboss:page", init);
  init();
})();
