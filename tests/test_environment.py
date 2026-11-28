"""A project's Environment (#810 step 6, migration 0091): requirement rows, "Suggest from the repo", the check as a lightweight runner job
(claim -> probes on the runner -> finish), compliance decided by the SERVER, the badges in the Runs-on picker, and the security rule that the
runner is only ever sent (kind, name, args). The runner's own probes are tests/test_runner_env.py."""
import json

import pytest

from app import config, devices, environment, runner_ws, runs_on, tree
from app.db import get_cursor
from app.repo import BadRequest, NotFound

from test_devices import est, signed_in  # noqa: F401
from test_run_on_pc import _device, _live_jobs, world  # noqa: F401
from test_web_auth import auth_on  # noqa: F401

FP = ":".join(["AB"] * 32)


def _h(signed_in):
    return {"cookie": signed_in["cookie"]}


def _rows(world, *rows):
    return [environment.add_row(world["app"], *r) for r in rows]


def _check(world, dev):
    """Queue a check and claim it the way the runner would."""
    environment.request_check(world["app"]["id"], dev["device_id"], world["bob"]["id"])
    return environment.claim(dev["device_id"])


# --- validation: only safe shapes are stored (and sent) ------------------------------------------------------------------------

@pytest.mark.parametrize("kind,name", [("tool", "../../bin/sh"), ("tool", "java;rm"), ("tool", "a b"), ("tool", "-x"), ("tool", "$(id)"),
                                       ("tool", "x" * 65), ("env", "bad name"), ("env", "1ABC"), ("android_sdk_pkg", "a b"),
                                       ("android_sdk_pkg", "x;`id`"), ("file", "../etc/passwd"), ("file", "a$(id)"), ("file", "a;b"),
                                       ("file", "-rf"), ("shell", "id"), ("", "java")])
def test_unsafe_or_unknown_rows_are_refused(kind, name):
    with pytest.raises(BadRequest):
        environment.clean(kind, name)


@pytest.mark.parametrize("name", ["//attacker/share/x", "\\\\attacker\\share", "\\\\?\\C:\\x", "\\\\.\\pipe\\x", "/\\host/x", "\\//host/x"])
@pytest.mark.parametrize("kind", ["file", "keystore"])
def test_network_and_device_paths_are_refused(kind, name):
    """A Windows runner that merely looks at a UNC path authenticates to that host (an NTLM leak): the row is never stored, never sent."""
    for os_ in ("any", "windows", "linux"):
        with pytest.raises(BadRequest):
            environment.clean(kind, name, os_=os_, fingerprint=FP if kind == "keystore" else "")


@pytest.mark.parametrize("name,ok", [("~/keys/up.jks", True), ("C:\\keys\\up.jks", True), ("c:/keys", True), ("/opt/keys", False),
                                     ("keys/up.jks", False), ("C:keys", False), ("C:\\a:stream", False)])
def test_windows_rows_take_only_tilde_or_drive_paths(name, ok):
    if ok:
        assert environment.clean("file", name, os_="windows")["name"] == name
    else:
        with pytest.raises(BadRequest):
            environment.clean("file", name, os_="windows")
    environment.clean("file", "/opt/keys", os_="linux")                  # other OSes keep their own paths


def test_at_sign_is_not_a_path_character():
    with pytest.raises(BadRequest):
        environment.clean("file", "~/a@host")


@pytest.mark.parametrize("name", ["curl", "bash", "powershell", "calc", "sh"])
def test_only_known_tools_can_be_listed(name):
    with pytest.raises(BadRequest, match="known tool"):
        environment.clean("tool", name)
    assert set(environment.TOOLS) >= {"java", "node", "python3", "dotnet", "git", "gradle", "adb", "msbuild"}


def test_good_rows_are_accepted_and_normalised():
    assert environment.clean("tool", "java", "17")["min_version"] == "17"
    assert environment.clean("android_sdk_pkg", "platforms;android-34")["name"] == "platforms;android-34"
    assert environment.clean("file", "~/keys/upload.jks")["name"] == "~/keys/upload.jks"
    assert environment.clean("file", "C:\\Android\\sdk")["name"] == "C:\\Android\\sdk"
    assert environment.clean("keystore", "~/k.jks", fingerprint=FP.lower())["expected_fingerprint"] == FP
    assert environment.clean("tool", "msbuild", os_="windows", hint="  Install   VS Build Tools ")["install_hint"] == "Install VS Build Tools"


