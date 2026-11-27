"""A project's ENVIRONMENT (#810 step 6, migration 0091): what a machine needs installed to build it, and whether a given runner has it.

Owner (11-16): "When moving a project to a runner its first step should be to check the environment for the software on each move. If Android
Studio and Java is needed for example. Each project should list environment requirements. Maybe a tab just for that. So if its a Windows app,
list what windows needs installed. And when changing to that runner, Crewboss does a check on that runner and marks compliant or not."

* Requirements (rows on the project's Environment tab): kind, name, minimum version, OS (any / linux / windows / macos), an install hint.
  "Suggest from the repo" proposes rows from the project's files when the repo is on this server (Gradle/AGP, package.json engines,
  pyproject requires-python, *.csproj). Rows are editable.
* The kinds are a FIXED catalogue (KINDS) that the RUNNER implements as built-in probes (runner/crewboss_runner/envcheck.py). SECURITY: this
  server sends the runner only (kind, name, args); it never sends a command string, the runner maps the kind to its own code, and an unknown
  kind comes back "unsupported". Names are validated here (and again by the runner) against safe patterns before they are stored or sent.
  A keystore row holds only its public SHA-256 fingerprint; the key never reaches Crewboss.
* A check is a lightweight runner JOB, no Claude agent: queued here, claimed by the runner over its verified socket (`env_claim`), finished with
  one short status per row (`env_finish`). It is requested whenever a person's "Runs on" switches to a runner, and by "Check again". The latest
  finished check per (project, runner) feeds the badges (Compliant / Missing N / Not checked). Non-compliant is a WARNING, never a block."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app import config, repo, tree
from app.db import get_cursor
from app.repo import BadRequest, NotFound

log = logging.getLogger("crewboss.environment")

KINDS = ("tool", "env", "android_sdk_pkg", "file", "keystore")
KIND_LABELS = {"tool": "Tool on PATH", "env": "Environment variable", "android_sdk_pkg": "Android SDK package", "file": "File or folder",
               "keystore": "Signing keystore"}
OSES = ("any", "linux", "windows", "macos")
STATUSES = ("ok", "missing", "old", "mismatch", "error", "unsupported", "na")
STATUS_WORDS = {"missing": "not found", "old": "too old", "mismatch": "fingerprint does not match", "error": "could not be checked",
                "unsupported": "this runner is too old to check it", "ok": "ok", "na": "not needed on this OS"}
MAX_ROWS = 30
CLAIM_AGAIN_AFTER = "2 minutes"          # a check a runner claimed and never finished is offered again

# The same shapes the runner checks (runner/crewboss_runner/envcheck.py): a name is a KEY, never a command.
TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9;._-]{0,79}$")
PATH_RE = re.compile(r"^[~/A-Za-z0-9._:\\()+][~/A-Za-z0-9._:\\ ()+-]{0,239}$")
NETWORK_PATH = re.compile(r"^[\\/]{2}")       # //host/share, \\host\share, \\?\, \\.\ : a Windows runner touching one would leak its credentials
WINDOWS_PATH = re.compile(r"^(~([\\/]|$)|[A-Za-z]:[\\/])")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")
FINGERPRINT_RE = re.compile(r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")

# The tools a requirement may name: the same list the runner implements (runner/crewboss_runner/envcheck.py TOOLS). A name outside it would only
# ever come back "unsupported", so it is refused here with the list.
TOOLS = ("java", "javac", "node", "npm", "python3", "python", "py", "pip3", "dotnet", "git", "gradle", "adb", "kotlin", "flutter", "go",
         "cargo", "rustc", "swift", "xcodebuild", "wix", "msbuild")

# What to tell a person when something is missing, per OS, when the row has no hint of its own.
HINTS = {
    ("tool", "java"): {"linux": "sudo apt install openjdk-17-jdk (or your distro's JDK package)", "macos": "brew install openjdk@17",
                       "windows": "winget install Microsoft.OpenJDK.17"},
    ("tool", "node"): {"linux": "Install Node.js from nodejs.org or with your package manager", "macos": "brew install node",
                       "windows": "winget install OpenJS.NodeJS.LTS"},
    ("tool", "python3"): {"linux": "sudo apt install python3", "macos": "brew install python", "windows": "winget install Python.Python.3.12"},
    ("tool", "dotnet"): {"linux": "Install the .NET SDK (learn.microsoft.com/dotnet/core/install/linux)", "macos": "brew install --cask dotnet-sdk",
                         "windows": "winget install Microsoft.DotNet.SDK.8"},
    ("tool", "git"): {"linux": "sudo apt install git", "macos": "xcode-select --install", "windows": "winget install Git.Git"},
    ("env", "ANDROID_HOME"): {"any": "Install Android Studio (or the command-line tools) and set ANDROID_HOME to the SDK folder"},
}
GENERIC_HINT = {"android_sdk_pkg": "In Android Studio's SDK Manager (or `sdkmanager \"{name}\"`) install {name}",
                "keystore": "Put the signing keystore at {name} (its fingerprint is checked, the key is never read by Crewboss)",
                "file": "Create or copy {name} onto this machine", "env": "Set the {name} environment variable",
                "tool": "Install {name} and make sure it is on PATH"}


def hint_for(row: dict, runner_os: str) -> str:
    if row.get("install_hint"):
        return row["install_hint"]
    table = HINTS.get((row["kind"], row["name"]), {})
    return table.get(runner_os) or table.get("any") or GENERIC_HINT[row["kind"]].format(name=row["name"])


# --- requirements ---------------------------------------------------------------------------------------------------

def clean(kind: str, name: str, min_version: str = "", os_: str = "any", hint: str = "", fingerprint: str = "") -> dict:
    """A requirement's fields, validated: BadRequest (with the reason) for anything unsafe or the wrong shape for its kind."""
    kind, name = (kind or "").strip(), (name or "").strip()
    min_version, os_, hint = (min_version or "").strip(), (os_ or "any").strip().lower(), " ".join((hint or "").split())
    fp = (fingerprint or "").strip().upper().replace(" ", "")
    if kind not in KINDS:
        raise BadRequest(f"kind: one of {', '.join(KINDS)}")
    pattern = {"tool": TOOL_RE, "env": ENV_RE, "android_sdk_pkg": PKG_RE, "file": PATH_RE, "keystore": PATH_RE}[kind]
    if not pattern.match(name) or ".." in name.replace("\\", "/").split("/"):
        raise BadRequest({"tool": "name: a program name such as java or node (letters, digits, . _ + -; no path)",
                          "env": "name: a variable name such as ANDROID_HOME",
                          "android_sdk_pkg": "name: an SDK package id such as platforms;android-34",
                          "file": "name: a path (letters, digits, . _ - / \\ : ~ and spaces), no ..",
                          "keystore": "name: the keystore's path (letters, digits, . _ - / \\ : ~ and spaces), no .."}[kind])
    if kind in ("file", "keystore"):
        if NETWORK_PATH.match(name):
            raise BadRequest("name: a network or device path (starting with // or \\\\) is never checked: a Windows PC would try to sign in to it")
        if os_ == "windows" and (not WINDOWS_PATH.match(name) or ":" in name[2:]):
            raise BadRequest("name: for Windows, a path under ~ (like ~/keys/up.jks) or a drive path (like C:\\keys\\up.jks)")
    if kind == "tool" and name not in TOOLS:
        raise BadRequest(f"name: a known tool: {', '.join(TOOLS)}")
    if min_version and (kind != "tool" or not VERSION_RE.match(min_version)):
        raise BadRequest("minimum version: a number such as 17 or 3.11, and only for a tool")
    if os_ not in OSES:
        raise BadRequest(f"OS: one of {', '.join(OSES)}")
    if len(hint) > 300:
        raise BadRequest("install hint: up to 300 characters")
    if kind == "keystore":
        if not FINGERPRINT_RE.match(fp):
            raise BadRequest("fingerprint: the keystore's SHA-256 certificate fingerprint, 32 hex pairs separated by colons "
                             "(keytool -list -v prints it). It is not secret; the key itself never leaves your machine")
    elif fp:
        raise BadRequest("a fingerprint is only for a keystore")
    return {"kind": kind, "name": name, "min_version": min_version, "os": os_, "install_hint": hint, "expected_fingerprint": fp}


