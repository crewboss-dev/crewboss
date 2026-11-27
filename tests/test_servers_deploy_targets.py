"""Servers page = deploy targets (task #648, change #52, migration 0041): the env (dev/prod) and reach (internal/external)
tags, the one editor for deploy targets, the page layout (servers with their targets, filter chips, the estate view inside a
closed <details>) and the page without servers.yml."""
import re

import psycopg
import pytest

from app import deploys, server_access
from app import servers as srv
from app.db import get_cursor
from app.repo import BadRequest

from test_deploys import depl  # noqa: F401  (fixture: acme, one commissioned server web-1, one target 'prod')
from test_devices import est, signed_in  # noqa: F401
from test_web_auth import auth_on  # noqa: F401
from test_jobs import world  # noqa: F401

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}


def _h(signed_in):
    return {"cookie": signed_in["cookie"]}


def _target(tid):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM deploy_targets WHERE id = %s", (tid,))
        return cur.fetchone()


# --- the migration ------------------------------------------------------------------------------------------------

def test_migration_columns_and_checks(depl):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT table_name, column_name, column_default, is_nullable FROM information_schema.columns "
                    "WHERE (table_name, column_name) IN (('deploy_targets', 'env'), ('commissioned_servers', 'reach'))")   # reach: the column stays (#810), nothing reads it
        cols = {(r["table_name"], r["column_name"]): r for r in cur.fetchall()}
    assert "'prod'" in cols[("deploy_targets", "env")]["column_default"]
    assert "'internal'" in cols[("commissioned_servers", "reach")]["column_default"]
    assert all(c["is_nullable"] == "NO" for c in cols.values())
    assert depl["target"]["env"] == "prod"                                           # the default
    for sql in ("UPDATE deploy_targets SET env = 'qa'", "UPDATE commissioned_servers SET reach = 'dmz'"):
        with pytest.raises(psycopg.errors.CheckViolation):
            with get_cursor() as cur:
                cur.execute(sql)


# --- setting the tags ---------------------------------------------------------------------------------------------

def test_set_env_validates(depl):
    acct, tid, sid = depl["acme"]["id"], depl["target"]["id"], depl["server_id"]
    deploys.set_target_env(acct, tid, "dev", "user:1")
    assert _target(tid)["env"] == "dev"
    deploys.set_target_env(acct, tid, "staging", "user:1")                           # dev | staging | production (change #77)
    deploys.set_target_env(acct, tid, "production", "user:1")
    assert _target(tid)["env"] == "prod"                                              # 'production' is stored as prod
    deploys.set_target_env(acct, tid, "dev", "user:1")
    for bad in ("", "qa", "PROD ; x"):
        with pytest.raises(BadRequest):
            deploys.set_target_env(acct, tid, bad)
    assert _target(tid)["env"] == "dev"
    t2 = deploys.add_target(acct, "acme-app", "devbox", sid, "/opt/stacks/app2", "", "main", "user:1", "dev")
    assert t2["env"] == "dev"
    with pytest.raises(BadRequest):
        deploys.add_target(acct, "acme-app", "bad", sid, "/opt/stacks/app3", "", "main", "user:1", "qa")


def test_set_env_is_account_scoped(depl):
    from app import repo
    other = repo.get_project_by_slug("beta")
    from app.repo import NotFound
    with pytest.raises(NotFound):
        deploys.set_target_env(other["id"], depl["target"]["id"], "dev")


# --- the page -----------------------------------------------------------------------------------------------------

def test_page_lists_servers_targets_and_chips(depl, client, signed_in):
    html = client.get("/acme/machines?tab=servers", headers=_h(signed_in)).text
    assert 'data-commissioned="web-1"' in html and 'data-reach-chip' not in html and 'data-status-chip' in html
    assert 'data-target="%d"' % depl["target"]["id"] in html and "data-used-by" in html
    assert 'href="/acme-app/releases"' in html and "acme-app · prod" in html            # "Used by" chips link to the project's Releases
    assert "/opt/stacks/app" in html and "branch main" in html                         # in the chip's title
    assert "data-toggle" not in html and "Add deploy target" not in html and "data-target-form" not in html   # edited on the project (#810)
    assert "data-add-panel" in html
    for key in ("all", "attention"):                                 # the Machines page's filters; jobs/deploys became the tabs (#838)
        assert f'data-filter="{key}"' in html
    assert 'data-filter="internal"' not in html and 'data-reach' not in html            # reach drives nothing (#810)
    assert 'data-envs="prod"' in html                                                   # what the filter reads
    assert "Dashboard access" not in html                                               # one list, no separate block