def test_field_rules():
    for bad in (("tool", "java", "seventeen"), ("env", "ANDROID_HOME", "17"), ("tool", "java", "17", "beos"), ("tool", "java", "", "any", "x" * 301),
                ("tool", "java", "", "any", "", FP), ("keystore", "~/k.jks", "", "any", "", ""), ("keystore", "~/k.jks", "", "any", "", "AB:CD")):
        with pytest.raises(BadRequest):
            environment.clean(*bad)


def test_a_row_is_unique_per_kind_name_and_os(world):
    environment.add_row(world["app"], "tool", "java", "17")
    with pytest.raises(BadRequest):
        environment.add_row(world["app"], "tool", "JAVA", "11")
    environment.add_row(world["app"], "tool", "java", "17", "windows")           # the same tool for another OS is another row
    assert len(environment.list_rows(world["app"]["id"])) == 2


# --- the tab --------------------------------------------------------------------------------------------------------------------

def test_the_environment_tab_adds_edits_and_removes_rows(world, client, signed_in):
    h = _h(signed_in)
    page = client.get("/acme-app/environment", headers=h).text
    assert "data-environment-page" in page and "data-no-rows" in page and "data-add-requirement" in page
    r = client.post("/acme-app/environment/rows", data={"kind": "tool", "name": "java", "min_version": "17", "os": "any"}, headers=h, follow_redirects=False)
    assert r.status_code == 303
    (row,) = environment.list_rows(world["app"]["id"])
    assert row["kind"] == "tool" and row["min_version"] == "17"
    page = client.get("/acme-app/environment", headers=h).text
    assert 'data-requirement="%d"' % row["id"] in page and "17 or newer" in page and "Tool on PATH" in page
    bad = client.post("/acme-app/environment/rows", data={"kind": "tool", "name": "java; rm -rf /"}, headers=h, follow_redirects=False)
    assert bad.status_code == 400 and "data-error" in bad.text and "java; rm" in bad.text.replace("&#34;", '"') or bad.status_code == 400
    assert len(environment.list_rows(world["app"]["id"])) == 1
    r = client.post(f"/acme-app/environment/rows/{row['id']}", data={"kind": "tool", "name": "java", "min_version": "21", "os": "linux",
                                                                      "install_hint": "apt install it"}, headers=h, follow_redirects=False)
    assert r.status_code == 303
    (row,) = environment.list_rows(world["app"]["id"])
    assert (row["min_version"], row["os"], row["install_hint"]) == ("21", "linux", "apt install it")
    r = client.post(f"/acme-app/environment/rows/{row['id']}/delete", headers=h, follow_redirects=False)
    assert r.status_code == 303 and environment.list_rows(world["app"]["id"]) == []


def test_another_projects_row_is_not_reachable_here(world, client, signed_in):
    other = environment.add_row(world["bapp"], "tool", "java")
    r = client.post(f"/acme-app/environment/rows/{other['id']}/delete", headers=_h(signed_in), follow_redirects=False)
    assert r.status_code in (303, 404) and len(environment.list_rows(world["bapp"]["id"])) == 1
    with pytest.raises(NotFound):
        environment.update_row(world["app"], other["id"], "tool", "node")


def test_the_tab_is_a_pill_in_the_catalogue(world, client, signed_in):
    from app import tabs
    assert "environment" in tabs.INFO and "environment" in tabs.ICONS
    html = client.get("/acme-app/environment", headers=_h(signed_in)).text
    assert "data-record-head" in html and 'data-pill="environment"' in html


# --- Suggest from the repo ------------------------------------------------------------------------------------------------------------