def list_rows(project_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM project_env_requirements WHERE project_id = %s ORDER BY kind, lower(name), os, id", (project_id,))
        return [dict(r) for r in cur.fetchall()]


def count(project_id: int) -> int:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM project_env_requirements WHERE project_id = %s", (project_id,))
        return cur.fetchone()["n"]


def _insert(cur, project_id: int, f: dict, source: str):
    cur.execute("INSERT INTO project_env_requirements (project_id, kind, name, min_version, os, install_hint, expected_fingerprint, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING *",
                (project_id, f["kind"], f["name"], f["min_version"], f["os"], f["install_hint"], f["expected_fingerprint"], source))
    return cur.fetchone()


def add_row(project: dict, kind: str, name: str, min_version: str = "", os_: str = "any", hint: str = "", fingerprint: str = "",
            by: str = "") -> dict:
    f = clean(kind, name, min_version, os_, hint, fingerprint)
    if count(project["id"]) >= MAX_ROWS:
        raise BadRequest(f"at most {MAX_ROWS} requirements per project")
    with get_cursor() as cur:
        row = _insert(cur, project["id"], f, "manual")
        if not row:
            raise BadRequest(f"{f['name']} is already listed for {f['os']}")
        repo.record_activity(cur, project["id"], by or "owner", "added", "environment", row["id"], f"Added environment requirement {f['kind']} {f['name']}")
    return dict(row)


def update_row(project: dict, row_id: int, kind: str, name: str, min_version: str = "", os_: str = "any", hint: str = "",
               fingerprint: str = "", by: str = "") -> dict:
    f = clean(kind, name, min_version, os_, hint, fingerprint)
    with get_cursor() as cur:
        try:
            cur.execute("UPDATE project_env_requirements SET kind = %s, name = %s, min_version = %s, os = %s, install_hint = %s, "
                        "expected_fingerprint = %s WHERE id = %s AND project_id = %s RETURNING *",
                        (f["kind"], f["name"], f["min_version"], f["os"], f["install_hint"], f["expected_fingerprint"], row_id, project["id"]))
        except Exception as exc:  # noqa: BLE001 - a unique clash
            if "project_env_requirements_one" in str(exc):
                raise BadRequest(f"{f['name']} is already listed for {f['os']}") from None
            raise
        row = cur.fetchone()
        if not row:
            raise NotFound("no such requirement")
        repo.record_activity(cur, project["id"], by or "owner", "updated", "environment", row_id, f"Updated environment requirement {f['kind']} {f['name']}")
    return dict(row)


def delete_row(project: dict, row_id: int, by: str = "") -> None:
    with get_cursor() as cur:
        cur.execute("DELETE FROM project_env_requirements WHERE id = %s AND project_id = %s RETURNING kind, name", (row_id, project["id"]))
        row = cur.fetchone()
        if not row:
            raise NotFound("no such requirement")
        repo.record_activity(cur, project["id"], by or "owner", "removed", "environment", row_id, f"Removed environment requirement {row['kind']} {row['name']}")


# --- "Suggest from the repo" ----------------------------------------------------------------------------------------

SKIP_DIRS = {"node_modules", ".git", "build", ".gradle", "dist", "venv", ".venv", "__pycache__", "Pods", "bin", "obj"}
MAX_READ = 256 * 1024
MAX_FILES = 300
MAX_DEPTH = 2


def repo_dir(project: dict) -> Path | None:
    """The project's folder on THIS server, if its repo is checked out under REPOS_DIR; None otherwise (the Environment tab then says so)."""
    name, sub = (project.get("repo") or "").strip(), (project.get("repo_path") or "").strip().strip("/")
    root = Path(config.REPOS_DIR).expanduser() if config.REPOS_DIR else None
    if not name or root is None or not root.is_dir() or ".." in f"{name}/{sub}".split("/"):
        return None
    path = (root / name / sub).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path if path.is_dir() else None


def _files(base: Path):
    seen = 0
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir() and not e.is_symlink() and e.name not in SKIP_DIRS and depth < MAX_DEPTH:
                stack.append((e, depth + 1))
            elif e.is_file() and not e.is_symlink():
                seen += 1
                if seen > MAX_FILES:
                    return
                yield e


def _read(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_READ)
    except OSError:
        return ""


def propose(base: Path) -> list[dict]:
    """Requirement rows the files under `base` imply. Read-only, bounded, nothing executed."""
    rows: dict[tuple, dict] = {}

    def want(kind, name, min_version="", os_="any"):
        rows.setdefault((kind, name.lower(), os_), {"kind": kind, "name": name, "min_version": min_version, "os": os_,
                                                    "install_hint": "", "expected_fingerprint": ""})

    gradle = [f for f in _files(base) if f.name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "libs.versions.toml")]
    if gradle:
        text = "\n".join(_read(f) for f in gradle)
        jdks = [int(b) if a == "1" and b else int(a) for a, b in re.findall(r"JavaVersion\.VERSION_(\d+)(?:_(\d+))?", text)]
        jdks += [int(x) for x in re.findall(r"jvmTarget\s*=\s*[\"']?(?:JvmTarget\.JVM_)?(\d+)", text)]
        jdks += [int(m.group(1) or m.group(2)) for m in re.finditer(r"jvmToolchain\((\d+)\)|JavaLanguageVersion\.of\((\d+)\)", text)]
        jdks = [j for j in jdks if j >= 8]
        android = bool(re.search(r"com\.android\.(application|library)|com\.android\.tools\.build:gradle", text))
        if android:       # the Android Gradle Plugin sets the floor: 8.x needs JDK 17, 7.x JDK 11
            agp = (re.search(r"com\.android\.tools\.build:gradle:(\d+)", text)
                   or re.search(r"com\.android\.(?:application|library)[\"')\s]+version\s*[\"'](\d+)", text))
            jdks.append(17 if (int(agp.group(1)) if agp else 8) >= 8 else 11)
        want("tool", "java", str(max(jdks)) if jdks else "")
        if android:
            want("env", "ANDROID_HOME")
            sdk = re.search(r"compileSdk(?:Version)?\s*[=( ]\s*(\d+)", text)
            if sdk:
                want("android_sdk_pkg", f"platforms;android-{sdk.group(1)}")
            bt = re.search(r"buildToolsVersion\s*[=( ]\s*[\"']([0-9.]+)", text)
            want("android_sdk_pkg", f"build-tools;{bt.group(1) if bt else '34.0.0'}")
    for f in _files(base):
        if f.name == "package.json":
            try:
                node = (json.loads(_read(f)).get("engines") or {}).get("node")
            except (ValueError, AttributeError):
                node = None
            m = re.search(r"(\d+)", node or "")
            want("tool", "node", m.group(1) if m else "")
        elif f.name == "pyproject.toml":
            m = re.search(r"requires-python\s*=\s*[\"'][><=~! ]*(\d+\.\d+)", _read(f))
            if m or "[project]" in _read(f):
                want("tool", "python3", m.group(1) if m else "")
        elif f.suffix == ".csproj":
            m = re.search(r"<TargetFrameworks?>[^<]*?net(?:coreapp)?(\d+\.\d+)", _read(f))
            want("tool", "dotnet", m.group(1) if m else "")
    return list(rows.values())


