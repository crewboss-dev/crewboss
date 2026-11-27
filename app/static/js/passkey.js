// Passkeys (WebAuthn) for local sign-in (#706). Plain JS, no framework. Buttons carry the work in data- attributes:
//   <button data-passkey="register"     data-options-url=... data-verify-url=...>   add a passkey (enrol / account page)
//   <button data-passkey="authenticate" data-options-url=... data-verify-url=...>   sign in with one / re-confirm a session
//        add data-auto to open the prompt on page load (the identifier step)
// The server makes the challenge (options URL, POST) and checks the answer (verify URL, POST); this file only carries
// bytes between the server and navigator.credentials. Buttons stay hidden where the browser has no WebAuthn.
(function () {
  if (!window.PublicKeyCredential || !navigator.credentials) return;

  function b64uToBuf(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = atob(s), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }
  function bufToB64u(buf) {
    var bytes = new Uint8Array(buf), bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function say(msg) {
    document.querySelectorAll("[data-passkey-status]").forEach(function (n) { n.textContent = msg; });
  }
  function post(url, body, signal) {
    return fetch(url, {
      method: "POST", credentials: "same-origin", signal: signal,   // an aborted autofill request never sets a flow cookie
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? "{}" : JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || "That didn't work. Try again.");
        return j;
      });
    });
  }
  function descriptors(list) {
    return (list || []).map(function (d) { return { type: d.type, id: b64uToBuf(d.id), transports: d.transports }; });
  }

  function register(btn) {
    var nameInput = document.querySelector("[data-passkey-name]");
    say("Waiting for your device...");
    return post(btn.dataset.optionsUrl).then(function (o) {
      o.challenge = b64uToBuf(o.challenge);
      o.user.id = b64uToBuf(o.user.id);
      o.excludeCredentials = descriptors(o.excludeCredentials);
      return navigator.credentials.create({ publicKey: o });
    }).then(function (cred) {
      var r = cred.response;
      return post(btn.dataset.verifyUrl, {
        name: nameInput ? nameInput.value : "",
        credential: {
          id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
          response: {
            clientDataJSON: bufToB64u(r.clientDataJSON), attestationObject: bufToB64u(r.attestationObject),
            transports: r.getTransports ? r.getTransports() : []
          },
          clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {}
        }
      });
    }).then(function (j) {
      say("Passkey added.");
      location.href = j.redirect || "/";
    });
  }

  function assertionJson(optionsUrl, verifyUrl) {
    return post(optionsUrl).then(function (o) {
      o.challenge = b64uToBuf(o.challenge);
      o.allowCredentials = descriptors(o.allowCredentials);       // empty at sign-in: the browser shows its own chooser (phone QR code included)
      return navigator.credentials.get({ publicKey: o });
    }).then(function (cred) {
      var r = cred.response;
      return post(verifyUrl, {
        id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
        response: {
          clientDataJSON: bufToB64u(r.clientDataJSON), authenticatorData: bufToB64u(r.authenticatorData),
          signature: bufToB64u(r.signature), userHandle: r.userHandle ? bufToB64u(r.userHandle) : null
        },
        clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {}
      });
    });
  }

  function authenticate(btn) {
    say("Waiting for your device...");
    return assertionJson(btn.dataset.optionsUrl, btn.dataset.verifyUrl).then(function (j) { location.href = j.redirect || "/"; });
  }

  // For the step-up modal (stepup.js): confirm with a passkey and hand back to the caller instead of navigating.
  window.CrewbossPasskey = { confirm: function () {
    return assertionJson("/account/security/passkey/options", "/account/security/passkey/verify");
  } };

  document.querySelectorAll("[data-passkey]").forEach(function (btn) {
    btn.hidden = false;
    btn.addEventListener("click", function () {
      btn.disabled = true;
      var work = btn.dataset.passkey === "register" ? register(btn) : authenticate(btn);
      work.catch(function (e) {
        say(e && e.name === "NotAllowedError" ? "Cancelled or timed out." : (e && e.message) || "That didn't work.");
      }).then(function () { btn.disabled = false; });
    });
    // The identifier step (#809): the passkey prompt opens on its own; "Use another way" sits beneath it.
    if (btn.hasAttribute("data-auto")) setTimeout(function () { btn.click(); }, 0);
  });
})();
