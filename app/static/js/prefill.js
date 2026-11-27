// /tasks/new#prefill=<base64url JSON> — the ERP's "remember this" link. Read CLIENT-SIDE ONLY from the URL
// fragment (a fragment is never sent to the server); nothing is submitted until the owner reviews and sends
// the form. The JSON is UNTRUSTED: only the known fields survive, each type- and length-checked; unknown keys
// are dropped; values are written with .value / .textContent, never as HTML. The server validates the
// submitted form again (app/routers/pages.py create_new_request).
//
// Shape: {title, description, stream, assignee, subtasks: [..], links: {workflows: [{id, step?}]},
//         suggestion: {conditions: <any JSON>}}
(function (factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;  // node tests
  else window.mddPrefill = api;
})(function () {
  "use strict";

  var STREAMS = ["feature", "bug", "tweak", "review", "error"];
  var ASSIGNEES = ["ai", "owner"];
  var WF_ID = /^[a-z0-9][a-z0-9._-]{0,120}$/;
  var STEP_ID = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}$/;
  var MAX_FRAGMENT = 16000, MAX_CONDITIONS = 4000;

  function oneLine(s, max) {
    return String(s).replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, max);
  }

  function decode(fragment) {
    // "#prefill=<b64url>" | "prefill=<b64url>" -> object, or null on anything malformed
    var m = /(?:^|[#&])prefill=([A-Za-z0-9_-]+={0,2})(?:&|$)/.exec(String(fragment || ""));
    if (!m || m[1].length > MAX_FRAGMENT) return null;
    try {
      var b64 = m[1].replace(/-/g, "+").replace(/_/g, "/");
      while (b64.length % 4) b64 += "=";
      var bin = typeof atob === "function" ? atob(b64) : Buffer.from(b64, "base64").toString("binary");
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      var json = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      var data = JSON.parse(json);
      return data && typeof data === "object" && !Array.isArray(data) ? data : null;
    } catch (e) {
      return null;
    }
  }

  function sanitize(raw) {
    var out = {};
    if (!raw || typeof raw !== "object") return out;
    if (typeof raw.title === "string" && oneLine(raw.title, 300)) out.title = oneLine(raw.title, 300);
    if (typeof raw.description === "string") out.description = raw.description.replace(/\u0000/g, "").slice(0, 20000);
    if (STREAMS.indexOf(raw.stream) >= 0) out.stream = raw.stream;
    if (ASSIGNEES.indexOf(raw.assignee) >= 0) out.assignee = raw.assignee;
    if (Array.isArray(raw.subtasks)) {
      var steps = raw.subtasks.filter(function (s) { return typeof s === "string"; })
        .map(function (s) { return oneLine(s, 500); }).filter(Boolean).slice(0, 20);
      if (steps.length) out.subtasks = steps;
    }
    var wfs = raw.links && typeof raw.links === "object" && Array.isArray(raw.links.workflows) ? raw.links.workflows : [];
    var links = [];
    wfs.forEach(function (w) {
      if (!w || typeof w !== "object" || typeof w.id !== "string" || !WF_ID.test(w.id)) return;
      var e = { id: w.id };
      if (typeof w.step === "string" && STEP_ID.test(w.step)) e.step = w.step;
      if (!links.some(function (x) { return x.id === e.id && x.step === e.step; })) links.push(e);
    });
    if (links.length) out.workflows = links.slice(0, 10);
    if (raw.suggestion && typeof raw.suggestion === "object" && raw.suggestion.conditions !== undefined) {
      var c = JSON.stringify(raw.suggestion.conditions);
      if (typeof c === "string" && c.length <= MAX_CONDITIONS) out.conditions = c;
    }
    return out;
  }

  function apply(form, p, doc) {
    doc = doc || document;
    var q = function (sel) { return form.querySelector(sel); };
    if (p.title) q('[data-prefill="title"]').value = p.title;
    if (p.description) q('[data-prefill="description"]').value = p.description;
    if (p.subtasks) q('[data-prefill="subtasks"]').value = p.subtasks.join("\n");
    ["stream", "assignee"].forEach(function (k) {
      if (!p[k]) return;
      form.querySelectorAll('input[type="radio"][name="' + k + '"]').forEach(function (r) { r.checked = r.value === p[k]; });
    });
    if (p.workflows) {
      var field = q('[data-prefill="workflows"]');
      var have = [];
      try { have = JSON.parse(field.value || "[]"); } catch (e) { have = []; }
      p.workflows.forEach(function (w) {
        if (!have.some(function (x) { return x.id === w.id && x.step === w.step; })) have.push(w);
      });
      field.value = JSON.stringify(have);
      var chips = doc.getElementById("linked-chips");
      p.workflows.forEach(function (w) {
        var span = doc.createElement("span");
        span.className = "chip bg-charcoal/[.06] text-charcoal/70 normal-case";
        span.textContent = w.id + (w.step ? " · " + w.step : "");
        chips.appendChild(span);
      });
      doc.getElementById("linked").hidden = false;
    }
    if (p.conditions) {
      q('[data-prefill="conditions"]').value = p.conditions;
      doc.getElementById("linked").hidden = false;
      doc.getElementById("conditions-note").hidden = false;
    }
    var note = doc.getElementById("prefill-note");
    if (note) note.hidden = false;
  }

  function boot() {
    var form = document.getElementById("new-request");
    if (!form || !/prefill=/.test(location.hash)) return;
    var p = sanitize(decode(location.hash));
    if (Object.keys(p).length) apply(form, p);
    // drop the fragment so a reload doesn't overwrite the owner's edits
    history.replaceState(null, "", location.pathname + location.search);
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }

  return { decode: decode, sanitize: sanitize, apply: apply };
});
