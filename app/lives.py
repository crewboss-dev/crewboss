"""Where each project lives and how it is tested (#839): per stage (Dev / Staging / Production) the server, folder and URL, and the SHARED
SERVICES on that server (the other apps there, their domains, databases and volumes), read from the server manifests (app/manifest.py), never typed.
Plus the project's TEST MODE:

  browser    a web app. Dev = the runner's preview, Staging = the URL the owner opens, Production = live.
  installed  Android / iOS / EXE / MSI / anything with no web screen. The runner is the test area: a task is pinned to one runner until it is
             committed (app/runs_on.installed_pin), and the installable is signed on that runner. Staging = a test channel (free text).
  none       no UI (a library, a worker): tests and logs. Staging is optional.

Conflicts between people working on one app are left to the merge, as before: every task has its own branch and worktree, and the branch is
rebased on main before review."""
from __future__ import annotations

from app import access, manifest, repo, runs_on, stateful, tree
from app.db import get_cursor
from app.repo import BadRequest, Forbidden

MODES = ("browser", "installed", "none")
MODE_LABELS = {"browser": "Browser", "installed": "Installed", "none": "No UI"}
MODE_HELP = {
    "browser": "A web app. Dev is the runner's preview, Staging is a web address you open to check, Production is live.",
    "installed": "Android, iOS, a desktop installer or anything with no web screen. The runner is the test area: a task is pinned to one runner "
                 "until it is committed, and the installable is built and signed on that runner. You install it on a device.",
    "none": "Tests and logs only (a library, a worker). Staging is optional.",
}
STAGES = (("dev", "Dev"), ("staging", "Staging"), ("prod", "Production"))
CHANNEL_MAX = 200


def set_test_mode(who, project: dict, mode: str, channel: str = "") -> dict:
    """Store the project's test mode (and, for Installed, its staging test channel). Needs the right to change the project."""
    if not access.can_edit(who, project["id"]):
        raise Forbidden("you can't change this project")
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise BadRequest("test mode: browser, installed or none")
    channel = " ".join((channel or "").split())
    if len(channel) > CHANNEL_MAX:
        raise BadRequest(f"the test channel is at most {CHANNEL_MAX} characters")
    if stateful.looks_like_secret(channel):
        raise BadRequest("the test channel looks like a secret. Name the channel (such as 'Play internal track'), not a key")
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET test_mode = %s, test_channel = %s WHERE id = %s RETURNING *", (mode, channel, project["id"]))
        row = cur.fetchone()
        repo.record_activity(cur, project["id"], f"user:{who.user_id}" if who.user_id else "owner", "set", "test_mode", project["id"],
                             f"Test mode is now {MODE_LABELS[mode]}")
    return dict(row)


def set_require_state(who, project: dict, on: bool) -> None:
    """Opt in (or out) of refusing a deploy whose state is not declared (the release check's block)."""
    stateful.require_manage(who, project)
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET require_state_declared = %s WHERE id = %s", (bool(on), project["id"]))


def _targets(project_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT t.id, t.name, t.env, t.repo_path, t.branch, t.enabled, t.server_id, s.name AS server_name FROM deploy_targets t "
                    "JOIN commissioned_servers s ON s.id = t.server_id WHERE t.project_id = %s ORDER BY t.env, lower(t.name)", (project_id,))
        return [dict(r) for r in cur.fetchall()]


def _slug_ids(slugs: set) -> dict:
    if not slugs:
        return {}
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT slug, id FROM projects WHERE slug = ANY(%s)", (list(slugs),))
        return {r["slug"]: r["id"] for r in cur.fetchall()}


