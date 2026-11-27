// Step-up (#809). A sensitive action answers 403 + HX-Trigger "stepup-required" when the session has no recent passkey tap. This opens a
// small dialog, runs the passkey re-confirm (js/passkey.js: window.CrewbossPasskey.confirm), and then re-sends the original request by
// re-triggering the element that made it. Pages that aren't htmx (a plain form) get a full "Confirm it's you" page instead, which uses
// the same confirm and then goes back.
(function () {
  var dlg = null, pending = null;

  function withPasskey(cb) {
    if (window.CrewbossPasskey) return cb();
    var tag = document.querySelector("script[data-stepup-passkey-src]");
    var s = document.createElement("script");
    s.src = tag ? tag.getAttribute("data-stepup-passkey-src") : "/static/js/passkey.js";
    s.onload = function () { cb(); };
    document.head.appendChild(s);
  }

  function build() {
    dlg = document.createElement("dialog");
    dlg.setAttribute("data-stepup-dialog", "");
    dlg.className = "rounded-xl border border-line bg-surface text-charcoal p-5 max-w-sm w-[calc(100%-2rem)]";
    dlg.innerHTML = '<h2 class="text-lead font-semibold mb-1">Confirm it\'s you</h2>' +
      '<p class="text-small text-charcoal/70 mb-3">Type the 6-digit code from your authenticator app.</p>' +
      '<input class="field w-full mb-2" inputmode="numeric" autocomplete="one-time-code" maxlength="12" aria-label="6-digit code" data-stepup-code>' +
      '<p class="text-small mb-2" data-stepup-status role="status" aria-live="polite"></p>' +
      '<div class="flex gap-2 justify-end flex-wrap"><button type="button" class="btn-ghost text-small border border-line" data-stepup-cancel>Cancel</button>' +
      '<button type="button" class="btn-ghost text-small border border-line" data-stepup-passkey hidden>Use my passkey instead</button>' +
      '<button type="button" class="btn-primary text-small" data-stepup-confirm>Confirm</button></div>';
    document.body.appendChild(dlg);
    dlg.querySelector("[data-stepup-cancel]").addEventListener("click", function () { pending = null; dlg.close(); });
    dlg.querySelector("[data-stepup-confirm]").addEventListener("click", runCode);
    dlg.querySelector("[data-stepup-passkey]").addEventListener("click", run);
    dlg.querySelector("[data-stepup-code]").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); runCode(); } });
  }

  function finish() {
    var elt = pending; pending = null; dlg.close(); say("");
    resend(elt);
  }

  function postCode(code) {
    return fetch("/account/security/reauth", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
      body: "code=" + encodeURIComponent(code)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || "That code didn't work.");
        return j;
      });
    });
  }

  function runCode() {
    var inp = dlg.querySelector("[data-stepup-code]"), go = dlg.querySelector("[data-stepup-confirm]");
    go.disabled = true; say("Checking...");
    postCode(inp.value).then(function () { go.disabled = false; inp.value = ""; finish(); })
      .catch(function (e) { go.disabled = false; say((e && e.message) || "That didn't work. Try again."); });
  }

  function say(t) { dlg.querySelector("[data-stepup-status]").textContent = t; }

  function resend(elt) {
    if (!elt || !window.htmx) return;
    var tag = (elt.tagName || "").toLowerCase();
    window.htmx.trigger(elt, tag === "form" ? "submit" : (tag === "input" || tag === "select" || tag === "textarea") ? "change" : "click");
  }

  function run() {
    var go = dlg.querySelector("[data-stepup-passkey]");
    go.disabled = true; say("Waiting for your device...");
    withPasskey(function () {
      window.CrewbossPasskey.confirm().then(function () {
        go.disabled = false; finish();
      }).catch(function (e) {
        go.disabled = false;
        say(e && e.name === "NotAllowedError" ? "Cancelled or timed out." : (e && e.message) || "That didn't work. Try again.");
      });
    });
  }

  document.addEventListener("stepup-required", function (evt) {
    if (!dlg) build();
    pending = evt.target;
    say("");
    if (!dlg.open) dlg.showModal();
    var pk = dlg.querySelector("[data-stepup-passkey]");
    pk.hidden = true;
    fetch("/account/security/stepup-info", { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); }).then(function (j) { pk.hidden = !(j && j.passkey); }).catch(function () {});
    dlg.querySelector("[data-stepup-code]").focus();
  });

  // The full "Confirm it's you" page for a request that wasn't htmx: a code form, and a passkey button where there is one.
  document.addEventListener("submit", function (evt) {
    var f = evt.target.closest && evt.target.closest("[data-stepup-page-code]");
    if (!f) return;
    evt.preventDefault();
    var status = document.querySelector("[data-passkey-status]");
    postCode(f.elements.code.value).then(function () { history.back(); })
      .catch(function (e) { if (status) status.textContent = (e && e.message) || "That didn't work."; });
  });
  document.addEventListener("click", function (evt) {
    var b = evt.target.closest && evt.target.closest("[data-stepup-page-go]");
    if (!b) return;
    var status = document.querySelector("[data-passkey-status]");
    b.disabled = true;
    withPasskey(function () {
      window.CrewbossPasskey.confirm().then(function () { history.back(); }).catch(function (e) {
        b.disabled = false;
        if (status) status.textContent = e && e.name === "NotAllowedError" ? "Cancelled or timed out." : (e && e.message) || "That didn't work.";
      });
    });
  });
  // Show the passkey button on the full page only for a person who has one.
  var pageGo = document.querySelector("[data-stepup-page-go]");
  if (pageGo) fetch("/account/security/stepup-info", { credentials: "same-origin", headers: { "Accept": "application/json" } })
    .then(function (r) { return r.json(); }).then(function (j) { pageGo.hidden = !(j && j.passkey); }).catch(function () {});
})();
