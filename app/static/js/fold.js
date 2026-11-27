// Fold-away (task #582): an action that removes or moves an item updates IN PLACE — the item collapses, what is
// below slides up, focus moves to the next item, and the page never reloads or jumps to the top. The same
// behaviour as the ERP's remove-in-place (mallen-erp #583), same convention and motion, so both surfaces feel alike.
//
// THE CONVENTION (server half: app/fold.py)
//   [data-fold]                 an item an action can remove. Value = a KEY ("now:12") when it lives in a
//                               [data-fold-root] view, empty for a stand-alone item (a change card, an event row).
//   [data-fold-section]         the list the items sit in. Kept current after every removal/insert:
//                                 [data-fold-count]        its live item count,
//                                 [data-fold-placeholder]  shown only while the list is empty,
//                                 [data-fold-hide-empty]   (on the section itself) the whole section goes when empty.
//   [data-fold-root]            a view the server re-renders whole (the board, Focus, the plan, the checklist). When a
//                               re-render of it arrives, items are matched BY KEY: a key that vanished folds out, a key
//                               that appeared folds in, the rest are replaced under the reader without moving the page.
//   An htmx request aimed at ONE item (hx-target="closest [data-fold]" hx-swap="outerHTML") that answers an EMPTY body
//   folds that item away. Its answer may also carry <template data-fold-into="#sel" data-fold-pos="afterbegin"> blocks:
//   rows to insert into another list (an item that MOVES to another section on the same page).
//
// Motion (mallen-design §4): 220 ms, height + opacity only, critically damped cubic-bezier(.25,.1,.25,1) out and its
// mirror in. prefers-reduced-motion: no motion at all, the change is instant. Styles: css/fold.css.
(function () {
  "use strict";

  var ITEM = "[data-fold]";
  var DUR = 220;

  // ---- pure helpers (unit-tested in tests/fold.test.mjs) -------------------------------------------------------
  // Which keys leave, which arrive, which stay.
  function diffKeys(oldKeys, newKeys) {
    var o = new Set(oldKeys), n = new Set(newKeys);
    return {
      leaving: new Set(oldKeys.filter(function (k) { return !n.has(k); })),
      entering: new Set(newKeys.filter(function (k) { return !o.has(k); })),
      staying: new Set(oldKeys.filter(function (k) { return n.has(k); })),
    };
  }
  // Where focus should land when the item at `index` leaves: the next item that stays, else the previous one.
  function pickFocusKey(orderedKeys, index, leaving) {
    for (var i = index + 1; i < orderedKeys.length; i++) if (!leaving.has(orderedKeys[i])) return orderedKeys[i];
    for (var j = index - 1; j >= 0; j--) if (!leaving.has(orderedKeys[j])) return orderedKeys[j];
    return null;
  }
  // How many items a list really holds: live ones that are not nested inside another item of the same list (a row's
  // own buttons are an item too — "cd:5" inside "dec:5" — and must not be counted as a second row).
  // `o.live(n)` and `o.parentItem(n)` (the nearest enclosing item, or null) are passed in so this stays testable.
  function countTopLevel(nodes, section, o) {
    return nodes.filter(function (n) {
      if (!o.live(n)) return false;
      var a = o.parentItem(n);
      return !(a && a !== section && (!section.contains || section.contains(a)));
    }).length;
  }
  // The <template data-fold-into> blocks of a response, and whatever is left of it.
  function splitResponse(text) {
    var tpl = document.createElement("template");
    tpl.innerHTML = text || "";
    var blocks = [];
    Array.prototype.slice.call(tpl.content.querySelectorAll("template[data-fold-into]")).forEach(function (t) {
      blocks.push({ into: t.getAttribute("data-fold-into"), pos: t.getAttribute("data-fold-pos") || "beforeend", frag: t.content });
      t.parentNode.removeChild(t);
    });
    var removes = Array.prototype.slice.call(tpl.content.querySelectorAll("template[data-fold-remove]")).map(function (t) {
      var q = t.getAttribute("data-fold-remove");
      t.parentNode.removeChild(t);
      return q;
    });
    return { blocks: blocks, removes: removes, rest: tpl.innerHTML.trim(), tpl: tpl };
  }

  if (typeof module === "object" && module.exports) module.exports = { diffKeys: diffKeys, pickFocusKey: pickFocusKey, countTopLevel: countTopLevel };
  if (typeof document === "undefined") return;

  // ---- DOM ---------------------------------------------------------------------------------------------------------
  function reduced() { return !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches); }
  function keyOf(el) { return el.getAttribute("data-fold") || ""; }
  function cssq(v) { return String(v).replace(/["\\]/g, "\\$&"); }
  function setHidden(el, hide) { el.hidden = hide; el.style.display = hide ? "none" : ""; }
  function live(n) {
    return n && n.nodeType === 1 && !n.hidden && !n.classList.contains("fold-collapsed") && !n.__foldGoing;
  }
  function sibling(el, dir, skip) {
    for (var n = dir > 0 ? el.nextElementSibling : el.previousElementSibling; n;
         n = dir > 0 ? n.nextElementSibling : n.previousElementSibling) {
      if (n.matches(ITEM) && live(n) && !(skip && skip.has(keyOf(n)))) return n;
    }
    return null;
  }
  var FOCUSABLE = 'a[href],button:not([disabled]),input:not([type=hidden]):not([disabled]),select,textarea,[tabindex]:not([tabindex="-1"])';
  function focusables(node) { return Array.prototype.slice.call(node.querySelectorAll(FOCUSABLE)); }
  function focusNode(node) {
    if (!node) return;
    var t = focusables(node)[0] || node;
    if (t === node && !node.hasAttribute("tabindex")) node.setAttribute("tabindex", "-1");
    try { t.focus({ preventScroll: true }); } catch (_) {}
  }
  function focusHeading(section) {
    var h = section && section.querySelector("h2,h3,[data-fold-heading]");
    if (!h) return;
    if (!h.hasAttribute("tabindex")) h.setAttribute("tabindex", "-1");
    try { h.focus({ preventScroll: true }); } catch (_) {}
  }
  function sectionOf(el) { return el && el.closest ? el.closest("[data-fold-section]") : null; }

  // Keep a list's count, empty-state and visibility true after items came or went.
  function refreshSection(section) {
    if (!section) return;
    var left = countTopLevel(Array.prototype.filter.call(section.querySelectorAll(ITEM), function (n) { return sectionOf(n) === section; }),
      section, { live: live, parentItem: function (n) { return n.parentElement && n.parentElement.closest(ITEM); } });
    Array.prototype.forEach.call(section.querySelectorAll("[data-fold-count]"), function (c) {
      if (sectionOf(c) === section) c.textContent = left;
    });
    Array.prototype.forEach.call(section.querySelectorAll("[data-fold-placeholder]"), function (p) {
      if (sectionOf(p) === section) setHidden(p, left > 0);
    });
    if (section.hasAttribute("data-fold-hide-empty")) setHidden(section, left === 0);
  }

  function rowGap(parent) {
    var cs = parent && getComputedStyle(parent);
    return cs && /flex|grid/.test(cs.display) ? (parseFloat(cs.rowGap) || 0) : 0;
  }

  // Collapse an item (height + opacity), then call done(). A flex/grid gap is cancelled by a negative margin so the
  // space under it closes with it instead of snapping shut at the end.
  function collapse(el, done) {
    el.__foldGoing = true;
    if (reduced()) { done(); return; }
    var row = el.tagName === "TR";
    if (!row) {
      el.style.setProperty("--fold-mb", -rowGap(el.parentNode) + "px");
      el.style.height = el.offsetHeight + "px";
    }
    el.classList.add("fold-anim");
    void el.offsetHeight;                       // commit the start height before the end state lands
    el.classList.add("fold-collapsed");
    var fired = false;
    function once() { if (fired) return; fired = true; done(); }
    el.addEventListener("transitionend", function (e) { if (e.target === el && e.propertyName === (row ? "opacity" : "height")) once(); });
    setTimeout(once, DUR + 60);                 // transitionend can be skipped (hidden tab, display:none ancestor)
  }

  // Fold an item IN: measure it at full size, start collapsed, expand. Mirror of collapse().
  function expand(el) {
    if (reduced() || !el.isConnected) return;
    var row = el.tagName === "TR";
    var h = el.offsetHeight;
    if (!row) el.style.setProperty("--fold-mb", -rowGap(el.parentNode) + "px");
    el.classList.add("fold-anim", "fold-collapsed", "fold-in");
    void el.offsetHeight;
    el.classList.remove("fold-collapsed");
    if (!row) el.style.height = h + "px";
    var fired = false;
    function end() {
      if (fired) return; fired = true;
      el.classList.remove("fold-anim", "fold-in");
      el.style.height = ""; el.style.removeProperty("--fold-mb");
    }
    el.addEventListener("transitionend", function (e) { if (e.target === el && e.propertyName === (row ? "opacity" : "height")) end(); });
    setTimeout(end, DUR + 60);
  }

  // Insert the response's <template data-fold-into> rows into their lists, each folding in.
  function insertBlocks(blocks) {
    blocks.forEach(function (b) {
      var dest = null;
      try { dest = document.querySelector(b.into); } catch (_) {}
      if (!dest) return;
      var nodes = Array.prototype.slice.call(b.frag.children);
      if (b.pos === "afterbegin") dest.prepend(b.frag);
      else if (b.pos === "beforebegin") dest.before(b.frag);
      else if (b.pos === "afterend") dest.after(b.frag);
      else dest.append(b.frag);
      nodes.forEach(function (n) { if (window.htmx) htmx.process(n); expand(n); });
      refreshSection(sectionOf(dest) || sectionOf(nodes[0]));
    });
  }

  // Remove one item in place. `blocks` = rows to insert elsewhere in the same breath (a move).
  window.mddFoldAway = function (el, blocks) {
    if (!el || !el.parentNode || el.__foldGoing) return;
    var section = sectionOf(el);
    var ae = document.activeElement;
    // Focus only moves when it was ON the item (their button) or already lost.
    var moveFocus = !ae || ae === document.body || el.contains(ae);
    var target = sibling(el, 1) || sibling(el, -1);
    if (blocks && blocks.length) insertBlocks(blocks);
    collapse(el, function () {
      var parent = el.parentNode;
      el.remove();
      refreshSection(section);
      if (moveFocus) {
        if (target && document.body.contains(target)) focusNode(target);
        else if (section && !section.hidden) focusHeading(section);
        else { var nx = document.querySelector("[data-fold-section]:not([hidden]) h2"); if (nx) focusHeading(nx.closest("[data-fold-section]")); }
      }
      try { (parent || document.body).dispatchEvent(new CustomEvent("mdd:folded", { bubbles: true, detail: { section: section } })); } catch (_) {}
    });
  };

  // ---- a request aimed at one item, answered empty (or with rows to insert elsewhere) ---------------------------------
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var d = e.detail;
    if (!d || !d.shouldSwap || d.isError) return;
    var t = d.target, item = t && t.closest ? t.closest(ITEM) : null;
    if (!item || t !== item) return;            // a swap aimed at a CHILD is not the removal of the item
    var parts = splitResponse(d.serverResponse || "");
    if (parts.rest !== "") return;              // a 409 current card, a 422 retry: swap normally
    d.shouldSwap = false;
    // Rows to fold away as well (an override replaces the whole row, not just the buttons that were pressed).
    var gone = parts.removes.map(function (q) { try { return document.querySelector(q); } catch (_) { return null; } })
      .filter(function (n) { return n && n !== item; });
    if (gone.some(function (g) { return g.contains(item); })) {   // the pressed item lives inside a row that goes: fold the row
      insertBlocks(parts.blocks);
      gone.forEach(function (g) { window.mddFoldAway(g, []); });
    } else {
      window.mddFoldAway(item, parts.blocks);
      gone.forEach(function (g) { window.mddFoldAway(g, []); });
    }
  });

  // ---- a whole view re-rendered (the board, Focus, the plan, the checklist): reconcile by key ---------------------------
  function scrollers(el) {
    var out = [], pg = document.getElementById("page");
    for (var n = el.parentElement; n; n = n.parentElement) {
      var oy = getComputedStyle(n).overflowY;
      if ((oy === "auto" || oy === "scroll") && n.scrollHeight > n.clientHeight) out.push(n);
    }
    if (pg && out.indexOf(pg) < 0) out.push(pg);
    return out.map(function (n) { return [n, n.scrollTop]; });
  }
  function restoreScroll(list) { list.forEach(function (p) { if (p[0].scrollTop !== p[1]) p[0].scrollTop = p[1]; }); }

  function reconcile(t, text) {
    if (!window.htmx || !htmx.swap || !t.id) return false;
    if (t.__foldQueue !== undefined) { t.__foldQueue = text; return true; }   // a swap is already waiting: newest wins
    var probe = document.createElement("template");
    probe.innerHTML = text;
    var nr = probe.content.firstElementChild;
    if (!nr || nr.id !== t.id) return false;                                 // not a whole-view answer: leave to htmx

    var oldEls = Array.prototype.filter.call(t.querySelectorAll(ITEM), function (n) { return keyOf(n); });
    var oldKeys = oldEls.map(keyOf);
    var newKeys = Array.prototype.filter.call(nr.querySelectorAll(ITEM), function (n) { return keyOf(n); }).map(keyOf);
    var diff = diffKeys(oldKeys, newKeys);
    var leavers = oldEls.filter(function (n) { return diff.leaving.has(keyOf(n)) && live(n); });

    // Where focus goes. It is lost when the swap replaces the element it sits in.
    var ae = document.activeElement, keepKey = null, keepIdx = -1, moveKey = null, headId = null;
    if (ae && ae !== document.body && t.contains(ae)) {
      var host = ae.closest(ITEM);
      if (host && t.contains(host)) {
        if (diff.leaving.has(keyOf(host))) {
          var sibs = Array.prototype.filter.call(host.parentNode.children, function (n) { return n.matches(ITEM); }).map(keyOf);
          moveKey = pickFocusKey(sibs, sibs.indexOf(keyOf(host)), diff.leaving);
          var sec = sectionOf(host);
          headId = sec && sec.getAttribute("aria-labelledby");
        } else { keepKey = keyOf(host); keepIdx = focusables(host).indexOf(ae); }
      }
    }
    var animate = !reduced();
    t.__foldQueue = text;

    function apply() {
      var latest = t.__foldQueue;
      delete t.__foldQueue;
      if (!t.isConnected) return;
      var id = t.id, saved = scrollers(t);
      htmx.swap(t, latest, { swapStyle: "outerHTML", swapDelay: 0, settleDelay: 0 }, {});
      var cur = document.getElementById(id);
      restoreScroll(saved);
      if (!cur) return;
      if (animate) {
        diff.entering.forEach(function (k) {
          var n = cur.querySelector('[data-fold="' + cssq(k) + '"]');
          if (n) expand(n);
        });
      }
      var f = null;
      if (moveKey) f = cur.querySelector('[data-fold="' + cssq(moveKey) + '"]');
      if (f) focusNode(f);
      else if (headId && !keepKey) {            // the list emptied: land on its heading, not the top of the page
        var hd = document.getElementById(headId);
        if (hd) { if (!hd.hasAttribute("tabindex")) hd.setAttribute("tabindex", "-1"); try { hd.focus({ preventScroll: true }); } catch (_) {} }
      }
      if (keepKey) {
        var h = cur.querySelector('[data-fold="' + cssq(keepKey) + '"]');
        var fs = h && focusables(h);
        if (fs && fs[keepIdx]) { try { fs[keepIdx].focus({ preventScroll: true }); } catch (_) {} }
      }
      Array.prototype.forEach.call(cur.querySelectorAll("[data-fold-section]"), refreshSection);
      restoreScroll(saved);
      try { cur.dispatchEvent(new CustomEvent("mdd:reconciled", { bubbles: true })); } catch (_) {}
    }

    if (!animate || !leavers.length) { apply(); return true; }
    var pending = leavers.length;
    leavers.forEach(function (n) { collapse(n, function () { if (--pending === 0) apply(); }); });
    return true;
  }

  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var d = e.detail;
    if (!d || !d.shouldSwap || d.isError) return;
    var t = d.target;
    if (!t || !t.hasAttribute || !t.hasAttribute("data-fold-root")) return;
    var text = d.serverResponse || "";
    if (text.trim() === "") return;
    if (reconcile(t, text)) d.shouldSwap = false;
  });

  // ---- KEEP THE SCROLL WHEN A RELOAD IS UNAVOIDABLE ---------------------------------------------------------------------
  // #page (not the window) is the scroller, so the browser's own scroll restoration never applies: every plain form
  // POST that comes back as a 303, and every HX-Refresh, would land at the top. Save #page.scrollTop (keyed to the path)
  // just before leaving, restore it if the same path comes back within 20 s — unless the reader has already scrolled.
  window.crewbossKeepScroll = function () {
    var pg = document.getElementById("page");
    if (!pg) return;
    try { sessionStorage.setItem("crewboss_keep_scroll", JSON.stringify({ p: location.pathname + location.search, y: pg.scrollTop, t: Date.now() })); } catch (_) {}
  };
  (function () {
    var saved = null;
    try { saved = JSON.parse(sessionStorage.getItem("crewboss_keep_scroll") || "null"); sessionStorage.removeItem("crewboss_keep_scroll"); } catch (_) {}
    if (saved && saved.p === location.pathname + location.search && Date.now() - saved.t < 20000 && saved.y > 0) {
      var touched = false;
      ["wheel", "touchstart", "keydown"].forEach(function (ev) { window.addEventListener(ev, function () { touched = true; }, { once: true, passive: true }); });
      var put = function () { var pg = document.getElementById("page"); if (pg && !touched) pg.scrollTop = saved.y; };
      put(); document.addEventListener("DOMContentLoaded", put); window.addEventListener("load", put); setTimeout(put, 400);
    }
  })();
  document.addEventListener("submit", function (e) {      // a plain (no-JS-path) POST: the 303 comes back to this page
    var f = e.target;
    if (!e.defaultPrevented && f && f.method && f.method.toLowerCase() === "post" && !(f.hasAttribute && f.hasAttribute("hx-post"))) window.crewbossKeepScroll();
  });
  document.body.addEventListener("htmx:beforeOnLoad", function (e) {   // HX-Refresh = location.reload()
    var x = e.detail && e.detail.xhr;
    if (x && x.getResponseHeader && x.getResponseHeader("HX-Refresh") === "true") window.crewbossKeepScroll();
  });
})();