def _shared(who, project: dict, target: dict, mf: dict, cache: dict) -> dict:
    """The other apps on this target's server, from its manifest. An app of a project the viewer may not see is only counted."""
    others = [a for a in mf["apps"] if not (a["project"] == project["slug"] and a["target"] == target["name"])]
    ids = cache.get("ids")
    if ids is None:
        ids = cache["ids"] = _slug_ids({a["project"] for a in mf["apps"]})
    visible = access.visible_ids(who)
    shown, hidden = [], 0
    for a in others:
        pid = ids.get(a["project"])
        if visible is not None and pid not in visible:
            hidden += 1
            continue
        items = a["backup"]["items"]
        shown.append({"project": a["project"], "name": a["name"], "stage": a["stage"], "folder": a["folder"], "domains": a["domains"],
                      "ports": a["ports"], "databases": [i["name"] for i in items if i["kind"] == "database"],
                      "volumes": [i.get("name") or i.get("path") for i in items if i["kind"] in ("volume", "uploads")]})
    return {"apps": shown, "hidden": hidden,
            "domains": sorted({d for a in shown for d in a["domains"]}),
            "databases": sorted({d for a in shown for d in a["databases"]}),
            "volumes": sorted({v for a in shown for v in a["volumes"] if v})}


def _url(recipe: dict | None) -> str:
    r = recipe or {}
    if r.get("domains"):
        return "https://" + r["domains"][0]
    h = r.get("health_url") or ""
    return h if h.startswith("http") else ""


def view(who, project: dict, user_id: int | None = None) -> dict:
    """Everything the Where it lives page draws, for one project. Reads stored manifests (or builds one when none exists yet)."""
    mode = project.get("test_mode") or "browser"
    targets = [t for t in _targets(project["id"])]
    mf_cache: dict = {}
    cache: dict = {}
    stages = []
    for key, label in STAGES:
        rows = []
        for t in (x for x in targets if x["env"] == key):
            mf = mf_cache.get(t["server_id"])
            if mf is None:
                st = manifest.stored(t["server_id"])
                mf = mf_cache[t["server_id"]] = st["manifest"] if st else manifest.build(t["server_id"])
            recipe = stateful.effective(project["id"], t["id"])
            rows.append({**t, "url": _url(recipe), "declared": stateful.declared(recipe), "shared": _shared(who, project, t, mf, cache)})
        stages.append({"key": key, "label": label, "targets": rows, "role": _role(mode, key, project)})
    server_ids = sorted({t["server_id"] for t in targets if t["enabled"]})
    names = {t["server_id"]: t["server_name"] for t in targets}
    if not server_ids:
        topo = {"kind": "none", "text": "No server is set up for this project yet."}
    elif len(server_ids) == 1:
        topo = {"kind": "one", "text": f"On one server: {names[server_ids[0]]}."}
    else:
        topo = {"kind": "spread", "text": f"Spread across {len(server_ids)} servers: {', '.join(names[i] for i in server_ids)}."}
    runner = None
    if mode == "installed" and user_id is not None:
        account_id = tree.account_root_id(project["id"])
        pref = runs_on.get_pref(project["id"], user_id)
        runner = next((d for d in runs_on.my_devices(account_id, user_id) if d["id"] == pref), None)
    return {"mode": mode, "mode_label": MODE_LABELS[mode], "channel": project.get("test_channel") or "", "stages": stages, "topology": topo,
            "runner": runner}


def _role(mode: str, stage: str, project: dict) -> str:
    if mode == "browser":
        return {"dev": "The runner's preview: your runner serves it while a task runs.",
                "staging": "The staging address you open to check a change before it goes live.",
                "prod": "Live."}[stage]
    if mode == "installed":
        return {"dev": "The runner is the test area: build, sign and install from the runner a task is pinned to.",
                "staging": "A test channel" + (f": {project.get('test_channel')}" if project.get("test_channel") else " (set it below)")
                           + " such as an internal track, TestFlight or a test installer.",
                "prod": "The store or the published release."}[stage]
    return {"dev": "Tests and logs on the runner.", "staging": "Optional: nothing to open, only logs and test results.",
            "prod": "Live."}[stage]


def run_line(project: dict, device_name: str = "") -> str:
    """The line on a task's job area: only for an Installed project."""
    if (project.get("test_mode") or "browser") != "installed":
        return ""
    return f"Installed app: tested on {device_name}" if device_name else "Installed app: tested on the runner this task is pinned to"