def suggest(project: dict, by: str = "") -> dict:
    """Add the rows the repo implies (never replacing or duplicating one). {found: bool, added: n, files: bool}: `found` False = the repo is
    not on this server (nothing could be read); the person adds rows by hand."""
    base = repo_dir(project)
    if base is None:
        return {"found": False, "added": 0}
    proposals = propose(base)
    added = 0
    with get_cursor() as cur:
        room = MAX_ROWS - count(project["id"])
        for p in proposals[:max(room, 0)]:
            f = clean(p["kind"], p["name"], p["min_version"], p["os"], "", "")
            if _insert(cur, project["id"], f, "suggested"):
                added += 1
        if added:
            repo.record_activity(cur, project["id"], by or "owner", "suggested", "environment", None, f"Added {added} suggested environment requirement(s) from the repo")
    return {"found": True, "added": added}


# --- checks (a lightweight runner job) -------------------------------------------------------------------------------

def _rows_for_runner(rows: list[dict]) -> list[dict]:
    """What the runner is sent: (kind, name, args) per row, plus the OS it applies to. Never a command."""
    out = []
    for r in rows:
        args = {}
        if r["min_version"]:
            args["min_version"] = r["min_version"]
        if r["expected_fingerprint"]:
            args["fingerprint"] = r["expected_fingerprint"]
        # `i` is the requirement's own id: the runner echoes it, so an answer lands on the row it was for even if rows were edited meanwhile
        out.append({"i": r["id"], "kind": r["kind"], "name": r["name"], "os": r["os"], "args": args})
    return out


