#!/usr/bin/env python3
"""Throwaway deployment test (task #589, change #85). Stdlib only, Python 3.10+. Run it as `./crewboss deploy-test`.

    ./crewboss deploy-test [run] [--mode localhost|lan] [--ref REF] [--keep [MINUTES]] [--lan-ip IP]   install a fresh copy, check it, print the LAN URL, delete it
    ./crewboss deploy-test down ID                                               tear a kept trial down now
    ./crewboss deploy-test list                                                  trials still on this machine

`--mode lan` (#976) installs in LAN mode on free HIGH ports (never the live 80/443), checks https with the self-signed certificate (the
fingerprint must match frontdoor/front_door.json), signs the owner in through a browser that accepts that certificate, and enrols a
runner with a pinned code until it is ready (`--no-runner` skips that; DEPLOY_TEST_RUNNER_PYTHON names a python that has the runner's
dependencies, otherwise a venv is made and the runner installed from the snapshot).

What it does: `git archive REF` into a new temp folder (only tracked files: exactly what ships), then the ordinary outsider
install of that snapshot (`./crewboss install --yes --mode localhost`) with its own instance folder, its own compose project
`crewboss-trial-<id>`, a free port and fresh secrets, an empty database. It then checks the install over HTTP and in a browser, prints
a LAN address for the owner to open (with --keep), and deletes everything of the trial (containers, volumes, network, image, folder).

Safety (docs/deploy-test.md): it removes COMPOSE_*, CREWBOSS_*, POSTGRES_* and APP_PORT from its own environment; refuses any
project name that is not `crewboss-trial-<14 digits>`; refuses any folder that is, contains or sits inside a live path; and tears
down only by the trial's own compose project label.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import re
import shutil
import hashlib
import json
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREFIX = "crewboss-trial-"
ID_RE = re.compile(r"\d{18}")                      # YYYYmmddHHMMSS + 4 random digits (two runs in one second differ)
TRIAL_RE = re.compile(r"crewboss-trial-\d{18}")
# The caller's own environment, taken before main() strips it: live_paths() must see what their shell pointed CREWBOSS_* at.
CALLER_ENV = dict(os.environ)
STRIP_PREFIXES = ("COMPOSE_", "CREWBOSS_", "POSTGRES_", "APP_PORT")
DEFAULT_KEEP_MINUTES = 60
PW_RE = re.compile(r"temporary password[^\n]*?:\s*(\S+)", re.I)


class Fail(SystemExit):
    def __init__(self, message: str, code: int = 1):
        super().__init__(code)
        self.message = message


def say(msg: str = "") -> None:
    print(msg, flush=True)


# --- safety rules (tested without docker) -----------------------------------------------------------------------------------

def live_paths(environ: dict | None = None, home: Path | None = None) -> list[Path]:
    """Places a trial must never be, contain, or sit inside: the default instance folders any install would use, and whatever
    the caller's shell points CREWBOSS_* at. `docker_live_paths` adds the folders of every compose project running here."""
    home = home or Path.home()
    paths = [home / ".crewboss", home / ".config" / "crewboss", home / ".local" / "share" / "crewboss"]
    env = CALLER_ENV if environ is None else environ
    for k in ("CREWBOSS_HOME", "CREWBOSS_SECRETS_DIR", "CREWBOSS_MATERIALS_HOST_DIR"):
        if env.get(k):
            paths.append(Path(env[k]).expanduser())
    return paths


def docker_live_paths() -> list[Path]:
    """The checkout folder and the folders mounted into containers of every compose project already on this machine, so a
    live install is protected wherever it lives (nothing about it has to be known here)."""
    fmt = ('{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
           '{{range .Mounts}}|{{if eq .Type "bind"}}{{.Source}}{{end}}{{end}}')
    ids = run(["docker", "ps", "-a", "-q"], check=False, capture=True).stdout.split()
    out = run(["docker", "inspect", "-f", fmt, *ids], check=False, capture=True).stdout if ids else ""
    root = Path(os.path.realpath(tempfile.gettempdir()))
    found = []
    for item in {x for ln in out.splitlines() for x in ln.split("|") if x.startswith("/")}:
        p = Path(os.path.realpath(item))
        if p != Path("/") and p != root and p not in root.parents and root not in p.parents:     # trial siblings under temp are not "live"
            found.append(p)
    return found


