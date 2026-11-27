// THE APP SWITCHER (#778, Alpine component `appSwitcher`; markup in partials/topbar.html). The section tile + name (owner 11-16: no grid icon) opens a menu of apps (Build, Assets, Administration for
// the owner). Esc or a click outside closes it (Esc returns focus to the button); Up/Down/Home/End move between entries, Tab closes it.
// Loaded before alpine.min.js (base.html), so window.appSwitcher exists when Alpine starts.
(function () {
  "use strict";
  function component() {
    return {
      open: false,
      rows: function () { return Array.prototype.slice.call(this.$refs.menu.querySelectorAll('[role="menuitem"]')); },
      toggle: function () { if (this.open) this.close(false); else this.show(false); },
      openFirst: function () { this.show(true); },
      show: function (focusFirst) {
        var self = this;
        this.open = true;
        this.$nextTick(function () {
          var rows = self.rows(), cur = self.$refs.menu.querySelector('[aria-current="true"]');
          var t = focusFirst ? (cur || rows[0]) : null;
          if (t) t.focus();
        });
      },
      close: function (refocus) {
        if (!this.open) return;
        this.open = false;
        if (refocus && this.$refs.btn) this.$refs.btn.focus();
      },
      onKey: function (e) {
        var rows = this.rows(), i = rows.indexOf(document.activeElement);
        if (e.key === "ArrowDown") { e.preventDefault(); rows[(i + 1) % rows.length].focus(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); rows[i <= 0 ? rows.length - 1 : i - 1].focus(); }
        else if (e.key === "Home") { e.preventDefault(); rows[0].focus(); }
        else if (e.key === "End") { e.preventDefault(); rows[rows.length - 1].focus(); }
        else if (e.key === "Tab") { this.close(false); }
      }
    };
  }
  window.appSwitcher = component;
})();
