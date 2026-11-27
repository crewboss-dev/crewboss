// Recovery codes page helpers (#809): copy, download as recovery-codes.txt, and the "I've saved them" tick that enables Continue.
(function () {
  var pre = document.querySelector("[data-codes]");
  if (!pre) return;
  var text = function () { return pre.textContent.trim() + "\n"; };
  var copy = document.querySelector("[data-codes-copy]"), dl = document.querySelector("[data-codes-download]");
  if (copy && navigator.clipboard) {
    copy.hidden = false;
    copy.addEventListener("click", function () { navigator.clipboard.writeText(text()).then(function () { copy.textContent = "Copied"; }); });
  }
  if (dl && window.Blob && window.URL) {
    dl.hidden = false;
    dl.addEventListener("click", function () {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob(["Crewboss recovery codes. Each works once.\n\n" + text()], { type: "text/plain" }));
      a.download = "recovery-codes.txt";
      document.body.appendChild(a); a.click(); a.remove();
    });
  }
  var tick = document.querySelector("[data-codes-saved]"), go = document.querySelector("[data-codes-continue]");
  if (tick && go) {
    go.disabled = !tick.checked;
    tick.addEventListener("change", function () { go.disabled = !tick.checked; });
  }
})();