def clean_env(environ: dict | None = None) -> dict:
    env = dict(os.environ if environ is None else environ)
    for k in list(env):
        if k.startswith(STRIP_PREFIXES):
            del env[k]
    return env


def check_project(project: str) -> None:
    if project == "crewboss" or not TRIAL_RE.fullmatch(project):
        raise Fail(f"refusing project name {project!r}: a trial is always {PREFIX}<18-digit id>, never the live `crewboss`.")


def check_paths(*paths: Path, live: list[Path] | None = None, root: Path | None = None) -> None:
    """Every path must be under the temp root and unrelated to any live path (not equal, not a parent, not a child)."""
    live = [Path(os.path.realpath(p)) for p in ((live_paths() + docker_live_paths()) if live is None else live)]
    root = Path(os.path.realpath(root or tempfile.gettempdir()))
    for p in paths:
        r = Path(os.path.realpath(p))
        if root not in r.parents:
            raise Fail(f"refusing {p}: a trial lives only under {root}.")
        for lp in live:
            if r == lp or lp in r.parents or r in lp.parents:
                raise Fail(f"refusing {p}: it overlaps the live path {lp}.")


def trial_dir(trial_id: str) -> Path:
    if not ID_RE.fullmatch(trial_id):
        raise Fail(f"{trial_id!r} is not a trial id (see `./crewboss deploy-test list`).")
    return Path(tempfile.gettempdir()) / f"{PREFIX}{trial_id}"


def lan_ip(override: str | None = None, hostname_i: str | None = None) -> str | None:
    """The first private IPv4 address of this box, from `hostname -I` (never hard-coded); docker bridges (x.x.x.1) are skipped."""
    if override:
        return override
    out = hostname_i if hostname_i is not None else (run(["hostname", "-I"], check=False, capture=True).stdout or "")
    for a in out.split():
        p = a.split(".")
        if len(p) != 4 or not all(x.isdigit() for x in p):
            continue
        private = a.startswith("10.") or a.startswith("192.168.") or (p[0] == "172" and 16 <= int(p[1]) <= 31)
        if private and p[3] != "1":
            return a
    return None


# --- plumbing ---------------------------------------------------------------------------------------------------------------

def run(cmd: list[str], *, check: bool = True, capture: bool = False, cwd: Path | None = None,
        timeout: int | None = None) -> subprocess.CompletedProcess:
    """The one place a process starts (tests replace it). Always the cleaned environment."""
    res = subprocess.run([str(c) for c in cmd], text=True, capture_output=capture, cwd=cwd, env=clean_env(), timeout=timeout)
    if check and res.returncode != 0:
        raise Fail(f"command failed ({res.returncode}): {' '.join(map(str, cmd[:6]))}\n{((res.stderr or '') + (res.stdout or '')).strip()[-1200:]}")
    return res


def snapshot(ref: str, dest: Path) -> str:
    """The tracked files of `ref`, exactly as shipped, into dest. Returns the commit sha."""
    sha = run(["git", "-C", REPO, "rev-parse", "--verify", ref + "^{commit}"], capture=True).stdout.strip()
    dest.mkdir(parents=True, exist_ok=True)
    arc = subprocess.Popen(["git", "-C", str(REPO), "archive", "--format=tar", sha], stdout=subprocess.PIPE, env=clean_env())
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=arc.stdout, env=clean_env())
    arc.stdout.close()
    if arc.wait() != 0 or tar.returncode != 0:
        raise Fail("git archive / tar failed")
    # The installer insists on a git checkout (a clone has one). A bare `git init` gives the snapshot that: no history, no remote.
    run(["git", "init", "-q"], cwd=dest)
    return sha


