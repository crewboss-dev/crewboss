// THE LIST pill (#772): the behaviour of partials/task_table.html and pages/list.html. The server renders the table and does the sorting,
// filtering and grouping (a header is a link); this adds what a link cannot do:
//   - columns: the picker (the "..." menu or the header's last cell) shows / hides them; the choice is the person's, kept in localStorage
//     (try/catch: private mode just forgets it). A `cols` parameter in the URL (a saved filter) wins and is stored.
//   - rows: the chevron shows a task's checklist steps; a group header folds its rows; ticking rows opens the bulk bar.
//   - writes: the status pill's select and the bulk bar post to the SAME routes the Board's drag and the drawer use, one task at a time; "+ Create"
//     adds a title-only task. After a write the table refreshes (mdd:board-stale), it never reloads the page.
// The table is replaced by htmx on a refresh, so everything is delegated from document and re-applied after a swap. No hx-select anywhere.
(function () {
  "use strict";
  var KEY = "crewboss-list-cols";
  var ALL = ["type", "status", "priority", "assignee", "reporter", "progress", "created", "updated"];

  function table() { return document.querySelector("[data-tl-table]"); }
  function getSaved() { try { var v = JSON.parse(localStorage.getItem(KEY) || "null"); return Array.isArray(v) ? v : null; } catch (e) { return null; } }
  function setSaved(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* not remembered */ } }

  function visibleCols() {
    var t = table();
    if (!t) return [];
    return ALL.filter(function (c) { return !t.classList.contains("tl-hide-" + c); });
  }
  function applyCols(vis) {
    var t = table();
    if (!t) return;
    ALL.forEach(function (c) { t.classList.toggle("tl-hide-" + c, vis.indexOf(c) < 0); });
    Array.prototype.forEach.call(document.querySelectorAll("[data-col-toggle]"), function (b) { b.checked = vis.indexOf(b.getAttribute("data-col-toggle")) >= 0; });
  }
  function initCols() {
    var t = table();
    if (!t) return;
    var m = /[?&]cols=([^&]*)/.exec(location.search);
    if (m) { var fromUrl = decodeURIComponent(m[1]).split(",").filter(function (c) { return ALL.indexOf(c) >= 0; }); setSaved(fromUrl); applyCols(fromUrl); return; }
    var saved = getSaved();
    if (saved) applyCols(saved.filter(function (c) { return ALL.indexOf(c) >= 0; }));
    else applyCols(visibleCols());                 // the server's default set; syncs the picker
  }

  function toast(msg, tone) { if (window.mddToast) window.mddToast(msg, tone || "error"); }
  function post(url, fields) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "HX-Request": "true", "Content-Type": "application/x-www-form-urlencoded" },
                        body: new URLSearchParams(fields) })
      .then(function (r) { return r.ok ? r : r.text().then(function (t) { throw new Error(t || "That didn't work."); }); });
  }
  function refresh() { if (document.body) document.body.dispatchEvent(new CustomEvent("mdd:board-stale", { bubbles: true })); }

  // ── the "..." menu / the header's column button ────────────────────────────────────────────────────────────────
  document.addEventListener("click", function (e) {
    var open = e.target.closest && e.target.closest("[data-cols-open]");
    var menu = document.querySelector("[data-tl-menu]");
    if (!menu) return;
    if (open) { var show = menu.hidden; menu.hidden = !show; Array.prototype.forEach.call(document.querySelectorAll("[data-cols-open]"), function (b) { b.setAttribute("aria-expanded", show ? "true" : "false"); }); return; }
    if (!menu.hidden && !e.target.closest("[data-tl-menu]")) menu.hidden = true;
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { var m = document.querySelector("[data-tl-menu]"); if (m) m.hidden = true; } });
  document.addEventListener("change", function (e) {
    var tg = e.target.closest && e.target.closest("[data-col-toggle]");
    if (!tg) return;
    var vis = ALL.filter(function (c) { var b = document.querySelector('[data-col-toggle="' + c + '"]'); return b && b.checked; });
    setSaved(vis); applyCols(vis);
  });

  // ── a number or title opens the drawer in place (htmx on the row does the loading); without script it is a link to the Board's drawer ──
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[data-open]");
    if (a && !e.metaKey && !e.ctrlKey && !e.shiftKey && e.button === 0 && a.closest("[data-task]")) e.preventDefault();
  });

  // ── checklist steps, group folds ───────────────────────────────────────────────────────────────────────────────
  function groupClosed(row) {
    var g = row.getAttribute("data-g");
    if (!g) return false;
    var h = document.querySelector('.tl-group[data-group="' + g + '"] [data-group-toggle]');
    return !!h && h.getAttribute("aria-expanded") === "false";
  }
  document.addEventListener("click", function (e) {
    var ch = e.target.closest && e.target.closest("[data-expand]");
    if (ch) {
      var id = ch.getAttribute("data-expand"), on = ch.getAttribute("aria-expanded") !== "true";
      ch.setAttribute("aria-expanded", on ? "true" : "false");
      Array.prototype.forEach.call(document.querySelectorAll('[data-steps-of="' + id + '"]'), function (r) { r.hidden = !on; });
      return;
    }
    var gt = e.target.closest && e.target.closest("[data-group-toggle]");
    if (gt) {
      var key = gt.getAttribute("data-group-toggle"), open = gt.getAttribute("aria-expanded") === "false";
      gt.setAttribute("aria-expanded", open ? "true" : "false");
      Array.prototype.forEach.call(document.querySelectorAll('.tl-row[data-g="' + key + '"]'), function (r) {
        r.hidden = !open;
        var c = r.querySelector("[data-expand]");
        Array.prototype.forEach.call(document.querySelectorAll('[data-steps-of="' + r.getAttribute("data-task") + '"]'), function (s) { s.hidden = !(open && c && c.getAttribute("aria-expanded") === "true"); });
      });
    }
  });

  // ── selection and the bulk bar ─────────────────────────────────────────────────────────────────────────────────
  function picked() { return Array.prototype.slice.call(document.querySelectorAll("[data-pick]:checked")); }
  function syncBulk() {
    var bar = document.querySelector("[data-tl-bulk]");
    if (!bar) return;
    var n = picked().length;
    bar.hidden = n === 0;
    var c = bar.querySelector("[data-bulk-count]"); if (c) c.textContent = n + " selected";
  }
  document.addEventListener("change", function (e) {
    var all = e.target.closest && e.target.closest("[data-pick-all]");
    if (all) { Array.prototype.forEach.call(document.querySelectorAll("[data-pick]"), function (b) { if (!b.closest("tr").hidden) b.checked = all.checked; }); syncBulk(); return; }
    if (e.target.closest && e.target.closest("[data-pick]")) syncBulk();
  });
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-bulk-clear]")) { Array.prototype.forEach.call(document.querySelectorAll("[data-pick], [data-pick-all]"), function (b) { b.checked = false; }); syncBulk(); }
  });
  document.addEventListener("change", function (e) {
    var sel = e.target.closest && e.target.closest("[data-bulk]");
    if (!sel || !sel.value) return;
    var field = sel.getAttribute("data-bulk"), value = sel.value, rows = picked().map(function (b) { return b.closest("[data-task]"); });
    sel.value = "";
    var chain = Promise.resolve(), failed = 0;
    rows.forEach(function (row) {
      chain = chain.then(function () {
        if (field === "priority" && row.getAttribute("data-priority") === value) return null;   // unchanged: leave its place alone
        var o = {}; o[field] = value;
        if (field === "priority") o.end = "1";                                                  // a real change goes to the END of the new band
        return post(row.getAttribute("data-base") + "/tasks/" + row.getAttribute("data-task") + "/" + field, o).catch(function (err) { failed++; toast(err.message); });
      });
    });
    chain.then(function () { if (!failed) toast(rows.length + " updated", "info"); refresh(); });
  });

  // ── the status pill's select: the same route the Board's drag uses ─────────────────────────────────────────────
  document.addEventListener("change", function (e) {
    var s = e.target.closest && e.target.closest("select[data-set]");
    if (!s) return;
    var o = {}; o[s.getAttribute("data-set")] = s.value;
    post(s.getAttribute("data-url"), o).then(refresh).catch(function (err) { toast(err.message); refresh(); });
  });

  // ── "+ Create": a title-only row ───────────────────────────────────────────────────────────────────────────────
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-create-open]");
    if (b) { var f = b.parentNode.querySelector(".tl-create-form"); f.hidden = !f.hidden; b.hidden = !f.hidden; var i = f.querySelector("input[name=title]"); if (i && !f.hidden) i.focus(); return; }
    if (e.target.closest && e.target.closest("[data-refresh]")) refresh();
  });
  document.addEventListener("submit", function (e) {
    var f = e.target.closest && e.target.closest("[data-create]");
    if (!f) return;
    e.preventDefault();
    var o = { title: f.elements.title.value.trim(), stream: "feature", priority: "should", assignee: "ai" };
    if (f.elements.target) o.target = f.elements.target.value;
    if (!o.title) return;
    post(f.getAttribute("data-url"), o).then(function () { f.elements.title.value = ""; refresh(); }).catch(function (err) { toast(err.message); });
  });

  document.addEventListener("htmx:afterSettle", function (e) { if (e.target && e.target.id === "tasklist") { initCols(); syncBulk(); } });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initCols); else initCols();
  window.crewListCols = { visible: visibleCols, apply: applyCols };
})();
