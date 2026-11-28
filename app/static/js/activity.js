// The live runner conversation (#89): a READ-ONLY view of what a job's agent says and does, like watching a Claude Code session.
// There is no input box and nothing here sends text to the agent. Everything that arrives is UNTRUSTED (the agent read a repository and a
// task text): it is only ever put on the page with textContent / createElement, never innerHTML, and links are http(s) only.
//
// Mount it on any element:  <div data-live-activity data-activity-url="/<slug>/tasks/<id>/run/activity" data-activity-job="<job id, optional>">
// (partials/job_activity.html renders exactly that). The #987 job card's slot ([data-job-activity] with the same two data attributes and a
// [data-activity-body] inside) is mounted too. Mounting is automatic on load and after every htmx swap; a mount is dropped when its element leaves
// the page. A <details> ancestor that is closed delays the first fetch until it is opened.
//   history:  GET <url>?after=<seq>[&job=<id>]  -> {job_id, state, live, frames[], last_seq, session_url}
//   live:     EventSource <url>/stream?after=<seq>[&job=<id>]  events: frames {frames,last_seq,state} | end | reset
// Frame: {seq, at, speaker: claude|gemini|runner, kind: assistant_text|tool_call|tool_result|permission_request|status|note, text, meta}.
(function () {
  "use strict";
  var LIVE_STATES = { queued: 1, running: 1 };
  var MAX_ROWS = 1500;          // rows kept in the page: the oldest fold away (the server keeps them all)
  var POLL_MS = 3000;           // fallback when there is no EventSource
  var mounts = new WeakMap();

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // ---- safe Markdown: paragraphs, **bold**, *italic*, `code`, fenced code, lists, headings (as bold lines), http(s) links ----
  var INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[[^\]\n]{1,200}\]\((?:[^()\s]|\([^()\s]*\)){1,500}\))|(https?:\/\/[^\s<>"')\]]{1,500})/;
  function safeHref(u) {
    try {
      var x = new URL(u);
      return (x.protocol === "https:" || x.protocol === "http:") ? x.href : "";
    } catch (e) { return ""; }
  }
  function link(parent, label, url) {
    var href = safeHref(url);
    if (!href) { parent.appendChild(document.createTextNode(label)); return; }
    var a = el("a", "jact-link", label);
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer nofollow";
    parent.appendChild(a);
  }
  function inline(parent, text, depth) {
    depth = depth || 0;
    while (text) {
      var m = INLINE.exec(text);
      if (!m) { parent.appendChild(document.createTextNode(text)); return; }
      if (m.index) parent.appendChild(document.createTextNode(text.slice(0, m.index)));
      var tok = m[0];
      if (m[1]) parent.appendChild(el("code", "jact-code", tok.slice(1, -1)));
      else if (m[2] && depth < 2) { var b = el("strong"); inline(b, tok.slice(2, -2), depth + 1); parent.appendChild(b); }
      else if (m[3] && depth < 2) { var i = el("em"); inline(i, tok.slice(1, -1), depth + 1); parent.appendChild(i); }
      else if (m[4]) { var close = tok.indexOf("]("); link(parent, tok.slice(1, close), tok.slice(close + 2, -1)); }
      else if (m[5]) link(parent, tok, tok);
      else parent.appendChild(document.createTextNode(tok));
      text = text.slice(m.index + tok.length);
    }
  }
  function markdown(text) {
    var root = el("div", "jact-md");
    var lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
    var para = [], list = null, listTag = "";
    function flushPara() { if (para.length) { var p = el("p"); inline(p, para.join(" ")); root.appendChild(p); para = []; } }
    function flushList() { list = null; listTag = ""; }
    for (var n = 0; n < lines.length; n++) {
      var line = lines[n], fence = /^\s*```/.exec(line);
      if (fence) {
        flushPara(); flushList();
        var code = [];
        for (n++; n < lines.length && !/^\s*```/.test(lines[n]); n++) code.push(lines[n]);
        var pre = el("pre", "jact-pre"); pre.appendChild(el("code", null, code.join("\n"))); root.appendChild(pre);
        continue;
      }
      var li = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line), h = /^\s{0,3}#{1,6}\s+(.*)$/.exec(line);
      if (li) {
        flushPara();
        var tag = li[1] ? "ul" : "ol";
        if (!list || listTag !== tag) { list = el(tag, "jact-list"); listTag = tag; root.appendChild(list); }
        var item = el("li"); inline(item, li[3]); list.appendChild(item);
      } else if (h) {
        flushPara(); flushList();
        var hp = el("p", "jact-h"); inline(hp, h[1]); root.appendChild(hp);
      } else if (!line.trim()) { flushPara(); flushList(); }
      else { flushList(); para.push(line.trim()); }
    }
    flushPara();
    return root;
  }

  // ---- one mounted view ----
  function Mount(host) {
    var self = this;
    this.host = host;
    this.slot = host.matches("[data-job-activity]") ? host.querySelector("[data-activity-body]") : host;
    this.url = host.getAttribute("data-activity-url") || "";
    this.job = host.getAttribute("data-activity-job") || "";
    this.last = 0; this.rows = 0; this.live = false; this.state = ""; this.working = false; this.session = "";
    this.es = null; this.timer = null; this.started = false; this.stuck = true; this.lastCall = null; this.dead = false;
    this.build();
    var d = host.closest("details");
    this.details = d;
    this.userClosed = false;
    // A collapsible slot (the job card's Activity) is read at once and opened by itself while the job is live, so a person sees the work
    // without hunting for it; if they close it themselves it stays closed.
    this.onToggle = function () { if (!d.open) self.userClosed = true; };
    if (d) d.addEventListener("toggle", this.onToggle);
    this.maybeStart();
  }
  Mount.prototype.q = function (extra) {
    var p = ["after=" + this.last]; if (this.job) p.push("job=" + encodeURIComponent(this.job));
    return "?" + p.join("&") + (extra || "");
  };
  Mount.prototype.build = function () {
    var self = this, s = this.slot;
    while (s.firstChild) s.removeChild(s.firstChild);
    var root = el("section", "jact");
    root.setAttribute("aria-label", "Runner conversation, watch only");
    var head = el("div", "jact-head");
    head.appendChild(el("span", "jact-watch", "Watch only"));
    this.statusEl = el("span", "jact-status", "");
    head.appendChild(this.statusEl);
    var tools = el("span", "jact-tools");
    this.openEl = el("a", "jact-btn", "Open in claude.ai/code");
    this.openEl.target = "_blank"; this.openEl.rel = "noopener noreferrer"; this.openEl.hidden = true;
    this.stopEl = el("button", "jact-btn", "Stop"); this.stopEl.type = "button"; this.stopEl.hidden = true;
    this.stopEl.addEventListener("click", function () {
      var b = document.querySelector("[data-run-cancel]");   // the job card's own Stop (cancel is that button's form; nothing new is sent from here)
      if (b) b.click();
    });
    tools.appendChild(this.openEl); tools.appendChild(this.stopEl); head.appendChild(tools);
    this.log = el("div", "jact-log");
    this.log.setAttribute("role", "log"); this.log.setAttribute("aria-live", "polite"); this.log.setAttribute("aria-relevant", "additions");
    this.log.tabIndex = 0;
    this.empty = el("p", "jact-empty", "Nothing to show yet.");
    this.log.appendChild(this.empty);
    this.typing = el("div", "jact-typing"); this.typing.hidden = true;
    this.typing.setAttribute("role", "status");
    var dots = el("span", "jact-dots");                                  // the dots are drawn by CSS
    for (var i = 0; i < 3; i++) dots.appendChild(el("i"));
    this.typing.appendChild(dots);
    this.typing.appendChild(document.createTextNode(" working\u2026"));
    this.more = el("button", "jact-more", "New messages ↓"); this.more.type = "button"; this.more.hidden = true;
    this.more.addEventListener("click", function () { self.scrollEnd(true); });
    this.log.addEventListener("scroll", function () {
      self.stuck = self.log.scrollHeight - self.log.scrollTop - self.log.clientHeight < 48;
      if (self.stuck) self.more.hidden = true;
    });
    root.appendChild(head); root.appendChild(this.log); root.appendChild(this.typing); root.appendChild(this.more);
    s.appendChild(root);
  };
  Mount.prototype.maybeStart = function () {
    if (this.started || this.dead) return;
    this.started = true;
    this.load();
  };
  Mount.prototype.load = function () {
    var self = this;
    if (!this.url) return;
    fetch(this.url + this.q(), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(function (d) {
        if (self.dead) return;
        if (d.job_id && self.job && String(d.job_id) !== self.job) return;
        self.setState(d.state, d.live);
        if (d.live && self.details && !self.details.open && !self.userClosed) self.details.open = true;
        if (d.session_url) self.setSession(d.session_url);
        self.add(d.frames || []);
        if (self.live) self.connect(); else self.render();
      })
      .catch(function () { if (!self.dead) { self.statusEl.textContent = "Could not load the live view"; self.timer = setTimeout(function () { self.load(); }, POLL_MS * 2); } });
  };
  Mount.prototype.connect = function () {
    var self = this;
    this.close();
    if (typeof window.EventSource !== "function") { this.poll(); return; }
    var es = new EventSource(this.url + "/stream" + this.q());
    this.es = es;
    es.addEventListener("frames", function (e) {
      try { var d = JSON.parse(e.data); self.setState(d.state, !!LIVE_STATES[d.state]); self.add(d.frames || []); } catch (x) { /* a bad event is skipped */ }
    });
    es.addEventListener("end", function (e) { self.setState(e.data, false); self.close(); });
    es.addEventListener("reset", function () { self.close(); self.reset(); });
    es.addEventListener("error", function () {
      if (es.readyState === 2 /* CLOSED */) { self.close(); if (!self.dead) self.timer = setTimeout(function () { self.load(); }, POLL_MS); }
    });
  };
  Mount.prototype.poll = function () {
    var self = this;
    this.timer = setTimeout(function () {
      if (self.dead) return;
      fetch(self.url + self.q(), { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
        self.setState(d.state, d.live); self.add(d.frames || []);
        if (self.live) self.poll();
      }).catch(function () { self.poll(); });
    }, POLL_MS);
  };
  Mount.prototype.close = function () {
    if (this.es) { try { this.es.close(); } catch (e) { /* closed */ } this.es = null; }
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
  };
  Mount.prototype.destroy = function () {
    this.dead = true; this.close();
    if (this.details) this.details.removeEventListener("toggle", this.onToggle);
  };
  Mount.prototype.reset = function () {
    this.last = 0; this.rows = 0; this.working = false; this.lastCall = null;
    while (this.log.firstChild) this.log.removeChild(this.log.firstChild);
    this.log.appendChild(this.empty);
    this.load();
  };
  Mount.prototype.setSession = function (url) {
    var href = safeHref(url);
    if (href && /^https:\/\/claude\.ai\/code\//.test(href)) { this.session = href; this.openEl.href = href; }
    this.render();
  };
  Mount.prototype.setState = function (state, live) {
    this.state = state || this.state; this.live = !!live;
    this.render();
  };
  Mount.prototype.render = function () {
    var label = this.live ? (this.state === "queued" ? "Waiting for a runner" : "Live") : (this.state ? this.state.charAt(0).toUpperCase() + this.state.slice(1) : "");
    this.statusEl.textContent = label;
    this.statusEl.setAttribute("data-live", this.live ? "1" : "0");
    this.stopEl.hidden = !(this.live && document.querySelector("[data-run-cancel]"));
    this.openEl.hidden = !(this.live && this.session);
    this.typing.hidden = !(this.live && this.working);
  };
  Mount.prototype.scrollEnd = function (force) {
    if (force || this.stuck) { this.log.scrollTop = this.log.scrollHeight; this.more.hidden = true; this.stuck = true; }
    else this.more.hidden = false;
  };
  Mount.prototype.add = function (frames) {
    var added = 0;
    for (var i = 0; i < frames.length; i++) {
      var f = frames[i];
      if (!f || typeof f.seq !== "number" || f.seq <= this.last) continue;   // a reconnect replays: each seq once
      this.last = f.seq;
      if (this.row(f)) added++;
    }
    if (this.empty.parentNode && this.rows) this.log.removeChild(this.empty);
    while (this.rows > MAX_ROWS && this.log.firstChild) { this.log.removeChild(this.log.firstChild); this.rows--; }
    this.render();
    if (added) this.scrollEnd(false);
  };
  Mount.prototype.row = function (f) {
    var meta = f.meta || {}, text = typeof f.text === "string" ? f.text : "", node = null, kind = f.kind, who = f.speaker;
    if (kind === "status") {
      if (meta.session_url) this.setSession(meta.session_url);
      var st = meta.state || "";
      if (st === "working" || st === "thinking") this.working = true;
      else if (st === "idle" || st === "done") this.working = false;
      if (st === "unavailable" || (!st && text)) node = el("p", "jact-note", text);
    } else if (kind === "assistant_text") {
      this.working = true;
      node = el("div", "jact-msg " + (who === "gemini" ? "gemini" : "claude"));
      node.appendChild(el("span", "jact-who", who === "gemini" ? "Gemini" : "Claude"));
      node.appendChild(markdown(text));
    } else if (kind === "tool_call" && who === "gemini") {         // what Claude sent Gemini
      node = el("div", "jact-msg to-gemini");
      node.appendChild(el("span", "jact-who", "Claude → Gemini"));
      node.appendChild(markdown(text));
    } else if (kind === "tool_call") {
      this.working = true;
      node = el("details", "jact-tool");
      var sum = el("summary", null), tool = meta.tool || "Tool", rest = text;
      if (text.indexOf(tool + " ") === 0) rest = text.slice(tool.length + 1);
      else if (text === tool) rest = "";
      sum.appendChild(el("span", "jact-tool-name", tool));
      sum.appendChild(el("span", "jact-tool-text", rest));
      node.appendChild(sum);
      this.lastCall = node;
      if (text.length > 60) { var full = el("pre", "jact-pre"); full.textContent = text; node.appendChild(full); }
    } else if (kind === "tool_result") {
      if (meta.restricted) {                                   // a viewer sees that a result came back, not what was in it
        node = el("p", "jact-note jact-result-hidden", "Result hidden (output is for people who can edit this project)");
      } else {
        node = el("details", "jact-result" + (meta.is_error ? " err" : ""));
        var rs = el("summary", null, (meta.is_error ? "Error" : "Result") + (meta.status && meta.status !== "ok" && meta.status !== "error" ? " · " + meta.status : ""));
        node.appendChild(rs);
        var body = el("pre", "jact-pre"); body.textContent = text || "(no output)"; node.appendChild(body);
      }
    } else if (kind === "permission_request") {
      this.working = false;
      node = el("p", "jact-perm", text || ("Waiting for permission" + (meta.tool ? ": " + meta.tool : "")));
      node.setAttribute("role", "status");
    } else if (kind === "note") node = el("p", "jact-note", text);
    if (!node) return false;
    this.log.appendChild(node); this.rows++;
    return true;
  };

  // ---- mounting ----
  function mountAll(root) {
    var hosts = (root || document).querySelectorAll("[data-live-activity], [data-job-activity]");
    for (var i = 0; i < hosts.length; i++) {
      var h = hosts[i];
      if (!mounts.has(h) && h.getAttribute("data-activity-url")) mounts.set(h, new Mount(h));
    }
  }
  function sweep() {
    mountAll(document);
    // drop the mounts whose element is no longer in the page (a swapped card): close their streams
    for (var i = 0; i < tracked.length; i++) if (!document.contains(tracked[i].host)) { tracked[i].destroy(); tracked.splice(i--, 1); }
  }
  var tracked = [];
  var origSet = mounts.set.bind(mounts);
  mounts.set = function (k, v) { tracked.push(v); return origSet(k, v); };

  window.CrewbossActivity = { mount: function (host) { if (!mounts.has(host)) mounts.set(host, new Mount(host)); return mounts.get(host); }, markdown: markdown, sweep: sweep };
  document.addEventListener("DOMContentLoaded", sweep);
  if (document.readyState !== "loading") sweep();
  document.addEventListener("htmx:afterSettle", sweep);
  document.addEventListener("htmx:afterSwap", sweep);
  window.addEventListener("pagehide", function () { for (var i = 0; i < tracked.length; i++) tracked[i].destroy(); tracked = []; });
})();