def project_resources(project: str) -> dict:
    """Everything docker has under this trial's compose label, nothing else."""
    lab = f"label=com.docker.compose.project={project}"

    def ids(*cmd):
        return run(["docker", *cmd, "--filter", lab, "-q"], check=False, capture=True).stdout.split()
    return {"containers": ids("ps", "-a"), "volumes": ids("volume", "ls"), "networks": ids("network", "ls")}


def teardown(trial_id: str) -> None:
    d = trial_dir(trial_id)
    project = PREFIX + trial_id
    check_project(project)
    check_paths(d)
    snap, home = d / "src", d / "home"
    if (home / "compose.env").is_file() and (snap / "crewboss").is_file():
        run([sys.executable, snap / "crewboss", "down", "--home", home, "--project", project,
             "--delete-data", "--confirm-project", project], check=False, capture=True)
    # Belt and braces, still only this project's label.
    res = project_resources(project)
    if res["containers"]:
        run(["docker", "rm", "-f", *res["containers"]], check=False, capture=True)
    res = project_resources(project)
    if res["volumes"]:
        run(["docker", "volume", "rm", "-f", *res["volumes"]], check=False, capture=True)
    if res["networks"]:
        run(["docker", "network", "rm", *res["networks"]], check=False, capture=True)
    run(["docker", "image", "rm", f"{project}-app"], check=False, capture=True)      # the image compose built for this project
    shutil.rmtree(d, ignore_errors=True)
    left = project_resources(project)
    if any(left.values()) or d.exists():
        raise Fail(f"teardown of {project} left something behind: {left}, folder exists: {d.exists()}")
    say(f"Deleted trial {trial_id}: containers, volumes, network, image and {d}.")


