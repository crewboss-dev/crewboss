// THE HEADER'S JUMP BOX (#771, Alpine component `crewJump`; the markup is partials/topbar.html). Type, see matches, one click or Enter opens
// the record. The server (/all/jump, app/search.py) decides what the person may see; this only asks, shows and moves.
//   - Up / Down move the highlight, Enter opens the highlighted result, Esc closes. Ctrl/Cmd+K or "/" focuses the box from anywhere.
//   - Typing asks after a short pause; a slow answer to an earlier keystroke never overwrites a newer one (a sequence number).
//   - Empty box: the left menu's own projects and accounts, as somewhere to start.
// Loaded before alpine.min.js (base.html), so window.crewJump exists when Alpine starts.
(function () {
  "use strict";
  var DELAY_MS = 120;

  // The next highlight for a key: wraps round; -1 when there is nothing to highlight.
  function move(sel, n, key) {
    if (!n) return -1;
    if (key === "ArrowDown") return sel < 0 ? 0 : (sel + 1) % n;
    if (key === "ArrowUp") return sel <= 0 ? n - 1 : sel - 1;
    return sel;
  }

  function component(section) {
    return {
      section: section || "build",
      open: false, q: "", items: [], sel: -1, loaded: false, _t: null, _seq: 0,
      wide: function () { return window.matchMedia("(min-width: 768px)").matches; },
      focusBox: function () {
        var self = this;
        this.$nextTick(function () { var r = self.wide() ? self.$refs.hdr : self.$refs.find; if (r) r.focus(); });
      },
      show: function (focus) {
        this.open = true;
        if (!this.loaded) this.ask();
        if (focus) this.focusBox();
      },
      close: function () {
        if (!this.open) return;
        this.open = false; this.sel = -1;
        if (this.$refs.hdr && document.activeElement === this.$refs.hdr) this.$refs.hdr.blur();
      },
      typed: function () {
        var self = this;
        this.open = true;
        clearTimeout(this._t);
        this._t = setTimeout(function () { self.ask(); }, DELAY_MS);
      },
      ask: function () {
        var self = this, seq = ++this._seq;
        fetch("/all/jump?q=" + encodeURIComponent(this.q) + (this.section !== "build" ? "&section=" + this.section : ""), { credentials: "same-origin", headers: { Accept: "application/json" } })
          .then(function (r) { return r.ok ? r.json() : { results: [] }; })
          .then(function (d) {
            if (seq !== self._seq) return;           // a newer keystroke is already asking
            self.items = d.results || []; self.sel = self.items.length ? 0 : -1; self.loaded = true;
          })
          .catch(function () { if (seq === self._seq) { self.items = []; self.sel = -1; self.loaded = true; } });
      },
      onKey: function (e) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault(); this.open = true;
          this.sel = move(this.sel, this.items.length, e.key);
          this.$nextTick(function () { var a = document.querySelector('[data-jump-result][aria-selected="true"]'); if (a && a.scrollIntoView) a.scrollIntoView({ block: "nearest" }); });
        } else if (e.key === "Enter") {
          var it = this.items[this.sel];
          if (it) { e.preventDefault(); window.location.href = it.url; }
        } else if (e.key === "Escape") {
          this.close();
        }
      },
      shortcut: function (e) {
        var inField = e.target.closest && e.target.closest("input, textarea, select, [contenteditable]");
        if (((e.key === "k" || e.key === "K") && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !inField)) {
          e.preventDefault(); this.show(true);
        }
      }
    };
  }
  window.crewJump = component;
  window.crewJumpMove = move;
})();
