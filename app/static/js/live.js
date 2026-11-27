// Live updates (task #177): server→browser push so an open tab refreshes itself when ANY session
// (Claude / Gemini / the owner / another tab / the phone) advances the board — no manual reload.
//
// The server streams Server-Sent Events from /all/events (app/routers/pages.py): a `changed` frame
// carrying the latest activity id whenever the board moves, plus `: ping` heartbeats. On a `changed`
// we dispatch `mdd:board-stale` on <body>; every panel (board_status, focus, global_plan, tasklist,
// triage_banner) is already wired to `hx-trigger="mdd:board-stale from:body"` and refetches its own
// scoped data. We only fire that one event — the panels do the rest.
//
// Degrades gracefully: no EventSource (or any error) => the page behaves exactly as before (own-tab
// changes still refresh via htmx; cross-session changes wait for the next manual refresh). `defer`red
// like app.js so it runs after htmx is present.

(function () {
  "use strict";

  if (typeof window.EventSource !== "function") return;  // old browser: no live updates, no error

  var source = null;
  var lastId = -1;          // de-dupe: ignore a `changed` we've already acted on (reconnect replays)
  var debounceTimer = null; // collapse a burst of changes into one refresh

  function fireStale() {
    debounceTimer = null;
    try {
      document.body.dispatchEvent(new CustomEvent("mdd:board-stale", { bubbles: true }));
    } catch (e) { /* a detached body during teardown — nothing to refresh */ }
  }

  function onChanged(evt) {
    var id = parseInt(evt && evt.data, 10);
    if (!isNaN(id)) {
      if (id <= lastId) return;  // already handled (e.g. the frame a reconnect re-sends)
      lastId = id;
    }
    if (debounceTimer) return;   // a refresh is already pending within the window
    debounceTimer = setTimeout(fireStale, 1000);  // at most one board refresh per ~1s
  }

  function connect() {
    if (source) return;  // guard: never open a second connection to the same stream
    try {
      source = new EventSource("/all/events");
    } catch (e) {
      source = null;
      return;
    }
    source.addEventListener("changed", onChanged);
    // EventSource reconnects on its own after a drop (honouring the server's `retry:`); on a hard
    // error it may close — tear down so a later visibility/pageshow can reopen cleanly.
    source.addEventListener("error", function () {
      if (source && source.readyState === EventSource.CLOSED) disconnect();
    });
  }

  function disconnect() {
    if (!source) return;
    try { source.close(); } catch (e) { /* already closed */ }
    source = null;
  }

  // Don't hold a stream open for a backgrounded/hidden tab; reopen when it comes back (and refresh
  // once, since we may have missed changes while away).
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      disconnect();
    } else {
      connect();
      if (debounceTimer) return;
      debounceTimer = setTimeout(fireStale, 1000);
    }
  });
  window.addEventListener("pagehide", disconnect);

  if (document.visibilityState !== "hidden") connect();
})();