def http(url: str, timeout: float = 10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        say(f"  [{status}] {name}" + (f": {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return bool(self.rows) and all(s in ("PASS", "SKIP") for _, s, _ in self.rows)


def http_checks(base: str, rep: Report) -> None:
    code, _ = http(base + "/healthz")
    rep.add("healthz answers", "PASS" if code == 200 else "FAIL", f"HTTP {code}")
    code, body = http(base + "/auth/login")
    rep.add("sign-in page loads", "PASS" if code == 200 and "Email or username" in body else "FAIL", f"HTTP {code}")
    rep.add("owner account exists (page does not say 'no owner')", "PASS" if "data-no-owner" not in body else "FAIL")


def browser_python(tmp: Path) -> str | None:
    """A python with Playwright: $DEPLOY_TEST_PYTHON, else this one, else a venv inside the trial folder."""
    cand = os.environ.get("DEPLOY_TEST_PYTHON") or sys.executable
    if run([cand, "-c", "import playwright.sync_api"], check=False, capture=True).returncode == 0:
        return cand
    say("  no Playwright for this Python: making a small venv for it (needs network) ...")
    venv = tmp / "pwvenv"
    if run([sys.executable, "-m", "venv", venv], check=False, capture=True).returncode != 0:
        return None
    py = str(venv / "bin" / "python")
    if run([py, "-m", "pip", "install", "-q", "playwright"], check=False, capture=True).returncode != 0:
        return None
    return py


def browser_checks(base: str, username: str, password: str, tmp: Path, rep: Report) -> None:
    py = browser_python(tmp)
    if not py:
        rep.add("browser checks", "SKIP", "no Playwright available (pip install playwright, then re-run)")
        return
    res = run([py, REPO / "scripts" / "deploy_test_browser.py", base, username, password], check=False, capture=True, timeout=300)
    seen = False
    for ln in (res.stdout or "").splitlines():
        m = re.match(r"^(PASS|FAIL|SKIP) (.+?)(?:: (.*))?$", ln)
        if m:
            seen = True
            rep.add(m.group(2), m.group(1), m.group(3) or "")
    if not seen:
        rep.add("browser checks", "FAIL", ((res.stderr or "") + (res.stdout or "")).strip()[-600:])


def expose_on_lan(project: str, home: Path, snap: Path, lan: str, rep: Report) -> str | None:
    """Add a second published port on the LAN address only (the installer's own binding stays 127.0.0.1)."""
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        lport = s.getsockname()[1]
    (home / "compose.override.yml").write_text(f'services:\n  app:\n    ports:\n      - "{lan}:{lport}:8000"\n')
    res = run([sys.executable, snap / "crewboss", "up", "--home", home, "--project", project], check=False, capture=True)
    url = f"http://{lan}:{lport}"
    for _ in range(40):
        if http(url + "/healthz", timeout=3)[0] == 200:
            rep.add("reachable on the LAN address", "PASS", url)
            return url
        time.sleep(1)
    rep.add("reachable on the LAN address", "FAIL", (res.stderr or url)[-300:])
    return None


def reset_owner_password(snap: Path, home: Path, project: str) -> str:
    res = run([sys.executable, snap / "crewboss", "owner", "--reset", "--home", home, "--project", project], check=False, capture=True)
    m = PW_RE.search(res.stdout or "")
    return m.group(1) if m else "(run ./crewboss owner --reset --home <folder>)"


def free_ports(n: int) -> list[int]:
    """n distinct free high ports on all interfaces, never 80, 443 or the 8931-8940 range the live install uses."""
    got: list[int] = []
    socks = []
    try:
        while len(got) < n:
            sk = socket.socket()
            sk.bind(("0.0.0.0", 0))
            port = sk.getsockname()[1]
            if port < 20000 or 8931 <= port <= 8940 or port in (80, 443):
                sk.close()
                continue
            socks.append(sk)
            got.append(port)
    finally:
        for sk in socks:
            sk.close()
    return got


def served_fingerprint(host: str, port: int) -> str:
    """SHA-256 of the certificate the server presents, as colon hex (certificate checking OFF: this is the very certificate we compare)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=10) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
        der = tls.getpeercert(binary_form=True)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def lan_checks(home: Path, host: str, https_port: int, rep: Report) -> str:
    """https answers with the self-signed certificate, and the certificate is the one front_door.json records. Returns the https base URL."""
    base = f"https://{host}:{https_port}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # curl -k
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=15, context=ctx) as r:
            rep.add("https answers (certificate not checked, like curl -k)", "PASS" if r.status == 200 else "FAIL", f"HTTP {r.status}")
    except (urllib.error.URLError, OSError) as exc:
        rep.add("https answers (certificate not checked, like curl -k)", "FAIL", str(exc)[:200])
    strict = ssl.create_default_context()
    try:
        urllib.request.urlopen(base + "/healthz", timeout=15, context=strict)
        rep.add("browsers without the click-through are refused (self-signed)", "FAIL", "the certificate was trusted")
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        rep.add("browsers without the click-through are refused (self-signed)", "PASS" if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "self" in str(exc).lower() else "FAIL", str(exc)[:80])
    try:
        want = json.loads((home / "frontdoor" / "front_door.json").read_text())["lan"]["cert_sha256"].upper()
        got = served_fingerprint(host, https_port)
        rep.add("certificate fingerprint matches front_door.json", "PASS" if got == want else "FAIL", got[:17] + "...")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        rep.add("certificate fingerprint matches front_door.json", "FAIL", f"{type(exc).__name__}: {exc}"[:200])
    return base


RUNNER_MAIN = "import sys; from crewboss_runner.cli import main; sys.exit(main())"


def runner_checks(code_file: Path, snap: Path, tmp: Path, rep: Report) -> None:
    """A runner in its own throwaway home enrols with the pinned LAN code and reaches `ready`."""
    if not code_file.is_file():
        rep.add("runner enrols with a pinned LAN code", "SKIP", "no enrolment code was made")
        return
    py = os.environ.get("DEPLOY_TEST_RUNNER_PYTHON")
    if not py:
        venv = tmp / "runnervenv"
        if run([sys.executable, "-m", "venv", venv], check=False, capture=True).returncode != 0 or \
                run([venv / "bin" / "pip", "install", "-q", snap / "runner"], check=False, capture=True, timeout=600).returncode != 0:
            rep.add("runner enrols with a pinned LAN code", "SKIP", "could not make a venv with the runner (needs network, or set DEPLOY_TEST_RUNNER_PYTHON)")
            return
        py = str(venv / "bin" / "python")
    rhome = tmp / "runnerhome"
    rhome.mkdir(exist_ok=True)
    env = clean_env()
    env.update({"HOME": str(rhome), "XDG_CONFIG_HOME": str(rhome / "c"), "XDG_STATE_HOME": str(rhome / "s"), "XDG_DATA_HOME": str(rhome / "d"),
                "PYTHONPATH": str(snap / "runner") if os.environ.get("DEPLOY_TEST_RUNNER_PYTHON") else ""})
    code = code_file.read_text().strip()
    rep.add("enrolment code is a pinned LAN code", "PASS" if re.search(r"#[a-z2-7]{8}\.[a-z2-7]{26}-", code) else "FAIL")
    res = subprocess.run([py, "-c", RUNNER_MAIN, "enrol", code, "--name", "trial-runner"], text=True, capture_output=True, env=env, timeout=120)
    rep.add("runner enrols with the pinned code", "PASS" if res.returncode == 0 else "FAIL", ((res.stdout or "") + (res.stderr or "")).strip()[-200:])
    if res.returncode != 0:
        return
    proc = subprocess.Popen([py, "-c", RUNNER_MAIN, "run"], text=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state, detail = "", ""
    try:
        for _ in range(60):
            time.sleep(2)
            st = subprocess.run([py, "-c", RUNNER_MAIN, "status"], text=True, capture_output=True, env=env, timeout=30).stdout
            m = re.search(r"^runner:\s+(\S+)", st, re.M)
            state = m.group(1) if m else ""
            detail = st
            if state == "ready":
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    pinned = re.search(r"^tls:\s+pinned", detail, re.M) is not None
    rep.add("runner is pinned to the certificate (status: tls pinned)", "PASS" if pinned else "FAIL")
    rep.add("runner reaches ready", "PASS" if state == "ready" else "FAIL", f"state {state or 'unknown'}")


# --- commands ---------------------------------------------------------------------------------------------------------------

def trial_port() -> int:
    """A free ephemeral loopback port, never in the 8931-8940 range the live install and its neighbours use."""
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if not 8931 <= port <= 8940:
            return port


def keep_failure_files(d: Path, trial_id: str, rep: "Report") -> None:
    """On a failed run only, keep the browser's screenshot and page and the installer log beside the trial (it is deleted)."""
    if rep.ok or not d.exists():
        return
    files = [f for f in list(d.glob("deploy-test-failure.*")) + [d / "install.log"] if f.is_file()]
    if files:
        out = Path(tempfile.gettempdir()) / f"{PREFIX}{trial_id}-failure"
        out.mkdir(mode=0o700, exist_ok=True)
        os.chmod(out, 0o700)
        for f in files:
            target = out / f.name
            if f.name == "install.log":               # holds the trial's one-time password: scrub it
                target.write_text(scrub_password(f.read_text(errors="replace")))
            else:
                shutil.copy2(f, target)
            os.chmod(target, 0o600)
        say(f"Failure files kept in {out} (delete with: ./crewboss deploy-test down {trial_id}-failure, or `clean`)")


def scrub_password(text: str) -> str:
    return PW_RE.sub(lambda m: m.group(0).replace(m.group(1), "[removed]"), text)


def remove_failure_folder(name: str) -> None:
    trial_id = name[:-len("-failure")]
    if not ID_RE.fullmatch(trial_id):
        raise Fail(f"{name!r} is not a failure folder id.")
    d = Path(tempfile.gettempdir()) / f"{PREFIX}{name}"
    check_paths(d)
    shutil.rmtree(d, ignore_errors=True)
    say(f"Deleted {d}.")


def cmd_run(args) -> int:
    if run(["docker", "info"], check=False, capture=True).returncode != 0:
        raise Fail("the docker daemon is not reachable.")
    trial_id = dt.datetime.now().strftime("%Y%m%d%H%M%S") + f"{random.randrange(10000):04d}"
    project = PREFIX + trial_id
    check_project(project)
    d = trial_dir(trial_id)
    snap, home = d / "src", d / "home"
    check_paths(d, snap, home)
    try:
        d.mkdir()                       # exclusive: an id that already has a folder is refused BEFORE anything can delete it
    except OSError as exc:
        raise Fail(f"cannot make the trial folder {d} ({exc}); another run may own it. Nothing was touched.")
    os.environ["DEPLOY_TEST_SHOTS"] = str(d)
    t0 = time.time()
    rep = Report()
    try:
        say(f"Trial {trial_id}: project {project}, folder {d}")
        sha = snapshot(args.ref, snap)
        say(f"Snapshot of {args.ref} ({sha[:7]}) in {snap}")
        say("Installing exactly as an outsider would (the first build takes a few minutes) ...")
        t1 = time.time()
        lan_mode = args.mode == "lan"
        install = [sys.executable, snap / "crewboss", "install", "--yes", "--mode", args.mode, "--home", home,
                   "--project", project, "--port", str(trial_port()), "--name", "Crewboss trial", "--owner-username", "owner"]
        if lan_mode:
            lan = lan_ip(args.lan_ip)
            if not lan:
                raise Fail("--mode lan needs this computer's private network address, and `hostname -I` shows none. Pass --lan-ip.")
            http_port, https_port = free_ports(2)
            install += ["--lan-host", lan, "--http-port", str(http_port), "--https-port", str(https_port)]
            say(f"LAN mode on {lan}: http port {http_port}, https port {https_port} (never 80/443)")
        res = run(install, check=False, capture=True)
        out = (res.stdout or "") + (res.stderr or "")
        (d / "install.log").write_text(out)
        if args.log:
            Path(args.log).write_text(out)
        secs = time.time() - t1
        if res.returncode != 0:
            say(out[-1500:])
            rep.add("outsider install", "FAIL", f"exit {res.returncode} after {secs:.0f}s")
        else:
            rep.add("outsider install", "PASS", f"{secs:.0f}s")
            pw = PW_RE.search(out)
            env = dict(ln.split("=", 1) for ln in (home / "compose.env").read_text().splitlines() if "=" in ln and not ln.startswith("#"))
            base = f"http://localhost:{env['APP_PORT']}"
            rep.add("compose.env names the trial's own image (not derived from the shell)", "PASS" if env.get("CREWBOSS_IMAGE") == f"{project}-app" else "FAIL", env.get("CREWBOSS_IMAGE", "missing"))
            code_file = d / "enrol-code.txt"
            if lan_mode:
                base = lan_checks(home, lan, https_port, rep)
                os.environ["DEPLOY_TEST_IGNORE_HTTPS"] = "1"
                if not args.no_runner:
                    os.environ["DEPLOY_TEST_CODE_OUT"] = str(code_file)
            else:
                http_checks(base, rep)
            if pw:
                browser_checks(base, "owner", pw.group(1), d, rep)
                if lan_mode and not args.no_runner:
                    runner_checks(code_file, snap, d, rep)
            else:
                rep.add("temporary password in the installer output", "FAIL", "could not find it; see install.log")
            # The first-run tour walk-through (#938) lives in scripts/deploy_test_browser.py as
            # `tour_walkthrough(page, base)` (it reports its own PASS/FAIL rows and makes the trial project); nothing to change here.
            if args.keep is not None and rep.ok:
                lan = lan_ip(args.lan_ip)
                url = expose_on_lan(project, home, snap, lan, rep) if lan else None
                if not lan:
                    say("  (no private LAN address in `hostname -I`; the ssh tunnel below does not need one)")
                new_pw = reset_owner_password(snap, home, project)
                ap = env["APP_PORT"]
                say(f"\nChecks passed in {time.time() - t0:.0f}s.\n\nTo open it from another computer (this is the way that works, passkeys included):\n"
                    f"    ssh -L {ap}:localhost:{ap} <this machine>      then browse to  http://localhost:{ap}\n"
                    f"On this machine just open  http://localhost:{ap}\n"
                    f"Sign in as  owner  with the temporary password:  {new_pw}\n"
                    + (f"\nAlso published on the LAN: {url}  (shows the sign-in page only: the app accepts a browser's writes only from its own\n"
                       f"address and offers no sign-in over plain http).\n" if url else "")
                    + f"It stays up for {args.keep} min, or until you run:  ./crewboss deploy-test down {trial_id}   (Ctrl-C deletes it too)")
                try:
                    time.sleep(args.keep * 60)
                except KeyboardInterrupt:
                    say("\ninterrupted: deleting it now")
        say("\nChecks: " + ("ALL PASSED" if rep.ok else "FAILURES") + f"   (total {time.time() - t0:.0f}s)")
    finally:
        keep_failure_files(d, trial_id, rep)
        teardown(trial_id)
    return 0 if rep.ok else 1


def cmd_down(args) -> int:
    if args.id.endswith("-failure"):
        remove_failure_folder(args.id)
    else:
        teardown(args.id)
    return 0


def cmd_clean(args) -> int:
    """Remove every kept failure folder (never a running trial)."""
    found = sorted(Path(tempfile.gettempdir()).glob(PREFIX + "*-failure"))
    for p in found:
        remove_failure_folder(p.name[len(PREFIX):])
    if not found:
        say("no failure folders to remove")
    return 0


def cmd_list(args) -> int:
    found = sorted(Path(tempfile.gettempdir()).glob(PREFIX + "*"))
    for p in found:
        say(f"{p.name[len(PREFIX):]}  {p}")
    if not found:
        say("no trials on this machine")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crewboss deploy-test", description="Install a fresh copy from the exact snapshot, check it, delete it.")
    sub = p.add_subparsers(dest="command")

    def add_run_flags(sp):
        sp.add_argument("--ref", default="origin/main", help="what to test: a branch, tag or sha of this checkout (default origin/main)")
        sp.add_argument("--keep", nargs="?", type=int, const=DEFAULT_KEEP_MINUTES, default=None, metavar="MINUTES",
                        help=f"after the checks keep it up on the LAN for MINUTES (default {DEFAULT_KEEP_MINUTES}), then delete it")
        sp.add_argument("--log", metavar="FILE", help="also save the installer's output here (the trial's own copy is deleted with it)")
        sp.add_argument("--mode", choices=("localhost", "lan"), default="localhost", help="localhost (default) or lan: LAN mode on free high ports, https with the self-signed certificate, a pinned runner")
        sp.add_argument("--no-runner", action="store_true", help="lan mode: skip the runner enrolment step")
        sp.add_argument("--lan-ip", help="the address to publish on (default: the first private one in `hostname -I`)")
        sp.set_defaults(func=cmd_run)
    add_run_flags(p)
    add_run_flags(sub.add_parser("run", help="install, check, (keep), delete (the default)"))
    d = sub.add_parser("down", help="delete a trial now")
    d.add_argument("id")
    d.set_defaults(func=cmd_down)
    sub.add_parser("list", help="trials on this machine").set_defaults(func=cmd_list)
    sub.add_parser("clean", help="delete the failure folders kept after failed runs").set_defaults(func=cmd_clean)
    return p


def main(argv: list[str] | None = None) -> int:
    global CALLER_ENV
    CALLER_ENV = dict(os.environ)       # what the shell pointed CREWBOSS_* at: the live-path guard still reads it
    for k in list(os.environ):          # nothing from the caller's shell may redirect compose, even before the first call
        if k.startswith(STRIP_PREFIXES):
            del os.environ[k]
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except OSError as exc:
        print(f"deploy-test: {exc}", file=sys.stderr)
        return 1
    except Fail as exc:
        print(f"deploy-test: {exc.message}", file=sys.stderr)
        return exc.code if isinstance(exc.code, int) and exc.code else 1
    except KeyboardInterrupt:
        print("\ndeploy-test: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