def test_empty_state_when_no_servers(est, client):
    html = client.get("/acme/servers").text
    assert "data-servers-empty" in html and "data-add-server" in html and "data-filters" not in html


def test_estate_stub_is_gone(depl, client, signed_in, monkeypatch):
    """#810: monitoring belongs to Tech Assist, so the page no longer asks the estate repo for anything."""
    monkeypatch.setattr(srv, "summary", lambda slug, **k: pytest.fail("the page must not read the estate"))
    html = client.get("/acme/machines?tab=servers", headers=_h(signed_in)).text
    assert 'data-commissioned="web-1"' in html and "data-estate" not in html and "Estate status" not in html
    assert "data-next-steps" not in html


# --- the one editor -----------------------------------------------------------------------------------------------

def test_servers_page_no_longer_edits_targets(depl, client, signed_in):
    h, sid = _h(signed_in), depl["server_id"]
    form = {"project_slug": "acme-app", "name": "dev", "server_id": sid, "repo_path": "/opt/stacks/dev", "env": "dev"}
    assert client.post("/acme/servers/targets", data=form, headers=h).status_code in (404, 405)
    t = depl["target"]["id"]
    for path in (f"/acme/servers/targets/{t}/env", f"/acme/servers/targets/{t}/disable"):
        assert client.post(path, data={"env": "dev"}, headers=h).status_code in (404, 405)
    assert _target(t)["env"] == "prod" and _target(t)["enabled"] is True
    # #810: the reach tag drives nothing, so its route is gone too
    assert client.post(f"/acme/servers/{sid}/reach", data={"reach": "external"}, headers={**OWN, **h}).status_code in (404, 405)