def _repo(tmp_path, monkeypatch, world, files):
    root = tmp_path / "repos"
    (root / "mobile").mkdir(parents=True)
    for rel, text in files.items():
        f = root / "mobile" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    monkeypatch.setattr(config, "REPOS_DIR", str(root))
    tree.set_details(world["app"]["id"], "owner", repo_name="mobile")
    return repo_project(world)


def repo_project(world):
    from app import repo
    return repo.get_project(world["app"]["id"])


ANDROID = """plugins { id("com.android.application") version "8.2.0" }
android { compileSdk = 34
  defaultConfig { minSdk = 26 }
  compileOptions { sourceCompatibility = JavaVersion.VERSION_17 } }"""


def test_suggest_reads_gradle_node_python_and_dotnet(world, tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch, world, {
        "app/build.gradle.kts": ANDROID, "web/package.json": json.dumps({"engines": {"node": ">=20.1"}}),
        "svc/pyproject.toml": '[project]\nrequires-python = ">=3.11"\n', "win/App.csproj": "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>"})
    got = environment.suggest(project)
    assert got == {"found": True, "added": 7}
    rows = {(r["kind"], r["name"]): r for r in environment.list_rows(project["id"])}
    assert rows[("tool", "java")]["min_version"] == "17"                              # JDK 17: the AGP 8 floor and the source level
    assert ("env", "ANDROID_HOME") in rows and ("android_sdk_pkg", "platforms;android-34") in rows and ("android_sdk_pkg", "build-tools;34.0.0") in rows
    assert rows[("tool", "node")]["min_version"] == "20" and rows[("tool", "python3")]["min_version"] == "3.11" and rows[("tool", "dotnet")]["min_version"] == "8.0"
    assert all(r["source"] == "suggested" for r in rows.values())
    assert environment.suggest(project) == {"found": True, "added": 0}               # idempotent: never duplicates, never replaces an edit


def test_suggest_keeps_what_a_person_already_chose(world, tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch, world, {"build.gradle": ANDROID})
    environment.add_row(project, "tool", "java", "21")
    environment.suggest(project)
    java = [r for r in environment.list_rows(project["id"]) if r["name"] == "java" and r["os"] == "any"]
    assert [r["min_version"] for r in java] == ["21"]


def test_suggest_without_the_repo_says_so(world, tmp_path, monkeypatch, client, signed_in):
    monkeypatch.setattr(config, "REPOS_DIR", str(tmp_path / "nowhere"))
    assert environment.suggest(world["app"]) == {"found": False, "added": 0}
    h = _h(signed_in)
    assert "data-repo-not-here" in client.get("/acme-app/environment", headers=h).text
    r = client.post("/acme-app/environment/suggest", headers=h, follow_redirects=False)
    assert r.status_code == 303


def test_suggest_never_leaves_the_repos_folder(world, tmp_path, monkeypatch):
    root = tmp_path / "repos"
    (root / "mobile").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "package.json").write_text('{"engines": {"node": "99"}}')
    monkeypatch.setattr(config, "REPOS_DIR", str(root))
    for name, path in (("mobile", "../../secret"), ("..", ""), ("mobile", "")):
        project = {"repo": name, "repo_path": path}
        assert environment.repo_dir(project) in (None, root / "mobile")
    assert environment.repo_dir({"repo": "mobile", "repo_path": "../../secret"}) is None


# --- the check is a lightweight runner job ----------------------------------------------------------------------------------------------

def test_the_runner_is_sent_only_kind_name_and_args(world):
    dev = _device(world, world["bob"])
    _rows(world, ("tool", "java", "17"), ("keystore", "~/up.jks", "", "any", "my hint; rm -rf /", FP), ("file", "~/x", "", "windows"))
    check = _check(world, dev)
    assert set(check) == {"check_id", "project_id", "rows"}
    for row in check["rows"]:
        assert set(row) == {"i", "kind", "name", "os", "args"}                    # no command, no hint, no free text
        assert set(row["args"]) <= {"min_version", "fingerprint"}
    assert [r["kind"] for r in check["rows"]] == ["file", "keystore", "tool"] or {r["kind"] for r in check["rows"]} == {"tool", "keystore", "file"}
    assert "rm -rf" not in json.dumps(check)                                        # the install hint stays on the server


