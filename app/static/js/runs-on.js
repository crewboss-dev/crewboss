// "Runs on" pill (#810): opens the runner picker. A popover under the pill on a desktop, a bottom sheet on a phone (CSS decides which; the
// panel is the same element). Closes on the backdrop, outside click, Esc, picking an option (a plain form post) or a swipe down on the phone
// (chrome.js's dragSheet, which is loaded before this and only drags on touch). Without JS the panel stays hidden: the Machines page is the
// fallback, and the Run form carries the same choice.
(function () {
  "use strict";
  // Idempotent (#816): the pill can arrive by the page swap (event "crewboss:page"); the document listeners are bound once and use the current pill.
  var root = null, btn = null, panel = null, back = null;
  function setOpen(on) {
    if (!root) return;
    panel.hidden = !on; back.hidden = !on; btn.setAttribute("aria-expanded", on ? "true" : "false");
    if (on) { var first = panel.querySelector("button"); if (first && window.matchMedia("(min-width:768px)").matches) first.focus(); }
    else btn.focus({ preventScroll: true });
  }
  function init() {
    root = document.querySelector("[data-runs-on]");
    if (!root) { btn = panel = back = null; return; }
    btn = root.querySelector("[data-ro-open]"); panel = root.querySelector("[data-ro-panel]"); back = root.querySelector("[data-ro-backdrop]");
    if (root._roBound) return;
    root._roBound = true;
    btn.addEventListener("click", function () { setOpen(panel.hidden); });
    back.addEventListener("click", function () { setOpen(false); });
    if (window.dragSheet) window.dragSheet(panel, { backdrop: back, onDismiss: function () { setOpen(false); } });
  }
  document.addEventListener("click", function (e) { if (root && !panel.hidden && !root.contains(e.target)) setOpen(false); });
  document.addEventListener("keydown", function (e) { if (root && e.key === "Escape" && !panel.hidden) setOpen(false); });
  document.addEventListener("crewboss:page", init);
  init();
})();
