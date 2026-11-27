// The All projects home's PROJECT TREE as a collapsible tree (owner, 2026-10-02; task #157). Same model as the topbar
// switcher (static/js/switcher.js): the server renders every card with its guide lines; this only decides which rows
// show and flips each toggle's +/−.
//   - Default view matches the switcher: the root and the accounts / top-level projects (depth <= 1) show, every
//     branch BELOW an account is folded. Depth-0 nodes are seeded open (and re-open on every load, like the
//     switcher's roots). A card with children carries a +/− toggle; opening it reveals that branch (a collapsed
//     parent hides its whole subtree).
//   - What the owner opens is remembered (localStorage key crewboss-hometree-open, best effort; distinct from the
//     switcher's), layered on top of the depth-0 defaults. Rows at depth > 1 are rendered display:none inline, so
//     the folded state holds before this runs.
//   - The toggle is a real <button aria-expanded>; it never navigates (it sits outside the card's link overlay, and
//     also stops the click).
(function () {
  "use strict";

  var STORE_KEY = "crewboss-hometree-open";

  // Pre-Crewboss key: read once, write the new key, delete the old. Remove after the 2027-01 release.
  try {
    var oldRaw = window.localStorage.getItem("mdd-hometree-open");
    if (oldRaw !== null) { if (window.localStorage.getItem(STORE_KEY) === null) window.localStorage.setItem(STORE_KEY, oldRaw); window.localStorage.removeItem("mdd-hometree-open"); }
  } catch (e) { /* private mode */ }

  function load() {
    try {
      var raw = window.localStorage.getItem(STORE_KEY);
      var ids = raw ? JSON.parse(raw) : null;
      return Array.isArray(ids) ? ids.map(String) : [];
    } catch (e) { return []; }
  }

  function save(open) {
    try { window.localStorage.setItem(STORE_KEY, JSON.stringify(Object.keys(open))); } catch (e) { /* private mode */ }
  }

  function wire(root) {
    var rows = Array.prototype.slice.call(root.querySelectorAll("[data-tree-row]"));
    if (!rows.length) return;
    var byId = {}, parentOf = {};
    rows.forEach(function (r) {
      var id = r.getAttribute("data-node-id");
      byId[id] = r;
      var pid = r.getAttribute("data-parent-id");
      parentOf[id] = (pid === null || pid === "") ? null : pid;
    });

    // Depth-0 nodes open by default (so accounts / top-level projects show), the owner's saved set layered on top.
    var open = {};
    rows.forEach(function (r) { if (r.getAttribute("data-depth") === "0") open[r.getAttribute("data-node-id")] = true; });
    load().forEach(function (id) { if (byId[id]) open[id] = true; });

    // Visible when every ancestor is open (a folded parent hides its whole subtree).
    function ancestorsOpen(id) {
      var p = parentOf[id], seen = {};
      while (p != null && !seen[p]) {
        if (!open[p]) return false;
        seen[p] = true;
        p = parentOf[p];
      }
      return true;
    }

    function apply() {
      rows.forEach(function (r) {
        var id = r.getAttribute("data-node-id");
        r.style.display = ancestorsOpen(id) ? "" : "none";
        var tog = r.querySelector("[data-tree-toggle]");
        if (tog) {
          var isOpen = !!open[id];
          tog.setAttribute("aria-expanded", isOpen ? "true" : "false");
          tog.setAttribute("aria-label", (isOpen ? "Collapse " : "Expand ") + (tog.getAttribute("data-name") || "subtree"));
          var sign = tog.querySelector("[data-tree-sign]");
          if (sign) sign.textContent = isOpen ? "−" : "+";  // − / +
        }
      });
    }

    root.addEventListener("click", function (e) {
      var tog = e.target.closest ? e.target.closest("[data-tree-toggle]") : null;
      if (!tog || !root.contains(tog)) return;
      e.preventDefault();
      e.stopPropagation();  // never let the card's link overlay fire
      var id = tog.getAttribute("data-tree-toggle");
      if (open[id]) delete open[id]; else open[id] = true;
      save(open);
      apply();
    });

    apply();
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-project-tree]"), wire);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