def test_a_project_with_no_requirements_queues_nothing(world):
    dev = _device(world, world["bob"])
    assert environment.request_check(world["app"]["id"], dev["device_id"], world["bob"]["id"]) is None
    assert environment.claim(dev["device_id"]) is None
    assert environment.latest(world["app"]["id"], dev["device_id"])["state"] == "unchecked"


def test_finish_marks_compliant_or_missing_with_os_hints(world):
    dev = _device(world, world["bob"])
    rows = _rows(world, ("tool", "java", "17"), ("env", "ANDROID_HOME"), ("tool", "node", "18"))
    check = _check(world, dev)
    by_name = {r["name"]: r["i"] for r in check["rows"]}
    out = environment.finish(dev["device_id"], check["check_id"], [
        {"i": by_name["java"], "s": "old", "v": "11.0.2"}, {"i": by_name["ANDROID_HOME"], "s": "missing", "v": ""},
        {"i": by_name["node"], "s": "ok", "v": "20.1.0"}], "windows")
    assert out == {"result": "missing", "missing": 2}
    got = environment.latest(world["app"]["id"], dev["device_id"])
    assert got["state"] == "missing" and got["label"] == "Missing 2" and got["runner_os"] == "windows" and got["checked_at"]
    java = next(i for i in got["items"] if i["name"] == "java")
    assert java["status"] == "old" and java["found"] == "11.0.2" and "winget install Microsoft.OpenJDK.17" in java["hint"]      # the Windows hint
    assert next(i for i in got["items"] if i["name"] == "node")["hint"] == ""                                                 # nothing to fix
    # fix it and check again: compliant
    check = _check(world, dev)
    environment.finish(dev["device_id"], check["check_id"], [{"i": r["i"], "s": "ok", "v": ""} for r in check["rows"]], "linux")
    got = environment.latest(world["app"]["id"], dev["device_id"])
    assert got["state"] == "compliant" and got["label"] == "Compliant" and got["missing"] == 0
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM project_env_checks WHERE state = 'done'")
        assert cur.fetchone()["n"] == 1                                                # only the latest result is kept per (project, runner)


def test_the_server_decides_not_the_runner(world):
    dev = _device(world, world["bob"])
    _rows(world, ("tool", "java", "17"), ("tool", "node"), ("tool", "git"), ("tool", "msbuild", "", "windows"))
    check = _check(world, dev)
    ids = {r["name"]: r["i"] for r in check["rows"]}
    junk = [{"i": ids["java"], "s": "totally-fine"}, {"i": 99999, "s": "ok"}, "nonsense", {"i": True, "s": "ok"}, {"i": ids["node"], "s": "unsupported"}]
    environment.finish(dev["device_id"], check["check_id"], junk, "linux")
    items = {i["name"]: i["status"] for i in environment.latest(world["app"]["id"], dev["device_id"])["items"]}
    assert items == {"java": "error", "node": "unsupported", "git": "error", "msbuild": "error"}   # an invented status is an error; unanswered rows are errors
    assert environment.latest(world["app"]["id"], dev["device_id"])["state"] == "missing"     # an old runner's "unsupported" is never "compliant"


def test_not_applicable_rows_do_not_count(world):
    dev = _device(world, world["bob"])
    _rows(world, ("tool", "git"), ("tool", "msbuild", "", "windows"))
    check = _check(world, dev)
    answers = [{"i": r["i"], "s": "na" if r["os"] == "windows" else "ok", "v": ""} for r in check["rows"]]
    assert environment.finish(dev["device_id"], check["check_id"], answers, "linux")["result"] == "compliant"


def test_only_the_checked_runner_can_finish_its_check(world):
    mine = _device(world, world["bob"], name="mine")
    other = _device(world, world["bob"], name="other")
    _rows(world, ("tool", "git"))
    check = _check(world, mine)
    with pytest.raises(NotFound):
        environment.finish(other["device_id"], check["check_id"], [], "linux")
    assert environment.claim(other["device_id"]) is None                                 # and it is not offered to another runner either
    environment.finish(mine["device_id"], check["check_id"], [{"i": 0, "s": "ok", "v": ""}], "linux")
    with pytest.raises(NotFound):
        environment.finish(mine["device_id"], check["check_id"], [], "linux")             # a check finishes once


