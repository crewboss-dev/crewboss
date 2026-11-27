// CA, the Crewboss Assistant drawer (task #702; #727 added the empty state, uploads, the microphone, the linked-item chip and the
// history hooks). The server does the thinking, on the person's own runner; this file shows the chat, streams the reply over SSE
// (/ca/stream) and renders question / confirm cards as tappable cards.
// EVERYTHING that came from the model or from a task is written with textContent: never as HTML.
(function () {
  "use strict";
  var html = document.documentElement;
  var drawer = document.getElementById("ca-drawer");
  if (!drawer) return;

  var $ = function (id) { return document.getElementById(id); };
  var msgs = $("ca-msgs"), scroll = $("ca-scroll"), input = $("ca-input"), form = $("ca-form");
  var sendBtn = $("ca-send"), enterBtn = $("ca-enter"), stopBtn = $("ca-stop"), statusBox = $("ca-status");
  var toolLine = $("ca-tool"), titleEl = $("ca-title"), boxEl = document.querySelector("#ca-form .ca-box");
  var hero = $("ca-hero"), slot = $("ca-compose-slot"), dock = $("ca-dock"), greeting = $("ca-greeting");
  var recentBox = $("ca-recent"), recentList = $("ca-recent-list"), chipBox = $("ca-entity"), filesBox = $("ca-files");
  var fileInput = $("ca-file"), attachBtn = $("ca-attach"), micBtn = $("ca-mic"), micGroup = $("ca-mic-group");
  var micMenuBtn = $("ca-mic-menu-btn"), devMenu = $("ca-devmenu"), devList = $("ca-devlist"), meterBar = $("ca-meter-bar");

  var disc = $("ca-disc"), discList = $("ca-disc-list"), discTitle = $("ca-disc-title"), discNote = $("ca-disc-note"), discAll = $("ca-disc-all");
  var convShared = false, convCanPost = true, convShareable = false, myId = null;   // the open conversation is a task's shared discussion; may I post in it; who am I
  var discData = null, discOpen = false, taskKey = "", bootP = null;
  var convId = null, busy = false, liveEl = null, unread = 0, es = null, ready = false, signedOut = false;
  var convEntity = null;          // what the OPEN conversation is linked to ({kind, id, title, label}) or null
  var forced = null;              // "New discussion" on an item: the next new chat is linked to it
  var dismissedKey = null;        // the item whose chip the person removed (for a new chat)
  var selected = null;            // the last card clicked that carries data-ca-entity
  var pending = [];               // uploads attached to the composer, not yet sent: {id, name, kind, size}
  var uploading = 0, firstName = "", recent = [], speechOn = false, mic = null, vad = null, meterRaf = 0;
  var KEY = "crewboss-ca-open", CONV_KEY = "crewboss-ca-conv", MIC_KEY = "crewboss-ca-mic";

  function store(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

  // ---- what a NEW conversation would be bound to: this page, and the item in front of the person ------------------------
  // Pages declare the item with ONE hook, data-ca-entity="kind:id" (+ data-ca-title): the open task card, an expanded knowledge entry, a
  // change, a workflow page. The innermost one in the open card wins, then the last card the person clicked, then the page's own.
  // The server checks it again (what this person may see) and drops anything else: this is only what the chip offers.
  function entityFrom(el) {
    var v = el && el.getAttribute("data-ca-entity");
    var i = v ? v.indexOf(":") : -1;
    if (i < 1) return null;
    return { kind: v.slice(0, i), id: v.slice(i + 1), title: (el.getAttribute("data-ca-title") || "").trim() };
  }
  function drawerOpen() {
    try { var st = window.Alpine && window.Alpine.store("drawer"); return st ? !!st.open : true; } catch (e) { return true; }
  }
  function detectEntity() {
    var inCard = document.querySelectorAll("#drawer-content [data-ca-entity]");
    if (inCard.length && drawerOpen()) { var e1 = entityFrom(inCard[inCard.length - 1]); if (e1) return e1; }
    // Owner 11-18: Now, Dashboard and Recent (the menu items above the divider) and the other global pages (Assets, Administration,
    // /all/...) are never locked to a project or a task. They carry the last project in data-base only for their shared partials, and
    // a card clicked there is something passed through, not where the person is: a chat started there is unbound (the whole estate).
    // Only a task open in the task window binds (above). On a project's own pages: the clicked card, the record page, the project.
    var base = (document.body.getAttribute("data-base") || "").replace(/^\//, "");
    var path = location.pathname;
    var onProject = !!base && base.indexOf("/") < 0 && (path === "/" + base || path.indexOf("/" + base + "/") === 0);
    if (!onProject) return null;
    if (selected && document.contains(selected)) { var e2 = entityFrom(selected); if (e2) return e2; }
    var pg = document.querySelector("[data-ca-page][data-ca-entity]");
    if (pg) { var e3 = entityFrom(pg); if (e3) return e3; }
    return { kind: "project", id: base, title: base };
  }
  function entKey(e) { return e ? e.kind + ":" + e.id : ""; }
  // THE TASK IN VIEW (#830): the task open in the task window, or the task's own full page. While one is, the assistant's right-hand column is
  // that task's DISCUSSION (its conversations, shared by the people on the task) and the assistant button opens it; anywhere else the button
  // opens the global assistant as before.
  function taskNow() {
    var inCard = document.querySelectorAll("#drawer-content [data-ca-entity^='task:']");
    var el = inCard.length && drawerOpen() ? inCard[inCard.length - 1] : document.querySelector("[data-ca-page][data-ca-entity^='task:']");
    var e = el ? entityFrom(el) : null;
    return e && e.kind === "task" && /^\d+$/.test(e.id) ? e : null;
  }
  function offered() {            // the item a NEW chat would be linked to (null once the person removed the chip)
    var e = forced || detectEntity();
    return e && entKey(e) !== dismissedKey ? e : null;
  }
  function pageContext() {
    var base = (document.body.getAttribute("data-base") || "").replace(/^\//, "");
    var e = offered();
    return { path: location.pathname, project: base && base.indexOf("/") < 0 ? base : null, entity: e ? { kind: e.kind, id: e.id } : null };
  }
  // ---- the linked-item chip (above the composer) -------------------------------------------------------------------------
  var KIND_LABEL = { project: "Project", task: "Task", change: "Change", knowledge: "Knowledge", workflow: "Workflow" };
  function labelOf(e) {
    if (e.label) return e.label;
    var k = KIND_LABEL[e.kind] || e.kind;
    return (e.kind === "task" || e.kind === "change") ? k + " #" + e.id : k;
  }
  function chip(e, onRemove) {
    var c = document.createElement("span"); c.className = "ca-chip";
    var t = document.createElement("span"); t.className = "t";
    var lab = labelOf(e), s = lab;                    // "Project Acme Site", never "Project Acme Site Acme Site"
    if (e.title && lab.indexOf(e.title) < 0) s += e.kind === "project" ? " " + e.title : " · " + e.title;
    t.textContent = s; t.title = s; c.appendChild(t);
    var x = document.createElement("button"); x.type = "button"; x.className = "x"; x.setAttribute("aria-label", "Remove link to " + labelOf(e));
    x.textContent = "×"; x.addEventListener("click", onRemove); c.appendChild(x);
    return c;
  }
  function renderChip() {
    chipBox.textContent = "";
    var seen = offered();
    if (convId) {
      if (convEntity) chipBox.appendChild(chip(convEntity, unlinkNow));
      if (seen && entKey(seen) !== entKey(convEntity) && seen.kind !== "project") {     // moved to another item: offer, never silently re-bind
        var b = document.createElement("button"); b.type = "button"; b.className = "ca-linkpill";
        b.textContent = "Link to " + labelOf(seen) + (seen.title ? " · " + seen.title : "");
        b.addEventListener("click", function () { linkNow(seen); });
        chipBox.appendChild(b);
      }
    } else if (seen) {
      chipBox.appendChild(chip(seen, function () { dismissedKey = entKey(seen); forced = null; renderChip(); }));
    }
    chipBox.hidden = !chipBox.firstChild;
  }
  function unlinkNow() {
    if (!convId) return;
    api("PATCH", "/ca/conversations/" + convId, { entity: null }).then(function (r) {
      if (r.ok) { convEntity = null; dismissedKey = entKey(detectEntity()); renderChip(); loadState(); } else say((r.json && r.json.detail) || "That didn't work.");
    });
  }
  function linkNow(e) {
    api("PATCH", "/ca/conversations/" + convId, { entity: { kind: e.kind, id: e.id } }).then(function (r) {
      if (r.ok) { convEntity = (r.json.context || {}).entity || null; renderChip(); loadState(); } else say((r.json && r.json.detail) || "I can't link that.");
    });
  }
  var chipTimer = 0;
  function chipSoon() { clearTimeout(chipTimer); chipTimer = setTimeout(function () { if (html.classList.contains("ca-open")) renderChip(); viewChanged(); }, 150); }
  // The task in view changed (a task window opened or closed, or a task page loaded). The Discussion never opens by itself (#954, owner q751:
  // "never by itself"): it opens only from the assistant button or the task window's Discussion tab. A drawer the person opened stays open and
  // follows the new task; here only its list reloads for the new task and the tab's count is repainted.
  function viewChanged() {
    var t = taskNow(), key = t ? t.id : "";
    paintTabs();
    if (key === taskKey) return;
    taskKey = key; discData = null; discOpen = false;
    paintDisc();                                   // clears the previous task's count at once
    if (t) loadDisc();
  }
  document.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!t || !t.closest || t.closest("#ca-drawer")) return;
    var card = t.closest("[data-ca-entity]");
    if (card && !card.closest("#drawer-content")) selected = card;
    chipSoon();
  }, true);
  document.addEventListener("htmx:afterSettle", chipSoon);
  if (window.MutationObserver) {
    var dc = document.getElementById("drawer-content");
    if (dc) new MutationObserver(chipSoon).observe(dc, { childList: true, subtree: true });
    new MutationObserver(chipSoon).observe(document.body, { attributes: true, subtree: true, attributeFilter: ["data-ca-entity"] });
  }

  // ---- api ---------------------------------------------------------------------------------------------------------
  function api(method, url, body, raw) {
    var opt = { method: method, headers: { "Accept": "application/json" }, credentials: "same-origin" };
    if (raw) { opt.headers["Content-Type"] = "application/octet-stream"; opt.body = body; }
    else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      return r.text().then(function (t) {
        var j = null; try { j = t ? JSON.parse(t) : null; } catch (e) {}
        return { ok: r.ok, status: r.status, json: j };
      });
    });
  }
  function say(text, code) {            // a plain banner under the header; `code` is an optional command in monospace
    statusBox.textContent = "";
    if (!text) { statusBox.hidden = true; return; }
    var parts = String(text).split("`");
    parts.forEach(function (p, i) {
      if (i % 2) { var c = document.createElement("code"); c.textContent = p; statusBox.appendChild(c); }
      else statusBox.appendChild(document.createTextNode(p));
    });
    statusBox.hidden = false;
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------
  function toBottom() { scroll.scrollTop = scroll.scrollHeight; }
  function attEl(a) {              // a sent file: a thumbnail for an image, a chip for anything else (names via textContent)
    if (a.removed) {               // stripped by the sweep: the transcript keeps a stub
      var st = document.createElement("span"); st.className = "ca-att removed";
      st.textContent = a.filename + " · " + (a.removed_reason || "removed"); return st;
    }
    var url = "/ca/uploads/" + a.id;
    if (a.kind === "image") {
      var l = document.createElement("a"); l.href = url; l.target = "_blank"; l.rel = "noopener"; l.className = "ca-att img"; l.title = a.filename;
      var im = document.createElement("img"); im.src = url; im.alt = a.filename; im.loading = "lazy"; l.appendChild(im);
      return l;
    }
    var f = document.createElement("a"); f.href = url; f.target = "_blank"; f.rel = "noopener"; f.className = "ca-att file"; f.textContent = a.filename;
    return f;
  }
  // ---- links in a message: record links open the record's FULL PAGE in the main area (js/pagenav.js), the CA drawer untouched ----------------
  var RECORD = /^\/(?:all\/tasks\/\d+(?:\/page)?|[a-z0-9][a-z0-9-]*\/(?:tasks|knowledge|changes)\/\d+)\/?$/;
  var LINK_RE = /https?:\/\/[^\s<>"]+|(?<![\w\/])\/(?:all\/tasks\/\d+(?:\/page)?|[a-z0-9][a-z0-9-]*\/(?:tasks|knowledge|changes)\/\d+|[a-z0-9][a-z0-9-]*\/board\?open=\d+)|\btask #\d+/gi;
  function recordHref(h) {            // a same-site record address as a path, or null
    try {
      var u = new URL(h, location.origin);
      if (u.origin !== location.origin) return null;
      var m = /^\/([a-z0-9][a-z0-9-]*)\/board$/.exec(u.pathname), o = /^\d+$/.test(u.searchParams.get("open") || "");
      var path = m && o ? "/" + m[1] + "/tasks/" + u.searchParams.get("open") : u.pathname;
      return RECORD.test(path) ? path : null;
    } catch (e) { return null; }
  }
  function renderText(el, text) {     // text -> nodes: plain text via createTextNode, anchors only for recognised addresses
    el._raw = text; el.textContent = "";
    var last = 0, m;
    LINK_RE.lastIndex = 0;
    while ((m = LINK_RE.exec(text))) {
      var raw = m[0], trail = (/[.,;:!?)\]]+$/.exec(raw) || [""])[0];
      if (trail) raw = raw.slice(0, raw.length - trail.length);
      if (!raw) continue;
      el.appendChild(document.createTextNode(text.slice(last, m.index)));
      var a = document.createElement("a"), tm = /^task #(\d+)$/i.exec(raw), path = tm ? "/all/tasks/" + tm[1] + "/page" : recordHref(raw);
      if (path) { a.href = path; a.setAttribute("data-ca-link", "record"); }
      else if (/^https?:/i.test(raw)) { a.href = raw; a.target = "_blank"; a.rel = "noopener noreferrer"; }
      else { a = document.createTextNode(raw); }
      if (a.nodeType === 1) a.textContent = raw;
      el.appendChild(a);
      last = m.index + raw.length; LINK_RE.lastIndex = last;
    }
    el.appendChild(document.createTextNode(text.slice(last)));
  }
  msgs.addEventListener("click", function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest("a[data-ca-link]") : null;
    if (!a || ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    if (!window.crewbossNav) return;                       // no swap code: the link just navigates
    ev.preventDefault();
    window.crewbossNav(a.getAttribute("href"), { fromCA: true });
  });
  function addMsg(role, text, id, atts, author) {
    if (id && msgs.querySelector('[data-mid="' + id + '"]')) return null;
    var el = document.createElement("div");
    el.className = "ca-msg " + (role === "user" ? "user" : role === "system" ? "system" : "assistant");
    if (role === "system" && text && /^Looked at the code/.test(text)) {      // a look's answer: a note you can open, so the chat stays short
      var det = document.createElement("details"), sm = document.createElement("summary");
      sm.textContent = "Looked at the code"; det.appendChild(sm);
      var bd = document.createElement("div"); bd.className = "ca-lookbody"; bd.textContent = text; det.appendChild(bd);
      el.appendChild(det); text = "";
    }
    if (role === "user" && author && convShared) {                              // a task's discussion: who wrote it
      var by = document.createElement("div"); by.className = "ca-by"; by.textContent = author; el.appendChild(by);
      var tx = document.createElement("div"); tx.textContent = text; el.appendChild(tx); text = "";
    }
    if (text) { if (role === "user") el.textContent = text; else renderText(el, text); }
    if (atts && atts.length) {
      var row = document.createElement("div"); row.className = "ca-atts";
      atts.forEach(function (a) { row.appendChild(attEl(a)); });
      el.appendChild(row);
    }
    if (id) el.setAttribute("data-mid", id);
    msgs.appendChild(el);
    layout();
    toBottom();
    return el;
  }
  // The PA's rule (pa.js toggleSend): no send button while the box is empty; it appears once there is text, a file or a dictation going.
  function paintSend() {
    var has = !!input.value.trim() || pending.some(function (f) { return f && f.id; }) || !!(mic && mic.recording && mic.recording());
    sendBtn.hidden = busy || !has;
    enterBtn.classList.toggle("on", has);
    anchor(has && drawer.classList.contains("is-empty"));
  }
  // The big composer grows DOWNWARDS (pa.js anchor): once there is content the empty-state block keeps the offset it had when centred.
  var anchored = false;
  function anchor(on) {
    if (on === anchored) return;
    anchored = on;
    if (on) {
      var top = hero.getBoundingClientRect().top - scroll.getBoundingClientRect().top + scroll.scrollTop;
      hero.style.margin = Math.max(0, Math.round(top)) + "px 0 0";
    } else hero.style.margin = "";
  }
  function setBusy(on) {
    busy = on;
    stopBtn.hidden = !on;
    if (boxEl) boxEl.classList.toggle("ca-stopping", on);
    enterBtn.disabled = on;
    paintSend();
    input.disabled = false;
    if (!on) { toolLine.hidden = true; liveEl = null; }
  }
  function cardEl(card) {
    var el = document.createElement("div");
    el.className = "ca-card " + (card.state === "pending" ? "pending" : "");
    el.setAttribute("data-card", card.id);
    paintCard(el, card);
    return el;
  }
  function line(parent, cls, text) { var d = document.createElement("div"); d.className = cls; d.textContent = text; parent.appendChild(d); return d; }
  function mergeCard(el, patch) { paintCard(el, Object.assign({}, el._card || {}, patch)); }
  function paintCard(el, card) {
    el._card = card;
    el.textContent = "";
    el.className = "ca-card " + (card.state === "pending" ? "pending" : "");
    var p = card.payload || {};
    if (card.kind === "question") {
      line(el, "k", "Question for you");
      line(el, "q", p.question || "");
      if (p.context) line(el, "c", p.context);
    } else {
      line(el, "k", "Confirm");
      line(el, "q", p.summary || "");
      if (p.detail) line(el, "detail", p.detail);   // the WHOLE text the tap would record, scrollable (textContent only)
    }
    if (card.state !== "pending") {
      line(el, card.state === "failed" ? "fail" : "done", card.state === "dismissed" ? "Dismissed." : (card.result || card.state));
      return;
    }
    if (card.mine === false) { line(el, "c", "Waiting for " + (card.by || "the person who asked") + " to tap this."); return; }   // a card is its asker's to tap
    var row = document.createElement("div"); row.className = "row";
    function tap(label, action, answer, cls) {
      var b = document.createElement("button"); b.type = "button"; b.textContent = label; if (cls) b.className = cls;
      b.addEventListener("click", function () { resolve(card.id, action, answer, el); });
      row.appendChild(b);
    }
    if (card.kind === "question") {
      var opts = p.options || [];
      if (opts.length) opts.forEach(function (o) { tap(String(o), "answer", String(o)); });
      else {
        var ta = document.createElement("textarea"); ta.rows = 2; ta.setAttribute("aria-label", "Your answer"); el.appendChild(ta);
        var b = document.createElement("button"); b.type = "button"; b.className = "go"; b.textContent = "Send answer";
        b.addEventListener("click", function () { if (ta.value.trim()) resolve(card.id, "answer", ta.value.trim(), el); });
        row.appendChild(b);
      }
      tap("Not now", "dismiss", "");
    } else {
      tap("Confirm", "confirm", "", "go");
      tap("Dismiss", "dismiss", "");
    }
    el.appendChild(row);
  }
  function resolve(id, action, answer, el) {
    Array.prototype.forEach.call(el.querySelectorAll("button"), function (b) { b.disabled = true; });
    api("POST", "/ca/cards/" + id, { action: action, answer: answer }).then(function (r) {
      if (r.ok && r.json) { mergeCard(el, { state: r.json.state, result: r.json.result }); }
      else {
        Array.prototype.forEach.call(el.querySelectorAll("button"), function (b) { b.disabled = false; });
        say((r.json && r.json.detail) || "That didn't work. Try again.");
      }
    });
  }

  function paintConversation(c) {
    msgs.textContent = "";
    var byTurn = {};
    c.cards.forEach(function (k) { (byTurn[k.turn_id || 0] = byTurn[k.turn_id || 0] || []).push(k); });
    convShared = !!c.shared; convCanPost = c.can_post !== false; convShareable = !!c.shareable;
    c.messages.forEach(function (m) { addMsg(m.role, m.content, m.id, m.attachments, m.role === "user" ? (m.mine ? "You" : m.author) : ""); });
    c.cards.forEach(function (k) { msgs.appendChild(cardEl(k)); });
    var ctx = c.context || {};
    convEntity = ctx.entity || null;
    titleEl.textContent = "Crewboss Assistant";   // owner 11-09: the header always names the assistant (as the PA's does)
    setBusy(!!c.live_turn);
    if (c.live_turn && c.live_turn.output) { liveEl = addMsg("assistant", c.live_turn.output); if (liveEl) liveEl.classList.add("live"); }
    pending = []; renderFiles();
    renderChip(); layout(); paintDisc();
    toBottom();
  }

  function openConversation(id) {
    return api("GET", "/ca/conversations/" + id).then(function (r) {
      if (!r.ok) { convId = null; store(CONV_KEY, null); newChatView(); return; }
      convId = id; store(CONV_KEY, String(id)); pin();
      paintConversation(r.json);
      discOpen = false; paintDisc();
    });
  }
  function newChatView(entity) {
    convId = null; store(CONV_KEY, null); convEntity = null;
    forced = entity || null; dismissedKey = null; convShared = false; convCanPost = true; convShareable = false;
    msgs.textContent = ""; pending = []; renderFiles();
    titleEl.textContent = "Crewboss Assistant"; setBusy(false);
    renderChip(); layout(); paintDisc();
  }

  // ---- the Discussion panel of the task window (#830) -------------------------------------------------------------------
  // One strip above the thread (#ca-disc): "Discussion · Task #N", All (the task's conversations, newest first, one tap continues one),
  // New (a fresh conversation linked to the task) and, on a phone, a way back to the task. The thread below it is the ordinary assistant.
  // View-only people read it; the composer is hidden for them (the server answers 403 to a post anyway).
  function showingTask(t) {
    if (!t) return false;
    if (convId) return !!convEntity && entKey(convEntity) === entKey(t);
    var o = offered();
    return !!o && entKey(o) === entKey(t);
  }
  function viewOnly() { var t = taskNow(); return !!(t && discData && discData.task && String(discData.task.id) === String(t.id) && !discData.can_post && showingTask(t)); }
  function paintDisc() {
    var t = taskNow();
    disc.hidden = !t;
    drawer.classList.toggle("has-disc", !!t);
    drawer.classList.toggle("ca-ro", viewOnly());
    if (!t) {   // no task in view: clear the count too, so the reused sheet bar never shows the last task's number on the next task
      Array.prototype.forEach.call(document.querySelectorAll("[data-ca-disc-count]"), function (b) { b.textContent = ""; b.hidden = true; });
      return;
    }
    discTitle.textContent = "Discussion · Task #" + t.id;
    var n = discData && discData.items ? discData.items.length : 0;
    discAll.textContent = (discOpen ? "Hide" : "All") + (n ? " (" + n + ")" : "");
    discAll.setAttribute("aria-expanded", discOpen ? "true" : "false");
    discList.hidden = !discOpen;
    discNote.hidden = !viewOnly();
    $("ca-disc-share").hidden = !(convId && convShareable);
    if (viewOnly()) discNote.textContent = "You can read this discussion. You have view-only access to the task, so you can't post.";
    Array.prototype.forEach.call(document.querySelectorAll("[data-ca-disc-count]"), function (b) { b.textContent = n ? String(n) : ""; b.hidden = !n; });
    paintTabs();
  }
  function paintTabs() {
    var on = html.classList.contains("ca-open");
    Array.prototype.forEach.call(document.querySelectorAll("[data-task-tab]"), function (b) {
      b.setAttribute("aria-selected", (b.getAttribute("data-task-tab") === "discussion") === on ? "true" : "false");
    });
  }
  function renderDiscList() {
    discList.textContent = "";
    var items = (discData && discData.items) || [];
    if (!items.length) { var e = document.createElement("div"); e.className = "ca-disc-empty"; e.textContent = "No conversations on this task yet."; discList.appendChild(e); return; }
    items.forEach(function (c) {
      var b = document.createElement(c.open ? "button" : "div");
      b.className = "ca-disc-row" + (c.id && c.id === convId ? " on" : "") + (c.open ? "" : " soft");
      if (c.open) { b.type = "button"; b.addEventListener("click", function () { openConversation(c.id); }); }
      var t = document.createElement("span"); t.className = "t"; t.textContent = c.open ? (c.title || "New conversation") : "A chat by " + c.by + " that changed this task"; b.appendChild(t);
      var m = document.createElement("small");
      m.textContent = (c.mine ? "you" : c.by) + (c.linked ? " · linked" : "") + " · " + ago(c.updated_at) + (c.busy ? " · answering" : "");
      b.appendChild(m);
      discList.appendChild(b);
    });
  }
  function loadDisc() {
    var t = taskNow();
    if (!t) { discData = null; paintDisc(); return Promise.resolve(); }
    return api("GET", "/ca/tasks/" + t.id + "/discussion").then(function (r) {
      var cur = taskNow();
      if (!cur || cur.id !== t.id) return;
      discData = r.ok ? r.json : null;
      renderDiscList(); paintDisc(); layout();
    });
  }
  function openDiscussion(noFocus) {
    var t = taskNow();
    if (!t) { setOpen(true, true); return; }
    setOpen(true, true, false, noFocus);
    (bootP || Promise.resolve()).then(function () {
      // an open conversation (a link brought the person here with it) is kept, never thrown away: the list is offered beside it
      if (convId && showingTask(t)) discOpen = false;
      else if (convId) discOpen = true;
      else { newChatView({ kind: "task", id: t.id, title: t.title }); discOpen = true; }
      loadDisc();
    });
  }
  $("ca-disc-share").addEventListener("click", function () {     // an older task chat is its starter's own until they share it
    if (!convId || !window.confirm("Share this conversation with everyone who can see this task? They will be able to read all of it, and people who can edit the task can continue it.")) return;
    api("POST", "/ca/conversations/" + convId + "/share", {}).then(function (r) {
      if (r.ok) openConversation(convId).then(loadDisc); else say((r.json && r.json.detail) || "I couldn't share that.");
    });
  });
  discAll.addEventListener("click", function () { discOpen = !discOpen; paintDisc(); if (discOpen) loadDisc(); });
  $("ca-disc-new").addEventListener("click", function () {
    var t = taskNow(); if (!t) return;
    newChatView({ kind: "task", id: t.id, title: t.title }); discOpen = false; paintDisc(); input.focus();
  });
  $("ca-disc-global").addEventListener("click", function () {
    var t = taskNow(); newChatView(); if (t) dismissedKey = entKey(t); discOpen = false; renderChip(); paintDisc(); input.focus();
  });
  $("ca-disc-back").addEventListener("click", function () { closeByPerson(); });

  // ---- the empty state: the composer in the middle, recent conversations under it ------------------------------------------
  function isEmpty() { return !convId || !msgs.firstChild; }
  function layout() {
    var empty = !msgs.firstChild;
    hero.hidden = !empty;
    var form = $("ca-form");
    var want = empty ? slot : dock;
    if (form.parentNode !== want) want.appendChild(form);
    drawer.classList.toggle("is-empty", empty);
    if (!empty) anchor(false);
    greeting.textContent = firstName ? "What\u2019s on, " + firstName + "?" : "What\u2019s on?";
    renderRecent();
    paintSend();
  }
  function entLine(ctx) { var e = ctx && ctx.entity; return e ? (e.label || KIND_LABEL[e.kind] || e.kind) + (e.title ? " · " + e.title : "") : ""; }
  function renderRecent() {
    recentList.textContent = "";
    var list = recent.slice(0, 3);
    recentBox.hidden = !list.length;
    list.forEach(function (c) {
      var b = document.createElement("button"); b.type = "button"; b.className = "ca-rc";
      var t = document.createElement("span"); t.className = "t"; t.textContent = c.title || "New conversation"; b.appendChild(t);
      var line = entLine(c.context);
      if (line) { var e = document.createElement("small"); e.className = "e"; e.textContent = line; b.appendChild(e); }    // the linked item, in the accent, on its own line
      var w = document.createElement("small"); w.textContent = ago(c.updated_at); b.appendChild(w);
      b.addEventListener("click", function () { openConversation(c.id); });
      recentList.appendChild(b);
    });
  }

  // ---- conversations list ------------------------------------------------------------------------------------------
  // The PA's formatter (pa.js ago / when): "just now", "N min ago", "N h ago", then the date and time.
  function when(iso) { try { return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }); } catch (e) { return ""; } }
  function ago(iso) {
    var t = Date.parse(iso); if (!t) return "";
    var d = (Date.now() - t) / 1000;
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + " min ago";
    if (d < 86400) return Math.floor(d / 3600) + " h ago";
    return when(iso);
  }
  // ---- sending -----------------------------------------------------------------------------------------------------
  function ensureConversation() {
    if (convId) return Promise.resolve(convId);
    var c = pageContext();
    return api("POST", "/ca/conversations", { path: c.path, project: c.project, entity: c.entity }).then(function (r) {
      if (!r.ok) { say((r.json && r.json.detail) || "I couldn't start a conversation."); return null; }
      convShared = !!((r.json.context || {}).entity && r.json.context.entity.kind === "task"); convCanPost = true;
      convId = r.json.id; store(CONV_KEY, String(convId)); pin();
      convEntity = (r.json.context || {}).entity || null; forced = null;      // the server's checked answer, not the page's
      renderChip();
      return convId;
    });
  }
  var sentEl = null, sentText = null;              // the person's message shown before the server has answered with its id
  function send() {
    if (mic && mic.recording()) mic.stop();          // Enter / Send ends the listen first: the last words are in the box
    var text = input.value.trim();
    if ((!text && !pending.length) || busy || signedOut || uploading) return;
    say("");
    var files = pending.slice();
    ensureConversation().then(function (id) {
      if (!id) return;
      var el = addMsg("user", text, null, files.map(function (f) { return { id: f.id, filename: f.name, kind: f.kind }; }), "You");
      sentEl = el; sentText = text;                 // the live "message" event may beat the POST's answer: it adopts this one (owner 11-18: doubled)
      input.value = ""; fit(); pending = []; renderFiles(); paintSend();
      setBusy(true);
      api("POST", "/ca/conversations/" + id + "/messages", { text: text, attachments: files.map(function (f) { return f.id; }) }).then(function (r) {
        if (sentEl === el) { sentEl = null; sentText = null; }
        if (r.ok) {
          if (el && r.json) {
            var twin = msgs.querySelector('[data-mid="' + r.json.message_id + '"]');
            if (twin && twin !== el) el.remove(); else el.setAttribute("data-mid", r.json.message_id);
          }
          loadState(); return;
        }
        if (el) el.remove();
        layout();
        input.value = text; pending = files; renderFiles();      // nothing was stored: the words and files stay in the box
        setBusy(false);
        say((r.json && r.json.detail) || "That didn't send.");
        if (r.status === 403) loadDisc();
      });
    });
  }
  sendBtn.addEventListener("click", function () { send(); });
  enterBtn.addEventListener("click", function () { send(); });
  input.addEventListener("input", function () { fit(); paintSend(); });
  function fit() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, Math.round(window.innerHeight * 0.4)) + "px"; }
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  stopBtn.addEventListener("click", function () { if (convId) api("POST", "/ca/conversations/" + convId + "/stop", {}); });
  $("ca-close").addEventListener("click", function () { closeByPerson(); });

  // ---- files: the + button, paste, drop ------------------------------------------------------------------------------------
  var MAX_FILES = 10;
  function fmtSize(n) { return n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB"; }
  function renderFiles() {
    filesBox.textContent = "";
    pending.forEach(function (f, i) {
      var c = document.createElement("span"); c.className = "ca-file" + (f.kind === "image" ? " img" : "");
      if (f.kind === "image") { var im = document.createElement("img"); im.src = "/ca/uploads/" + f.id; im.alt = ""; c.appendChild(im); }
      var n = document.createElement("span"); n.className = "n"; n.textContent = f.name; n.title = f.name + " (" + fmtSize(f.size) + ")"; c.appendChild(n);
      var x = document.createElement("button"); x.type = "button"; x.className = "x"; x.setAttribute("aria-label", "Remove " + f.name); x.textContent = "×";
      x.addEventListener("click", function () {
        pending.splice(i, 1); renderFiles();
        api("DELETE", "/ca/uploads/" + f.id);
      });
      c.appendChild(x); filesBox.appendChild(c);
    });
    if (uploading) { var u = document.createElement("span"); u.className = "ca-file"; u.textContent = "Uploading…"; filesBox.appendChild(u); }
    filesBox.hidden = !filesBox.firstChild;
    paintSend();
  }
  function uploadFiles(list) {
    var files = Array.prototype.slice.call(list || []);
    if (!files.length || signedOut) return;
    if (pending.length + files.length > MAX_FILES) { say("At most " + MAX_FILES + " files go with one message."); files = files.slice(0, Math.max(0, MAX_FILES - pending.length)); }
    if (!files.length) return;
    uploading += files.length; renderFiles(); say("");
    ensureConversation().then(function (id) {
      if (!id) { uploading -= files.length; renderFiles(); return; }
      var chain = Promise.resolve();
      files.forEach(function (f) {
        chain = chain.then(function () {
          var name = f.name || ("pasted-" + Date.now() + "." + ((f.type || "image/png").split("/")[1] || "png"));
          return api("POST", "/ca/conversations/" + id + "/uploads?name=" + encodeURIComponent(name), f, true).then(function (r) {
            uploading--;
            if (r.ok && r.json) pending.push({ id: r.json.id, name: r.json.filename, kind: r.json.kind, size: r.json.size });
            else say((r.json && r.json.detail) || "I couldn't take " + name + ".");
            renderFiles();
          });
        });
      });
    });
  }
  attachBtn.addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () { uploadFiles(fileInput.files); fileInput.value = ""; });
  input.addEventListener("paste", function (e) {         // a screenshot on the clipboard; plain text pastes as usual
    var items = (e.clipboardData && e.clipboardData.files) || [];
    if (items.length) { e.preventDefault(); uploadFiles(items); }
  });
  ["dragenter", "dragover"].forEach(function (n) {
    drawer.addEventListener(n, function (e) {
      if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0) { e.preventDefault(); drawer.classList.add("is-drop"); }
    });
  });
  drawer.addEventListener("dragleave", function (e) { if (e.target === drawer || !drawer.contains(e.relatedTarget)) drawer.classList.remove("is-drop"); });
  drawer.addEventListener("drop", function (e) {
    drawer.classList.remove("is-drop");
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) { e.preventDefault(); uploadFiles(e.dataTransfer.files); }
  });

  // ---- the microphone: only when the server has a speech provider -------------------------------------------------------------
  function micState(st) { micBtn.classList.toggle("is-on", st !== "idle"); setTimeout(paintSend, 0); if (st === "idle") { vad = null; meterBar.style.width = "0"; } }
  function startMeter(stream, ctx, src) {
    try { vad = window.crewbossVoice.vad(ctx, src); } catch (e) { vad = null; }
    cancelAnimationFrame(meterRaf);
    (function tick() {
      if (vad && !devMenu.hidden) { var l = vad.sample(); meterBar.style.width = Math.max(0, Math.min(100, (l.over / 30) * 100)) + "%"; }
      meterRaf = vad ? requestAnimationFrame(tick) : 0;
    })();
  }
  function initMic() {
    if (mic || !speechOn || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
    var src = drawer.getAttribute("data-voice-src");
    if (!src) return;
    function ready() {
      if (!window.crewbossSpeech) return;
      mic = window.crewbossSpeech(input, micBtn, {
        deviceId: function () { return load(MIC_KEY) || undefined; },
        onState: micState, onMessage: function (m) { say(m); },
        onDeviceGone: function () { store(MIC_KEY, null); say("That microphone isn't available; using the default one."); },
        stream: startMeter
      });
      micGroup.hidden = false;
      micBtn.addEventListener("click", function () { mic.toggle(); input.focus(); });
    }
    if (window.crewbossSpeech) { ready(); return; }
    var sc = document.createElement("script"); sc.src = src; sc.onload = ready; document.head.appendChild(sc);
  }
  function paintDevices() {
    devList.textContent = "";
    var cur = load(MIC_KEY) || "";
    function row(label, id) {
      var b = document.createElement("button"); b.type = "button"; b.className = "ca-dev"; b.setAttribute("role", "menuitemradio"); b.setAttribute("aria-checked", id === cur ? "true" : "false");
      var l = document.createElement("span"); l.textContent = label; b.appendChild(l);
      var tick = document.createElement("span"); tick.className = "ca-tick"; tick.textContent = id === cur ? "✓" : ""; b.appendChild(tick);
      b.addEventListener("click", function () {
        store(MIC_KEY, id || null); cur = id; paintDevices();
        if (mic && mic.recording()) { mic.stop(); mic.toggle(); }     // restart on the chosen input; the typed text and words so far stay
      });
      devList.appendChild(b);
    }
    row("Default", "");
    if (!navigator.mediaDevices.enumerateDevices) return;
    navigator.mediaDevices.enumerateDevices().then(function (all) {
      var n = 0;
      all.forEach(function (d) {
        if (d.kind !== "audioinput" || d.deviceId === "default" || d.deviceId === "communications") return;
        n++; row(d.label || "Microphone " + n, d.deviceId);
      });
    }).catch(function () {});
  }
  function toggleDevMenu(on) {
    if (on === undefined) on = devMenu.hidden;
    devMenu.hidden = !on; micMenuBtn.setAttribute("aria-expanded", on ? "true" : "false");
    if (on) paintDevices();
  }
  micMenuBtn.addEventListener("click", function (e) { e.stopPropagation(); toggleDevMenu(); });
  document.addEventListener("click", function (e) { if (!devMenu.hidden && !micGroup.contains(e.target)) toggleDevMenu(false); });
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) navigator.mediaDevices.addEventListener("devicechange", function () { if (!devMenu.hidden) paintDevices(); });

  // ---- hooks other pages use: open a conversation, start a discussion on an item, archive from the history page -------------
  document.addEventListener("click", function (e) {
    var t = e.target && e.target.closest ? e.target : null;
    if (!t) return;
    var open = t.closest("[data-ca-open-conv]");
    if (open) { e.preventDefault(); var id = parseInt(open.getAttribute("data-ca-open-conv"), 10); if (id) { setOpen(true); openConversation(id); } return; }
    var nw = t.closest("[data-ca-new]");
    if (nw) {
      e.preventDefault();
      var v = nw.getAttribute("data-ca-new") || "", i = v.indexOf(":");
      setOpen(true);
      newChatView(i > 0 ? { kind: v.slice(0, i), id: v.slice(i + 1), title: nw.getAttribute("data-ca-title") || "" } : null);
      input.focus();
      return;
    }
    var ar = t.closest("[data-ca-archive]");
    if (ar) {
      var aid = ar.getAttribute("data-ca-archive");
      api("POST", "/ca/conversations/" + aid + "/archive", {}).then(function (r) {
        if (!r.ok) return;
        var row = ar.closest("[data-conv-row]"); if (row) row.remove();
        if (String(convId) === String(aid)) newChatView();
        loadState();
      });
    }
  });

  // ---- the live feed -----------------------------------------------------------------------------------------------
  function toast(n) {
    var root = document.getElementById("toast-root");
    if (!root) return;
    var el = document.createElement("div");
    el.className = "pointer-events-auto rounded-xl border px-3.5 py-2.5 text-small shadow-lg max-w-sm";
    el.style.cssText = "background:rgb(var(--c-surface));border-color:rgb(var(--c-line));color:rgb(var(--c-charcoal));font-size:.84rem;cursor:pointer;max-width:22rem";
    var t = document.createElement("div"); t.style.fontWeight = "600"; t.textContent = n.title; el.appendChild(t);
    if (n.body) { var b = document.createElement("div"); b.style.opacity = ".7"; b.textContent = n.body; el.appendChild(b); }
    el.addEventListener("click", function () { el.remove(); if (n.url && n.url.charAt(0) === "/") location.href = n.url; });
    root.appendChild(el);
    setTimeout(function () { el.remove(); }, 9000);
  }
  function badge(n) {
    unread = n;
    Array.prototype.forEach.call(document.querySelectorAll("[data-ca-badge]"), function (b) {
      b.textContent = n > 9 ? "9+" : String(n); b.hidden = n < 1;
    });
  }
  function connect() {
    if (es || typeof window.EventSource !== "function") return;
    es = new EventSource("/ca/stream");
    function on(name, fn) { es.addEventListener(name, function (ev) { var d = null; try { d = JSON.parse(ev.data); } catch (e) {} if (d) fn(d); }); }
    on("delta", function (d) {
      if (d.conversation_id !== convId) return;
      if (!liveEl) { liveEl = addMsg("assistant", ""); if (liveEl) liveEl.classList.add("live"); setBusy(true); toolLine.hidden = true; }
      if (liveEl) { renderText(liveEl, (liveEl._raw || "") + d.text); toBottom(); }
    });
    on("tool", function (d) {
      if (d.conversation_id !== convId) return;
      toolLine.textContent = "Working: " + d.name.replace(/_/g, " ") + "…"; toolLine.hidden = false;
    });
    on("message", function (d) {
      if (d.conversation_id === convId && d.message.role === "system") { addMsg("system", d.message.content, d.message.id); return; }   // a look's answer
      if (d.conversation_id === convId && d.message.role === "user" && sentEl && !sentEl.getAttribute("data-mid") && d.message.content === sentText) {
        sentEl.setAttribute("data-mid", d.message.id); sentEl = null; sentText = null; setBusy(true); return;   // our own send, already on screen
      }
      if (d.conversation_id === convId && d.message.role === "user") addMsg("user", d.message.content, d.message.id, (d.message.attachments || []).map(function (a) { return { id: a.id, filename: a.filename, kind: a.kind }; }), d.message.author_id === myId ? "You" : d.message.author) && setBusy(true);
      if (d.message.role === "user" && taskNow()) loadDisc();
    });
    on("turn_done", function (d) {
      if (d.conversation_id !== convId) return;
      if (liveEl) liveEl.remove();
      addMsg(d.message.role, d.message.content, d.message.id);
      setBusy(false);
      if (taskNow()) loadDisc();
      api("GET", "/ca/conversations/" + convId).then(function (r) {   // pick up cards the turn made, in their place
        if (r.ok) r.json.cards.forEach(function (k) { if (!msgs.querySelector('[data-card="' + k.id + '"]')) msgs.appendChild(cardEl(k)); });
        toBottom();
      });
    });
    on("card", function (d) {
      d.card.mine = d.card.user_id === myId;       // a card is its asker's to tap: the feed says whose it is, each browser decides (never "mine" from the server)
      if (d.conversation_id === convId && !msgs.querySelector('[data-card="' + d.card.id + '"]')) { msgs.appendChild(cardEl(d.card)); toBottom(); }
    });
    on("tool", function (d) { if (d.conversation_id === convId && d.name === "look") { toolLine.textContent = "Looking at the code…"; toolLine.hidden = false; } });
    on("card_resolved", function (d) {
      var el = msgs.querySelector('[data-card="' + d.id + '"]');
      if (el) mergeCard(el, { state: d.state, result: d.result });
    });
    on("notice", function (n) { toast(n); badge(unread + 1); });
    es.addEventListener("open", function () { if (convId && ready) openConversation(convId); });   // missed frames: reload the open chat
  }

  // ---- opening and closing -----------------------------------------------------------------------------------------
  // THE THREAD RULE (the PA's, owner 10-30): a conversation belongs to the page AND the item selected on it when it began. Opening the drawer
  // again on the same page and item carries on; another page or item starts a fresh, unlinked-until-sent chat. There is no "new chat" button.
  // OWNER RULE 11-16 (reconciles the thread rule): a conversation remembers and is not cleared until the person MOVES AWAY WHILE IT IS CLOSED.
  // While it is open, any navigation (a CA link, the menu, Back, a full page load) keeps it: the pin follows the page. Closed, then another
  // page = the next open starts a fresh conversation (the old one stays in history). Closed, same page = the same conversation. The pin is
  // the PAGE only: the item a thread began on stays on its chip and is never rebound by moving around.
  // The "place" a closed conversation belongs to (owner 11-18, "created a new conversation under the project. Grrr"): the PROJECT, not
  // the exact page. A task window puts /<project>/tasks/N in the address bar, so closing CA over a task and reopening it on the board
  // started a fresh chat. Now anywhere inside one project is one place, and Now / Dashboard / Recent / the other global pages are one place.
  var GLOBAL_FIRST = { "": 1, "dashboard": 1, "recent": 1, "assets": 1, "admin": 1, "all": 1, "machines": 1, "account": 1 };
  function here() {
    var first = location.pathname.split("/")[1] || "";
    return GLOBAL_FIRST[first] ? "global" : "/" + first;
  }
  function pin() { try { sessionStorage.setItem("crewboss-ca-pin", JSON.stringify({ id: convId, h: here() })); } catch (e) {} }
  function pinned() { try { return JSON.parse(sessionStorage.getItem("crewboss-ca-pin") || "null"); } catch (e) { return null; } }
  var minimised = false;          // closed only to make room for a page the assistant's link opened: not a "close" for the rule above
  function closeByPerson() { setOpen(false); }
  function setOpen(on, byPerson, keepPin, noFocus) {
    if (!on && convId && !keepPin && html.classList.contains("ca-open")) pin();   // closed HERE: the thread belongs to this page now
    minimised = !on && !!keepPin;
    html.classList.toggle("ca-open", on);
    drawer.setAttribute("aria-hidden", on ? "false" : "true");
    store(KEY, on ? "1" : null);
    if (on) {
      if (unread) { api("POST", "/ca/notices/read", {}); badge(0); }
      var pn = pinned();
      if (byPerson && convId && pn && pn.id === convId && pn.h !== here()) newChatView();
      if (!ready) boot(byPerson); else refreshRunner();
      renderChip();
      if (!noFocus) setTimeout(function () { if (html.classList.contains("ca-open")) input.focus({ preventScroll: true }); }, 240);
    }
    paintDisc();
  }
  function loadState() {
    return api("GET", "/ca/state").then(function (r) {
      if (r.status === 403) { signedOut = true; say("CA needs you signed in to Crewboss (it works as you, on your own runner)."); return; }
      if (!r.ok) return;
      myId = r.json.user_id || myId;
      firstName = r.json.first_name || ""; recent = r.json.conversations || []; speechOn = !!r.json.speech;
      if (speechOn) initMic();
      layout();
      if (!r.json.online) {
        say(r.json.runners.length
          ? "Your runner is offline: start it on your PC with `crewboss-runner run`. I can't answer until it is."
          : "You have no runner yet. On a PC with Claude Code signed in, make an enrolment code on this account's Runners page, then run `crewboss-runner enrol <code>` and `crewboss-runner run`.");
      } else say("");
    });
  }
  function refreshRunner() { return loadState(); }
  function saveScroll() { if (convId) { try { sessionStorage.setItem("crewboss-ca-scroll", JSON.stringify({ id: convId, top: scroll.scrollTop })); } catch (e) {} } }
  function restoreScroll(id) {
    try {
      var sv = JSON.parse(sessionStorage.getItem("crewboss-ca-scroll") || "null");
      if (sv && sv.id === id) scroll.scrollTop = sv.top;
    } catch (e) {}
  }
  window.addEventListener("pagehide", saveScroll);
  function boot(byPerson) {
    ready = true;
    refreshRunner();
    var saved = parseInt(load(CONV_KEY) || "", 10);
    var pn = pinned();
    if (saved && byPerson && pn && pn.id === saved && pn.h !== here()) saved = 0;       // opened by hand on another page than it was closed on
    bootP = saved ? openConversation(saved).then(function () { restoreScroll(saved); }) : (newChatView(), Promise.resolve());
    renderChip();
  }

  // Navigation by the page-swap (js/pagenav.js): the open (or just-minimised) conversation follows the person to the new page, unchanged.
  document.addEventListener("crewboss:page", function () {
    if (convId && (html.classList.contains("ca-open") || minimised)) pin();
    minimised = false;
    chipSoon();
  });
  window.crewbossCA = {
    isOpen: function () { return html.classList.contains("ca-open"); },
    minimise: function () { if (html.classList.contains("ca-open")) setOpen(false, false, true); },
    openDiscussion: function () { openDiscussion(); },
    close: function () { closeByPerson(); }
  };
  // The assistant button is context-aware (#830): with a task window open it opens (or focuses) THAT task's Discussion, and closes it when
  // it is already showing; anywhere else it opens or closes the global assistant as before.
  function toggle() {
    var t = taskNow(), isOpen = html.classList.contains("ca-open");
    if (t && !(isOpen && showingTask(t))) { openDiscussion(); return; }
    if (isOpen) closeByPerson(); else setOpen(true, true);
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-ca-open]"), function (b) { b.addEventListener("click", toggle); });
  document.addEventListener("click", function (e) {      // the Task | Discussion tabs of the task window: the way to the Discussion on every screen (#954: it never opens by itself)
    var tab = e.target && e.target.closest ? e.target.closest("[data-task-tab]") : null;
    if (!tab) return;
    if (tab.getAttribute("data-task-tab") === "discussion") { openDiscussion(); } else if (html.classList.contains("ca-open")) closeByPerson();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && html.classList.contains("ca-open") && !e.defaultPrevented) {
      if (document.activeElement && document.activeElement.closest && document.activeElement.closest("[data-jump]")) return;
      closeByPerson();
    }
  });

  // The phone sheet is dragged down to close (js/chrome.js dragSheet, the ERP's controller): the handle, or the thread pulled down from its top.
  if (window.dragSheet) window.dragSheet($("ca-panel"), { backdrop: $("ca-backdrop"), onDismiss: function () { setOpen(false); } });

  // The feed (toasts and the badge) runs on every page, open or not; the unread count comes with the first state.
  api("GET", "/ca/state").then(function (r) {
    if (r.status === 403) { signedOut = true; return; }
    if (!r.ok) return;
    badge(r.json.unread || 0);
    myId = r.json.user_id || myId;
    connect();
    viewChanged();
  });
  if (load(KEY) === "1") setOpen(true);
})();
