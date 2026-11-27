// THE HEADER'S + CREATE DIALOG (#778; Alpine component `crewCreate`, markup partials/create_task_modal.html). The project list it is given is the
// server's: only projects the person may change. It posts to the existing create route (/<slug>/tasks/new) asking for JSON, so the route's own
// access checks apply; a refusal comes back as plain text and is shown in the dialog. The last few projects used are remembered in localStorage
// (try/catch: private mode just has no "Recent projects"). On a page with no project the picker starts empty.
// Loaded before alpine.min.js (base.html), so window.crewCreate exists when Alpine starts.
(function () {
  "use strict";
  var KEY = "crewboss-create-recent", KEEP = 5;

  function readRecent() { try { var v = JSON.parse(localStorage.getItem(KEY) || "[]"); return Array.isArray(v) ? v : []; } catch (e) { return []; } }
  function writeRecent(slug) {
    var list = [slug].concat(readRecent().filter(function (s) { return s !== slug; })).slice(0, KEEP);
    try { localStorage.setItem(KEY, JSON.stringify(list)); } catch (e) { /* not remembered */ }
  }

  window.crewCreate = function (projects, current) {
    return {
      projects: projects || [], current: current || "", open: false, pickerOpen: false, find: "", project: "", type: "feature", title: "", body: "",
      priority: "should", another: false, busy: false, error: "",
      show: function () {
        this.project = this.current; this.error = ""; this.pickerOpen = false; this.find = ""; this.open = true;
        var self = this;
        this.$nextTick(function () { if (self.project) self.$refs.title.focus(); else { self.pickerOpen = true; self.$nextTick(function () { self.$refs.find.focus(); }); } });
      },
      close: function () { this.open = false; this.pickerOpen = false; },
      projectName: function () { var p = this.byslug(this.project); return p ? p.name : ""; },
      byslug: function (s) { return this.projects.filter(function (p) { return p.slug === s; })[0]; },
      favourites: function () { return this.projects.filter(function (p) { return p.fav; }); },
      recents: function () { var self = this; return readRecent().map(function (s) { return self.byslug(s); }).filter(function (p) { return p && !p.fav; }); },
      filtered: function () {
        var q = this.find.trim().toLowerCase();
        return this.projects.filter(function (p) { return !q || (p.name + " " + p.short + " " + p.slug).toLowerCase().indexOf(q) >= 0; });
      },
      pick: function (slug) {
        if (!slug) return;
        this.project = slug; this.pickerOpen = false; this.find = "";
        var self = this; this.$nextTick(function () { self.$refs.title.focus(); });
      },
      submit: function () {
        var self = this;
        if (this.busy || !this.project || !this.title.trim()) return;
        this.busy = true; this.error = "";
        var fields = new URLSearchParams({ title: this.title.trim(), body: this.body, stream: this.type, priority: this.priority, assignee: "ai" });
        fetch("/" + this.project + "/tasks/new", { method: "POST", credentials: "same-origin",
                headers: { "HX-Request": "true", Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" }, body: fields })
          .then(function (r) { return r.text().then(function (t) { if (!r.ok) throw new Error(t || "That didn't work."); return JSON.parse(t); }); })
          .then(function (d) {
            writeRecent(self.project);
            if (window.mddToast) window.mddToast("Created #" + d.id + " · " + d.title, "info");
            if (document.body) document.body.dispatchEvent(new CustomEvent("mdd:board-stale", { bubbles: true }));
            self.title = ""; self.body = "";
            if (self.another) self.$nextTick(function () { self.$refs.title.focus(); }); else self.close();
          })
          .catch(function (e) { self.error = e.message; })
          .then(function () { self.busy = false; });
      }
    };
  };
})();