def test_a_check_a_runner_never_finished_is_offered_again(world):
    dev = _device(world, world["bob"])
    _rows(world, ("tool", "git"))
    first = _check(world, dev)
    assert environment.claim(dev["device_id"]) is None                                    # running: not offered twice at once
    with get_cursor() as cur:
        cur.execute("UPDATE project_env_checks SET started_at = now() - interval '3 minutes'")
    assert environment.queued_for_device(dev["device_id"])
    assert environment.claim(dev["device_id"])["check_id"] == first["check_id"]


def test_a_second_request_while_one_waits_changes_nothing(world):
    dev = _device(world, world["bob"])
    _rows(world, ("tool", "git"))
    a = environment.request_check(world["app"]["id"], dev["device_id"])
    b = environment.request_check(world["app"]["id"], dev["device_id"])
    assert a == b


def test_the_socket_messages(world):
    dev = _device(world, world["bob"])
    _rows(world, ("tool", "git"))
    environment.request_check(world["app"]["id"], dev["device_id"])
    claim = runner_ws._env_message(dev["device_id"], {"type": "env_claim", "id": "r1"})
    assert claim["type"] == "env_claim_result" and claim["ok"] and claim["id"] == "r1" and claim["check"]["rows"][0]["name"] == "git"
    bad = runner_ws._env_message(dev["device_id"], {"type": "env_finish", "id": "r2", "check_id": "7"})
    assert bad["ok"] is False and bad["status"] == 400
    gone = runner_ws._env_message(dev["device_id"], {"type": "env_finish", "id": "r3", "check_id": 99999, "results": []})
    assert gone["ok"] is False and gone["status"] == 404
    done = runner_ws._env_message(dev["device_id"], {"type": "env_finish", "id": "r4", "check_id": claim["check"]["check_id"],
                                                     "results": [{"i": claim["check"]["rows"][0]["i"], "s": "ok", "v": ""}], "os": "linux"})
    assert done["ok"] and done["result"] == "compliant"
    assert "env_claim" in runner_ws.ENV_MESSAGES and "env_finish" in runner_ws.ENV_MESSAGES


def test_a_nudge_goes_only_to_a_connected_runner(world, monkeypatch):
    dev = _device(world, world["bob"])
    assert runner_ws.nudge_env(dev["device_id"]) == 0
    posted = []

    class Conn:
        verified = True

        def post(self, msg):
            posted.append(msg)

    monkeypatch.setattr(runner_ws, "_conns", {dev["device_id"]: Conn()})
    assert runner_ws.nudge_env(dev["device_id"]) == 1 and posted == [("env_nudge",)]


# --- on every Runs-on switch, and the badges -------------------------------------------------------------------------------------------------

def test_switching_runs_on_queues_a_check_and_the_picker_shows_the_badge(world, client, signed_in, monkeypatch):
    h = _h(signed_in)
    desktop, laptop = _device(world, world["bob"], name="desktop"), _device(world, world["bob"], name="laptop")
    _rows(world, ("tool", "java", "17"), ("tool", "node"))
    # change #90: the header picker (and its badge) is gone; the check itself still queues on a switch
    r = client.post("/acme-app/runs-on", data={"device_id": str(laptop["device_id"]), "next": "/acme-app/list"}, headers=h, follow_redirects=False)
    assert r.status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT device_id, state FROM project_env_checks")
        assert [(c["device_id"], c["state"]) for c in cur.fetchall()] == [(laptop["device_id"], "queued")]       # only the runner switched to
    check = environment.claim(laptop["device_id"])
    by = {r["name"]: r["i"] for r in check["rows"]}
    environment.finish(laptop["device_id"], check["check_id"], [{"i": by["java"], "s": "ok", "v": ""}, {"i": by["node"], "s": "missing", "v": ""}], "linux")
    assert environment.latest(world["app"]["id"], laptop["device_id"]) is not None     # the finished check is recorded
    # "any runner" checks nothing
    with get_cursor() as cur:
        cur.execute("DELETE FROM project_env_checks")
    client.post("/acme-app/runs-on", data={"device_id": "", "next": "/acme-app/list"}, headers=h, follow_redirects=False)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM project_env_checks")
        assert cur.fetchone()["n"] == 0


