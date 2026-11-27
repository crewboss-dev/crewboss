// Hand-written, not built — the workflow side panel, kanban drag-and-drop, and a small toast helper.
// Loaded after htmx + Alpine (both `defer`, so this must be `defer` too to run after them, but it
// doesn't depend on either for the parts below).

(function () {
  "use strict";

  // Global Alpine stores are registered in an inline <head> script in base.html — see the comment
  // there for why (Alpine can self-start before this `defer`red file even runs).
  // Workflow canvas -> side panel. The shared canvas (static/canvas) owns the panel and brings it into view; the
  // Dashboard's per-step Tasks section inside it reads $store.wf.pane: a card's "+" opens its new-request form.
  function openStep(id, pane) {
    var store = window.Alpine && Alpine.store("wf");
    var state = store || window.__mddStores.wf;
    state.active = id;
    state.pane = pane;
    if (pane === "new") {
      setTimeout(function () {
        var input = document.querySelector('.mallen-canvas .mc-panel form:not([style*="display: none"]) input[name="title"]');
        if (input && window.matchMedia("(pointer: fine)").matches) input.focus();
      }, 60);
    }
  }
  window.addEventListener("wf:node-click", function (evt) { openStep(evt.detail.id, "detail"); });
  window.addEventListener("wf:step-tasks", function (evt) { openStep(evt.detail.id, "tasks"); });
  window.addEventListener("wf:step-new-task", function (evt) { openStep(evt.detail.id, "new"); });
  window.addEventListener("wf:close", function () { openStep(null, "detail"); });

  // A #fragment naming a closed <details> (an integration row, #393: the diagram's names link to "#i-<id>") opens it.
  function openHashTarget() {
    var id = decodeURIComponent((location.hash || "").slice(1));
    var el = id && document.getElementById(id);
    if (el && el.tagName === "DETAILS" && !el.open) { el.open = true; el.scrollIntoView({ block: "start" }); }
  }
  window.addEventListener("hashchange", openHashTarget);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", openHashTarget);
  else openHashTarget();

  // (Light / Dark / Auto lives in the avatar menu now: static/js/chrome.js, #479.)

  // --- toast (used after an htmx swap signals an error via HX-Trigger) ---------------------
  window.mddToast = function (message, tone) {
    var root = document.getElementById("toast-root");
    if (!root) return;
    var el = document.createElement("div");
    el.className =
      "press pointer-events-auto rounded-lg px-3.5 py-2 text-small text-white shadow-float " +
      (tone === "error" ? "bg-flame" : "bg-charcoal");
    el.textContent = message;
    root.appendChild(el);
    // A long message (a "now delete this line on the server" instruction) stays up long enough to read.
    var ms = Math.min(9000, Math.max(2600, String(message).length * 55));
    setTimeout(function () {
      el.style.transition = "opacity 200ms ease-physics-out";
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 220);
    }, ms);
  };

  // A page write the server refused (validation) answers 4xx + HX-Trigger {"mdd:flash": {...}}:
  // show its message instead of the generic one.
  document.body.addEventListener("mdd:flash", function (evt) {
    var d = evt.detail || {};
    window.mddToast(String(d.message || "Something went wrong — try again."), d.tone || "error");
  });

  document.body.addEventListener("htmx:responseError", function (evt) {
    var xhr = evt.detail && evt.detail.xhr;
    if (xhr && xhr.getResponseHeader("HX-Trigger")) return;  // mdd:flash already told the owner why
    window.mddToast("Something went wrong — try again.", "error");
  });

  // --- checklist reorder: pointer-driven (mouse + touch, one thumb), plus arrow keys on the handle ---------
  // The row tracks the pointer 1:1; on release the new order is posted and the server re-renders the list.
  // Every page lives under its project ("/mallen-erp/tasks/…"); base.html puts that prefix on <body>.
  function base() { return document.body.dataset.base || ""; }

  function postOrder(list) {
    var ids = Array.prototype.map.call(list.querySelectorAll(":scope > li[data-subtask-id]"),
      function (li) { return li.dataset.subtaskId; });
    htmx.ajax("POST", base() + "/tasks/" + list.dataset.sortable + "/subtasks/reorder", {
      target: "#task-checklist", swap: "outerHTML", values: { order: ids.join(",") },
    });
  }

  document.addEventListener("pointerdown", function (evt) {
    var handle = evt.target.closest("[data-sortable] .drag-handle");
    if (!handle || evt.button > 0) return;
    var li = handle.closest("li[data-subtask-id]");
    var list = li.parentElement;
    evt.preventDefault();
    handle.setPointerCapture(evt.pointerId);
    var startY = evt.clientY, moved = false;
    li.classList.add("is-dragging");
    function onMove(e) {
      var dy = e.clientY - startY;
      if (Math.abs(dy) > 3) moved = true;
      li.style.transform = "translateY(" + dy + "px)";
      var sibs = Array.prototype.filter.call(list.querySelectorAll(":scope > li[data-subtask-id]"), function (x) { return x !== li; });
      for (var i = 0; i < sibs.length; i++) {
        var r = sibs[i].getBoundingClientRect();
        var mid = r.top + r.height / 2;
        var me = li.getBoundingClientRect();
        var meMid = me.top + me.height / 2;
        if (dy < 0 && meMid < mid && sibs[i].compareDocumentPosition(li) & Node.DOCUMENT_POSITION_FOLLOWING) {
          list.insertBefore(li, sibs[i]); startY = e.clientY; li.style.transform = ""; break;
        }
        if (dy > 0 && meMid > mid && sibs[i].compareDocumentPosition(li) & Node.DOCUMENT_POSITION_PRECEDING) {
          list.insertBefore(li, sibs[i].nextSibling); startY = e.clientY; li.style.transform = ""; break;
        }
      }
    }
    function onUp() {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      li.classList.remove("is-dragging");
      li.style.transform = "";
      if (moved) postOrder(list);
    }
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });

  document.addEventListener("keydown", function (evt) {
    var handle = evt.target.closest && evt.target.closest("[data-sortable] .drag-handle");
    if (!handle || (evt.key !== "ArrowUp" && evt.key !== "ArrowDown")) return;
    evt.preventDefault();
    var li = handle.closest("li[data-subtask-id]");
    var list = li.parentElement;
    var sib = evt.key === "ArrowUp" ? li.previousElementSibling : li.nextElementSibling;
    while (sib && !sib.dataset.subtaskId) sib = evt.key === "ArrowUp" ? sib.previousElementSibling : sib.nextElementSibling;
    if (!sib) return;
    list.insertBefore(li, evt.key === "ArrowUp" ? sib : sib.nextSibling);
    var id = li.dataset.subtaskId;
    postOrder(list);
    document.body.addEventListener("htmx:afterSwap", function refocus() {
      document.body.removeEventListener("htmx:afterSwap", refocus);
      var h = document.querySelector('li[data-subtask-id="' + id + '"] .drag-handle');
      if (h) h.focus();
    });
  });

  // --- kanban drag-and-drop, with a drop line showing exactly where the card will land -------------------
  // PLAN (data-plan-col, the global home): free order; a drop moves the card there and pins it (owner override).
  // BOARD (data-status-col): cards sort by priority band (Must → Should → Could → Won't), then rank. A drag moves
  //   a card to the pointer's place INSIDE ITS OWN BAND: same column = re-rank, another column = status + rank.
  //   Priority itself is changed in the drawer, never by dragging (the line can't leave the card's band).
  // Touch has no HTML5 drag: phones move cards with the status menu on the card.
  var PRIORITIES = ["must", "should", "could", "wont"];  // data-sort's band index -> priority
  var drag = null;          // { card, id, key: [bucket, rank], fromCol }
  var line = null;          // the one drop-line element

  function sortKey(el) {
    var p = (el.dataset.sort || "3:0").split(":");
    return [parseInt(p[0], 10) || 0, parseFloat(p[1]) || 0];
  }
  function keyLess(a, b) { return a[0] < b[0] || (a[0] === b[0] && a[1] < b[1]); }
  function cardsIn(col) {
    return Array.prototype.filter.call(col.querySelectorAll(":scope > [data-task-id]"), function (c) { return drag && c !== drag.card; });
  }
  function clearLine() {
    if (line && line.parentNode) line.parentNode.removeChild(line);
    document.querySelectorAll("[data-drop-col].is-over").forEach(function (c) { c.classList.remove("is-over"); });
    document.querySelectorAll(".drop-before, .drop-end").forEach(function (c) { c.classList.remove("drop-before", "drop-end"); });
  }
  function placeLine(col, before) {
    if (col.dataset.planCol !== undefined) {
      // Plan columns are grids (#267): a line element would become a grid cell and reflow the grid mid-drag, so
      // mark the card the drop lands before (or the column's end) instead.
      clearLine();
      if (before) before.classList.add("drop-before"); else col.classList.add("drop-end");
      col.classList.add("is-over");
      return;
    }
    if (!line) { line = document.createElement("div"); line.className = "drop-line"; line.setAttribute("aria-hidden", "true"); }
    if (before) col.insertBefore(line, before); else col.appendChild(line);
    col.classList.add("is-over");
  }
  // The card the line sits before (null = the end), for this column and pointer.
  // Within the card's own band: the first band card below the pointer; past the band, the first card of the next band.
  function insertionPoint(col, clientY, clientX) {
    if (col.dataset.planCol !== undefined) {  // the global PLAN: free order, the line follows the pointer
      // Plan columns are GRIDS (#267): reading order is row by row, so a card comes before the pointer when its
      // row is above the pointer, or it's on the pointer's row and to its left.
      var pc = cardsIn(col);
      for (var j = 0; j < pc.length; j++) {
        var pr = pc[j].getBoundingClientRect();
        if (clientY < pr.top) return pc[j];
        if (clientY <= pr.bottom && (clientX === undefined || clientX < pr.left + pr.width / 2)) return pc[j];
      }
      return null;
    }
    var cards = cardsIn(col), band = drag.key[0], afterBand = null;
    for (var i = 0; i < cards.length; i++) {
      var b = sortKey(cards[i])[0];
      if (b < band) continue;
      if (b > band) { afterBand = cards[i]; break; }
      var r = cards[i].getBoundingClientRect();
      if (clientY < r.top + r.height / 2) return cards[i];
    }
    return afterBand;
  }
  // The band card just above the drop point (null = the top of the band).
  function bandCardAbove(col, before) {
    var cards = cardsIn(col), above = null;
    for (var i = 0; i < cards.length; i++) {
      if (cards[i] === before) break;
      if (sortKey(cards[i])[0] === drag.key[0]) above = cards[i];
    }
    return above;
  }

  document.body.addEventListener("dragstart", function (evt) {
    var card = evt.target.closest && evt.target.closest("[data-task-id]");
    if (!card) return;
    drag = { card: card, id: card.dataset.taskId, key: sortKey(card), fromCol: card.closest("[data-drop-col]") };
    evt.dataTransfer.setData("text/task-id", card.dataset.taskId);
    evt.dataTransfer.effectAllowed = "move";
    requestAnimationFrame(function () { card.classList.add("is-dragging"); });
  });

  document.body.addEventListener("dragend", function () {
    if (drag) drag.card.classList.remove("is-dragging");
    clearLine();
    drag = null;
  });

  document.body.addEventListener("dragover", function (evt) {
    var col = evt.target.closest && evt.target.closest("[data-drop-col]");
    if (!col || !drag) return;
    evt.preventDefault();
    evt.dataTransfer.dropEffect = "move";
    document.querySelectorAll("[data-drop-col].is-over").forEach(function (c) { if (c !== col) c.classList.remove("is-over"); });
    var before = insertionPoint(col, evt.clientY, evt.clientX);
    if (line && line.parentNode === col && line.nextElementSibling === before) return;  // unchanged: no reflow
    placeLine(col, before);
  });

  document.body.addEventListener("dragleave", function (evt) {
    var col = evt.target.closest && evt.target.closest("[data-drop-col]");
    if (col && !col.contains(evt.relatedTarget)) { col.classList.remove("is-over"); if (line && line.parentNode === col) col.removeChild(line); }
  });

  document.body.addEventListener("drop", function (evt) {
    var col = evt.target.closest && evt.target.closest("[data-drop-col]");
    if (!col || !drag) return;
    evt.preventDefault();
    var before = insertionPoint(col, evt.clientY, evt.clientX);
    var id = drag.id, sameCol = col === drag.fromCol;
    clearLine();
    var board = document.getElementById("board");
    var qs = board ? board.dataset.query || "" : "";
    if (col.dataset.planCol !== undefined) {
      // The global PLAN (home): the card above the line decides the place; dropping pins it (the owner's call).
      var pcards = cardsIn(col), pabove = null;
      for (var k = 0; k < pcards.length; k++) { if (pcards[k] === before) break; pabove = pcards[k]; }
      var pv = { task_id: id, bucket: col.dataset.planCol };
      if (pabove) pv.after_id = pabove.dataset.taskId;
      htmx.ajax("POST", "/all/plan/move", { target: "#global-plan", swap: "outerHTML", values: pv });
      return;
    }
    if (col.dataset.statusCol === undefined) return;
    var above = bandCardAbove(col, before);
    var place = above ? { after_id: above.dataset.taskId } : { top: "1" };
    // An account's rolled-up board (task #148): each card writes to its OWN project (data-task-base).
    var tb = drag.card.dataset.taskBase || base();
    if (sameCol) {  // re-rank inside the band; the priority stays the card's own
      place.priority = PRIORITIES[drag.key[0]];
      htmx.ajax("POST", tb + "/tasks/" + id + "/priority?" + qs, { target: "#board", swap: "outerHTML", values: place });
      return;
    }
    place.status = col.dataset.statusCol;
    htmx.ajax("POST", tb + "/tasks/" + id + "/status?" + qs, { target: "#board", swap: "outerHTML", values: place });
  });
})();
