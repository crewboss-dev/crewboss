// THE TASK VIEWS (#772): Board and List are two looks at the same tasks, so they SHARE their filters and the person's last choice is remembered.
//   - On a Board or List page, the record head's List and Board pills carry the page's filter parameters, so switching keeps the filter.
//   - The last of the two the person opened is remembered (localStorage, try/catch). A project's page opens in that view ONLY when its landing
//     pill is itself a task view (data-landing="board" | "list" on the page); a Summary landing page is never redirected.
(function () {
  "use strict";
  var KEYS = ["assignee", "priority", "stream", "category", "subproject", "status", "q"];
  var STORE = "crewboss-task-view";
  var m = /\/(list|board)$/.exec(location.pathname);

  function carry() {
    var q = new URLSearchParams(location.search), out = new URLSearchParams();
    KEYS.forEach(function (k) { if (q.get(k)) out.set(k, q.get(k)); });
    return out.toString();
  }
  function wirePills() {
    var qs = carry();
    Array.prototype.forEach.call(document.querySelectorAll("[data-record-pills] [data-pill=list], [data-record-pills] [data-pill=board]"), function (a) {
      var base = a.getAttribute("href").split("?")[0];
      a.setAttribute("href", base + (qs ? "?" + qs : ""));
    });
  }
  if (m) {
    try { localStorage.setItem(STORE, m[1]); } catch (e) { /* not remembered */ }
    wirePills();
    return;
  }
  var landing = document.querySelector("[data-landing]");
  var kind = landing && landing.getAttribute("data-landing");
  if ((kind === "list" || kind === "board") && !location.search) {
    var last = null;
    try { last = localStorage.getItem(STORE); } catch (e) { /* none */ }
    if ((last === "list" || last === "board") && last !== kind) {
      var pill = document.querySelector("[data-record-pills] [data-pill=" + last + "]");
      if (pill) location.replace(pill.getAttribute("href"));
    }
  }
})();