def test_target_editor_on_the_projects_releases_tab(depl, client, signed_in):
    h, sid = _h(signed_in), depl["server_id"]
    page = client.get("/acme-app/releases", headers=h).text
    assert "data-add-target" in page and "Add a place to deploy" in page and "data-target-form" in page
    assert 'value="/opt/stacks/acme-app"' in page and 'name="env" value="dev" class="sr-only peer" checked' in page   # prefilled folder, Dev default
    assert "data-env-segment" in page and "docker compose up -d --build" in page                                # examples for the restart command
    form = {"server_id": sid, "repo_path": "/opt/stacks/dev", "branch": "main", "post_cmd": "", "env": "dev"}
    assert client.post("/acme-app/releases/targets", data=form, follow_redirects=False).status_code in (302, 303, 401, 403)
    r = client.post("/acme-app/releases/targets", data=form, headers=h, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/acme-app/releases"
    new = next(t for t in deploys.list_targets(depl["acme"]["id"]) if t["name"] == "dev")         # the name is the environment
    assert new["env"] == "dev" and new["project_slug"] == "acme-app"
    bad = client.post("/acme-app/releases/targets", data={**form, "env": "qa"}, headers=h)
    assert bad.status_code == 400 and "data-target-error" in bad.text and "dev, staging or production" in bad.text
    # env + enable toggles live on the project too
    assert client.post(f"/acme-app/releases/targets/{new['id']}/env", data={"env": "prod"}, headers=h,
                       follow_redirects=False).status_code == 303 and _target(new["id"])["env"] == "prod"
    assert client.post(f"/acme-app/releases/targets/{new['id']}/env", data={"env": "nope"}, headers=h, follow_redirects=False).status_code in (303, 400)
    assert _target(new["id"])["env"] == "prod"                                            # a bad tag changes nothing
    assert client.post(f"/acme-app/releases/targets/{new['id']}/disable", headers=h, follow_redirects=False).status_code == 303
    assert _target(new["id"])["enabled"] is False
    assert client.post(f"/acme-app/releases/targets/{new['id']}/bogus", headers=h, follow_redirects=False).status_code in (303, 404)
    assert _target(new["id"])["enabled"] is False                                     # an unknown action changes nothing


def test_only_ready_machines_are_offered_and_accepted(depl, client, signed_in):
    h, acct = _h(signed_in), depl["acme"]["id"]
    with get_cursor() as cur:
        cur.execute("INSERT INTO commissioned_servers (account_id, name, address, login_user, status, created_by) "
                    "VALUES (%s, 'pending01', '10.0.0.9', 'deploy', 'pending', 'test') RETURNING id", (acct,))
        pid = cur.fetchone()["id"]
    page = client.get("/acme-app/releases", headers=h).text
    assert ">web-1</option>" in page and "pending01" not in page
    r = client.post("/acme-app/releases/targets", headers=h,
                    data={"server_id": pid, "repo_path": "/opt/stacks/x", "env": "dev"}, follow_redirects=False)
    assert r.status_code == 400 and "ready to deploy to" in r.text


def test_another_projects_target_is_not_editable_here(depl, client, signed_in):
    h = _h(signed_in)
    r = client.post(f"/acme/releases/targets/{depl['target']['id']}/disable", headers=h, follow_redirects=False)     # the target is acme-app's, not acme's
    assert r.status_code in (303, 403, 404) and _target(depl["target"]["id"])["enabled"] is True


def test_approvals_page_links_to_each_projects_releases_instead_of_editing(depl, client, signed_in):
    html = client.get("/acme/approvals", headers=_h(signed_in)).text
    assert 'href="/acme-app/releases"' in html and "data-targets-link" in html and "/acme/servers#targets" not in html
    assert "data-target-form" not in html
    assert client.post("/acme/approvals/targets", data={}, headers=_h(signed_in)).status_code in (404, 405)


def test_env_flash_never_echoes_input(depl, client, signed_in):
    """Review fix: the flash cookie is fixed text; input strip() accepts (e.g. U+2028) must not reach the header."""
    t = next(t for t in deploys.list_targets(depl["acme"]["id"]) if t["name"] == "prod")
    r = client.post(f"/acme-app/releases/targets/{t['id']}/env", data={"env": "dev "}, headers=_h(signed_in),
                    follow_redirects=False)
    assert r.status_code == 303 and _target(t["id"])["env"] == "dev"


def test_the_releases_target_routes_need_a_real_signed_in_person(depl, client):
    """Restored from the old /servers/targets routes: these POSTs make commands that run on a server, so the owner browser alone is not enough."""
    sid, t = depl["server_id"], depl["target"]["id"]
    form = {"server_id": sid, "repo_path": "/opt/stacks/x", "env": "dev"}
    for path, data in (("/acme-app/releases/targets", form), (f"/acme-app/releases/targets/{t}/env", {"env": "dev"}),
                       (f"/acme-app/releases/targets/{t}/disable", {})):
        r = client.post(path, data=data, headers=OWN, follow_redirects=False)       # sign-in is off in the suite: no person behind the request
        assert r.status_code == 403, path
    assert _target(t)["enabled"] is True and _target(t)["env"] == "prod"


def test_a_target_never_points_at_a_server_that_is_not_ready(depl):
    from app import repo
    acct = depl["acme"]["id"]
    with get_cursor() as cur:
        cur.execute("INSERT INTO commissioned_servers (account_id, name, address, login_user, status, created_by) "
                    "VALUES (%s, 'pending02', '10.0.0.8', 'deploy', 'pending', 'test') RETURNING id", (acct,))
        pid = cur.fetchone()["id"]
    with pytest.raises(BadRequest, match="ready"):
        deploys.add_target(acct, "acme-app", "dev", pid, "/opt/stacks/y", "", "main", "user:1", "dev")        # the account-level path
    with get_cursor() as cur:
        cur.execute("UPDATE commissioned_servers SET probe_misses = 3, unreachable_since = now() - interval '12 minutes' WHERE id = %s", (depl["server_id"],))
    with pytest.raises(BadRequest, match="ready"):
        deploys.add_target(acct, "acme-app", "dev2", depl["server_id"], "/opt/stacks/y", "", "main", "user:1", "dev")   # Not answering is not ready either