def request_check(project_id: int, device_id: int, user_id: int | None = None) -> int | None:
    """Queue a check of `device_id` for the project (the runner's own, checked by the caller). None when the project lists nothing to check.
    A check already waiting is left alone. The runner is nudged if connected; a runner that is offline claims it when it next connects."""
    if count(project_id) == 0:
        return None
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_env_checks (project_id, device_id, requested_by) VALUES (%s, %s, %s) "
                    "ON CONFLICT (project_id, device_id) WHERE state IN ('queued', 'running') DO UPDATE SET project_id = EXCLUDED.project_id "
                    "RETURNING id", (project_id, device_id, user_id))
        check_id = cur.fetchone()["id"]
    try:
        from app import runner_ws
        runner_ws.nudge_env(device_id)
    except Exception:  # noqa: BLE001 - a hint only
        log.exception("env nudge")
    return check_id


def request_for_user(project_id: int, user_id: int, device_ids: list[int] | None = None) -> list[int]:
    """Queue a check on each of the person's live runners in the project's account (or just `device_ids`)."""
    from app import runs_on
    account_id = tree.account_root_id(project_id)
    mine = [d["id"] for d in runs_on.my_devices(account_id, user_id)]
    out = []
    for did in (mine if device_ids is None else [d for d in device_ids if d in mine]):
        cid = request_check(project_id, did, user_id)
        if cid:
            out.append(cid)
    return out


