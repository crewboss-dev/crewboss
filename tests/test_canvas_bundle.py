"""The shared canvas bundle (#393, docs/canvas-contract.md): the committed files, CSP-safety of the build, and a
headless-browser smoke that mounts BOTH kinds from JSON under `script-src 'self'; style-src 'self'` with no console
error and no CSP violation. The browser part needs a Chromium (CREWBOSS_CHROMIUM, `chromium` on PATH, or Playwright's
cached headless shell) and skips without one."""
import glob
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CANVAS = ROOT / "app/static/canvas"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'none'")


def test_bundle_files_are_committed_and_versioned():
    for name in ("mallen-canvas.js", "mallen-canvas.css", "VERSION"):
        assert (CANVAS / name).is_file(), name
    version = (CANVAS / "VERSION").read_text()
    assert re.search(r"^contract=1$", version, re.M)
    assert re.search(r"^source=[0-9a-f]{7,40}(-dirty)?$|^source=unknown$", version, re.M)


def test_bundle_is_csp_safe():
    js = (CANVAS / "mallen-canvas.js").read_text()
    for bad in ("eval(", "new Function", 'createElement("style")', "createElement('style')", "insertRule(",
                "adoptedStyleSheets", 'setAttribute("style"', "cdn.", "https://unpkg", "googleapis"):
        assert bad not in js, bad
    css = (CANVAS / "mallen-canvas.css").read_text()
    # React Flow's own stylesheet lives in the .css file (never injected at runtime) ...
    assert ".react-flow__viewport" in css and ".react-flow__pane" in css
    # ... and every rule of ours is scoped under .mallen-canvas (no Tailwind/host classes assumed)
    ours = css[css.index(".mallen-canvas{"):]
    for selector in re.findall(r"(?:^|})([^{}@]+){", ours):
        for part in selector.split(","):
            part = part.strip()
            if part and not part.startswith(("from", "to", "0%", "100%")):
                assert ".mallen-canvas" in part, part


def _chromium() -> str | None:
    for c in (os.environ.get("CREWBOSS_CHROMIUM"), shutil.which("chromium"), shutil.which("chromium-browser"),
              shutil.which("google-chrome"),
              *sorted(glob.glob(os.path.expanduser(
                  "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))):
        if c and os.access(c, os.X_OK):
            return c
    return None


PROBE = """
(function () {
  var out = [];
  function log(s) { out.push(s); var p = document.getElementById('probe'); if (p) p.textContent = out.join('\\n'); }
  document.addEventListener('securitypolicyviolation', function (e) { log('CSP ' + e.violatedDirective + ' ' + e.blockedURI); });
  window.addEventListener('error', function (e) { log('ERROR ' + e.message); });
  var ce = console.error; console.error = function () { log('CONSOLE ' + Array.prototype.join.call(arguments, ' ')); ce.apply(console, arguments); };
})();
"""


def _page(workflow_json: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>canvas smoke</title>
<link rel="stylesheet" href="/canvas/mallen-canvas.css"><script src="/probe.js"></script></head><body>
<pre id="probe"></pre>
<div class="mallen-canvas" id="wf" data-kind="workflow" data-mode="view" data-open="fetch"></div>
<script type="application/json" data-canvas-for="wf">{workflow_json}</script>
<div class="mallen-canvas" id="int" data-kind="integrations" data-mode="view" data-src="/integrations.json"
     data-integration-link="/admin/integrations#i-{{id}}">
  <template data-mc-integration-extra="zabbix-webhook"><p class="host-extra">Replace key here</p></template>
</div>
<script src="/canvas/mallen-canvas.js"></script></body></html>"""


@pytest.mark.skipif(_chromium() is None, reason="no headless Chromium on this box")
def test_bundle_mounts_both_kinds_under_strict_csp(tmp_path):
    catalog = json.loads((ROOT / "fixtures/catalog-decisions-fixture.json").read_text())
    integrations = json.loads((ROOT / "fixtures/integrations-fixture.json").read_text())
    wf = next(w for w in catalog["workflows"] if w["id"] == "email.triage.route")
    # json in a <script type="application/json">: escape "<" so no "</script>" can end it early
    wf_json = json.dumps({"workflow": wf, "integrations": integrations}).replace("<", "\\u003c")
    files = {
        "/smoke.html": ("text/html", _page(wf_json)),
        "/probe.js": ("text/javascript", PROBE),
        "/integrations.json": ("application/json", json.dumps({"integrations": integrations})),
        "/canvas/mallen-canvas.js": ("text/javascript", (CANVAS / "mallen-canvas.js").read_text()),
        "/canvas/mallen-canvas.css": ("text/css", (CANVAS / "mallen-canvas.css").read_text()),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            ctype, body = files.get(self.path.split("?")[0], ("text/plain", None))
            data = (body or "not found").encode()
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/smoke.html?open=zabbix-webhook"
        out = subprocess.run([_chromium(), "--headless", "--no-sandbox", "--disable-gpu", "--window-size=1280,1600",
                              f"--user-data-dir={tmp_path}", "--virtual-time-budget=10000", "--dump-dom", url],
                             capture_output=True, text=True, timeout=90)
    finally:
        server.shutdown()
    dom = out.stdout
    assert dom, out.stderr[-2000:]
    probe = re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S).group(1)
    assert probe.strip() == "", probe            # no CSP violation, no error, no console.error
    assert dom.count('data-mc-state="ready"') == 2
    # workflow: cards, a condition pill, and the opened step's panel with its integration block
    assert "react-flow__node-card" in dom and "react-flow__node-pill" in dom
    assert "Integrations (2)" in dom and "Send mail (Graph)" in dom and "SharePoint documents" in dom
    # integrations: hub + systems, ?open= reopened the Zabbix panel, the host's template landed in it
    assert "react-flow__node-hub" in dom and "react-flow__node-system" in dom
    assert "Zabbix alert webhook" in dom and "host-extra" in dom
    assert 'href="/admin/integrations#i-zabbix-webhook"' in dom
    # admin default: nothing about tasks
    assert "mc-task-badge" not in dom and "mc-plus" not in dom
