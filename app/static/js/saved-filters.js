// SAVED FILTERS (#772): the "Saved" dropdown beside Filter on List, Board and Recent (partials/saved_filters.html). The sets live on the server,
// per person (/all/filters), so they follow the person across devices. One click applies a set (the page opens with its query), each row has
// rename, delete and "default" (the set a page opens with when it is opened bare), and "Save filter" stores the page's CURRENT filters.
// What a set holds is the page's own query parameters (the same keys app/saved_filters.py keeps), plus the List's visible columns.
// Plain prompt()/confirm() dialogs: small and accessible; nothing here stores anything in the browser except the List's column choice (task-list.js).
(function () {
  "use strict";
  var KEYS = { board: ["assignee", "priority", "stream", "category", "subproject", "status", "q"],
               list: ["assignee", "priority", "stream", "category", "subproject", "status", "q", "group", "sort", "dir", "done", "cols"],
               recent: ["project", "kind"], machines: ["role"] };

  function api(method, url, fields) {
    var o = { method: method, credentials: "same-origin", headers: { "HX-Request": "true" } };
    if (fields) { o.headers["Content-Type"] = "application/x-www-form-urlencoded"; o.body = new URLSearchParams(fields); }
    return fetch(url, o).then(function (r) { return r.text().then(function (t) { if (!r.ok) throw new Error(t || "That didn't work."); return t ? JSON.parse(t) : {}; }); });
  }
  function toast(m, tone) { if (window.mddToast) window.mddToast(m, tone || "error"); }

  function current(page) {
    var q = new URLSearchParams(location.search), out = {};
    KEYS[page].forEach(function (k) { if (q.get(k)) out[k] = q.get(k); });
    if (page === "list" && window.crewListCols) out.cols = window.crewListCols.visible().join(",");
    return out;
  }
  function applyUrl(page, data) {
    var q = new URLSearchParams();
    Object.keys(data || {}).forEach(function (k) { if (KEYS[page].indexOf(k) >= 0 && data[k] !== "") q.set(k, data[k]); });
    var s = q.toString();
    return location.pathname + (s ? "?" + s : "");
  }

  function load(root) {
    var page = root.getAttribute("data-page"), project = root.getAttribute("data-project") || "";
    return api("GET", "/all/filters?page=" + encodeURIComponent(page) + "&project=" + encodeURIComponent(project)).then(function (d) { render(root, d.filters || []); })
      .catch(function () { render(root, []); });
  }
  function render(root, filters) {
    var ul = root.querySelector("[data-sf-list]"), empty = root.querySelector("[data-sf-empty]");
    ul.textContent = "";
    empty.hidden = filters.length > 0;
    var page = root.getAttribute("data-page");
    filters.forEach(function (f) {
      var li = document.createElement("li"); li.className = "tl-sf-row"; li.setAttribute("data-sf-id", f.id);
      var apply = document.createElement("a"); apply.className = "tl-sf-apply"; apply.href = applyUrl(page, f.data); apply.textContent = f.name;
      apply.setAttribute("data-sf-apply", "");
      apply.addEventListener("click", function () { if (page === "list" && f.data.cols !== undefined) { try { localStorage.setItem("crewboss-list-cols", JSON.stringify(f.data.cols.split(",").filter(Boolean))); } catch (e) { /* ok */ } } });
      var def = document.createElement("button"); def.type = "button"; def.className = "tl-sf-btn"; def.setAttribute("data-sf-default", "");
      def.setAttribute("aria-pressed", f.is_default ? "true" : "false"); def.title = f.is_default ? "Default: opens with this. Click to clear." : "Make this the default"; def.textContent = f.is_default ? "★" : "☆";
      var ren = document.createElement("button"); ren.type = "button"; ren.className = "tl-sf-btn"; ren.setAttribute("data-sf-rename", ""); ren.title = "Rename"; ren.textContent = "✎";
      var del = document.createElement("button"); del.type = "button"; del.className = "tl-sf-btn"; del.setAttribute("data-sf-delete", ""); del.title = "Delete"; del.textContent = "×";
      def.addEventListener("click", function () { api("POST", "/all/filters/" + f.id + "/default", { on: f.is_default ? "0" : "1" }).then(function () { return load(root); }).catch(function (e) { toast(e.message); }); });
      ren.addEventListener("click", function () { var n = window.prompt("Rename this filter", f.name); if (n && n.trim()) api("POST", "/all/filters/" + f.id + "/rename", { name: n.trim() }).then(function () { return load(root); }).catch(function (e) { toast(e.message); }); });
      del.addEventListener("click", function () { if (window.confirm("Delete the saved filter “" + f.name + "”?")) api("POST", "/all/filters/" + f.id + "/delete").then(function () { return load(root); }).catch(function (e) { toast(e.message); }); });
      li.appendChild(apply); li.appendChild(def); li.appendChild(ren); li.appendChild(del); ul.appendChild(li);
    });
  }

  document.addEventListener("click", function (e) {
    var root = e.target.closest && e.target.closest("[data-saved-filters]");
    var menus = document.querySelectorAll("[data-sf-menu]");
    if (!root) { Array.prototype.forEach.call(menus, function (m) { m.hidden = true; }); return; }
    var menu = root.querySelector("[data-sf-menu]");
    if (e.target.closest("[data-sf-open]")) {
      var show = menu.hidden; menu.hidden = !show; root.querySelector("[data-sf-open]").setAttribute("aria-expanded", show ? "true" : "false");
      if (show) load(root);
      return;
    }
    if (e.target.closest("[data-sf-save]")) {
      var page = root.getAttribute("data-page"), name = window.prompt("Name this filter");
      if (!name || !name.trim()) return;
      api("POST", "/all/filters", { page: page, project: root.getAttribute("data-project") || "", name: name.trim(), data: JSON.stringify(current(page)) })
        .then(function () { toast("Saved “" + name.trim() + "”", "info"); return load(root); }).catch(function (err) { toast(err.message); });
    }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") Array.prototype.forEach.call(document.querySelectorAll("[data-sf-menu]"), function (m) { m.hidden = true; }); });
  window.crewSavedFilters = { current: current, applyUrl: applyUrl };
})();