def queued_for_device(device_id: int) -> bool:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT 1 FROM project_env_checks WHERE device_id = %s AND (state = 'queued' OR (state = 'running' AND "
                    "started_at < now() - %s::interval)) LIMIT 1", (device_id, CLAIM_AGAIN_AFTER))
        return cur.fetchone() is not None


def claim(device_id: int) -> dict | None:
    """The runner's `env_claim`: the oldest waiting check of this device (or one it claimed and never finished) -> running, with the
    project's requirements as (kind, name, args) rows. None = nothing waiting. A project that lists nothing is finished compliant at once."""
    with get_cursor() as cur:
        cur.execute("SELECT id, project_id FROM project_env_checks WHERE device_id = %s AND (state = 'queued' OR (state = 'running' "
                    "AND started_at < now() - %s::interval)) ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED", (device_id, CLAIM_AGAIN_AFTER))
        chk = cur.fetchone()
        if not chk:
            return None
        cur.execute("UPDATE project_env_checks SET state = 'running', started_at = now() WHERE id = %s", (chk["id"],))
    rows = list_rows(chk["project_id"])
    if not rows:
        finish(device_id, chk["id"], [], "")
        return claim(device_id)
    return {"check_id": chk["id"], "project_id": chk["project_id"], "rows": _rows_for_runner(rows)}


def finish(device_id: int, check_id: int, results, runner_os: str) -> dict:
    """The runner's `env_finish`. Anything malformed becomes 'error' for that row (never trusted as 'ok'); the server decides compliance."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM project_env_checks WHERE id = %s AND device_id = %s AND state IN ('queued', 'running') FOR UPDATE",
                    (check_id, device_id))
        chk = cur.fetchone()
        if not chk:
            raise NotFound("no such check")
        rows = list_rows(chk["project_id"])
        by_i: dict[int, dict] = {}
        known = {r["id"] for r in rows}
        for r in (results if isinstance(results, list) else [])[:MAX_ROWS + 10]:
            if isinstance(r, dict) and isinstance(r.get("i"), int) and not isinstance(r.get("i"), bool) and r["i"] in known:
                s = r.get("s")
                by_i[r["i"]] = {"s": s if s in STATUSES else "error", "v": str(r.get("v") or "")[:40]}
        os_ = runner_os if runner_os in OSES[1:] else ""
        items = []
        for row in rows:      # a row added or edited after the runner claimed has no answer: it reads "could not be checked" until a new check
            got = by_i.get(row["id"], {"s": "error", "v": "no answer"})
            items.append({"kind": row["kind"], "name": row["name"], "min_version": row["min_version"], "os": row["os"], "status": got["s"],
                          "found": got["v"], "hint": hint_for(row, os_ or "linux") if got["s"] not in ("ok", "na") else ""})
        result = "compliant" if all(it["status"] in ("ok", "na") for it in items) else "missing"
        cur.execute("UPDATE project_env_checks SET state = 'done', result = %s, items = %s::jsonb, runner_os = %s, checked_at = now() WHERE id = %s",
                    (result, json.dumps(items), os_, check_id))
        cur.execute("DELETE FROM project_env_checks WHERE project_id = %s AND device_id = %s AND state = 'done' AND id <> %s",
                    (chk["project_id"], device_id, check_id))
    return {"result": result, "missing": sum(1 for it in items if it["status"] not in ("ok", "na"))}


def latest(project_id: int, device_id: int) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM project_env_checks WHERE project_id = %s AND device_id = %s AND state = 'done' ORDER BY id DESC LIMIT 1",
                    (project_id, device_id))
        row = cur.fetchone()
        cur.execute("SELECT 1 FROM project_env_checks WHERE project_id = %s AND device_id = %s AND state IN ('queued', 'running')",
                    (project_id, device_id))
        pending = cur.fetchone() is not None
    if not row:
        return {"state": "checking" if pending else "unchecked", "label": "Checking…" if pending else "Not checked", "missing": 0, "items": [],
                "checked_at": None, "runner_os": "", "pending": pending}
    items = row["items"] or []
    missing = sum(1 for it in items if it["status"] not in ("ok", "na"))
    return {"state": row["result"], "label": "Compliant" if row["result"] == "compliant" else f"Missing {missing}", "missing": missing,
            "items": items, "checked_at": row["checked_at"], "runner_os": row["runner_os"], "pending": pending}