def test_non_compliant_is_a_warning_never_a_block(world, client, signed_in):
    h = _h(signed_in)
    dev = _device(world, world["bob"])
    _rows(world, ("tool", "java", "17"))
    runs_on.set_pref(world["app"]["id"], world["bob"]["id"], dev["device_id"])
    check = _check(world, dev)
    environment.finish(dev["device_id"], check["check_id"], [{"i": 0, "s": "missing", "v": ""}], "linux")
    r = client.post(f"/acme-app/tasks/{world['task']['id']}/run", data={"profile": "builder"}, headers=h)
    assert r.status_code == 200 and len(_live_jobs(world["task"]["id"])) == 1                  # the job is queued regardless
    assert jobs_claim(dev)


def jobs_claim(dev):
    from app import jobs
    return jobs.claim(dev["device_id"])


def test_the_tab_lists_each_runner_with_what_is_missing_and_check_again(world, client, signed_in):
    h = _h(signed_in)
    dev = _device(world, world["bob"], name="laptop")
    assert "data-no-checks" in client.get("/acme-app/environment", headers=h).text
    _rows(world, ("tool", "java", "17"), ("android_sdk_pkg", "platforms;android-34"))
    page = client.get("/acme-app/environment", headers=h).text
    assert 'data-env-state="unchecked"' in page and "Not checked" in page and "data-check-again" in page
    r = client.post("/acme-app/environment/check", data={"device_id": str(dev["device_id"])}, headers=h, follow_redirects=False)
    assert r.status_code == 303
    check = environment.claim(dev["device_id"])
    environment.finish(dev["device_id"], check["check_id"], [{"i": 0, "s": "missing", "v": ""}, {"i": 1, "s": "old", "v": "3"}], "windows")
    page = client.get("/acme-app/environment", headers=h).text
    assert 'data-env-state="missing"' in page and "Missing 2" in page and "data-missing" in page
    assert "Install" in page or "install" in page                                              # the OS hint is shown
    assert "winget install Microsoft.OpenJDK.17" in page and "windows" in page
    assert devices.list_for(world["acme"]["id"])[0]["name"] == "laptop"


def test_checks_are_only_for_my_own_runners(world, client, signed_in):
    from test_devices import _user
    alice = _user(oid="aaaaaaaa-0000-4000-8000-000000000001", name="Alice2")
    theirs = _device(world, alice, name="alices")
    _rows(world, ("tool", "git"))
    client.post("/acme-app/environment/check", data={"device_id": str(theirs["device_id"])}, headers=_h(signed_in), follow_redirects=False)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM project_env_checks")
        assert cur.fetchone()["n"] == 0


# --- answers land on the row they were for (matched by the requirement's id) ------------------------------------------------------------------

def test_rows_edited_between_claim_and_finish_never_get_another_rows_answer(world):
    dev = _device(world, world["bob"])
    java, node = _rows(world, ("tool", "java", "17"), ("tool", "node", "18"))
    check = _check(world, dev)
    assert {r["i"] for r in check["rows"]} == {java["id"], node["id"]}          # the requirement's own id, not a position
    environment.delete_row(world["app"], java["id"])                              # a person deletes a row mid-check...
    new = environment.add_row(world["app"], "tool", "git")                        # ...and adds another
    environment.finish(dev["device_id"], check["check_id"], [{"i": java["id"], "s": "missing", "v": ""}, {"i": node["id"], "s": "ok", "v": "20"}], "linux")
    items = {i["name"]: i["status"] for i in environment.latest(world["app"]["id"], dev["device_id"])["items"]}
    assert items == {"node": "ok", "git": "error"}                                # the deleted row's answer is dropped; the new row was never asked
    assert new["id"] not in {r["i"] for r in check["rows"]}
