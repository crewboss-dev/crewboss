"""The Machines page (#810, migration 0089): ONE page for Servers and Runners, a card per machine with a block per role, the join between a
runner and a server (suggested, never automatic), the role filters, the autosaving labels, the jobs split, the lock banner and the
redirects from the old pages. The owner's view; a designer's is tests/test_machines_designer.py (a different fixture world)."""
import re

import pytest

from app import deploys, devices, jobs, server_access
from app.db import get_cursor

from test_deploys import depl  # noqa: F401  (fixture: acme, a ready server web-1, one target 'prod', a runner 'pc')
from test_devices import est, signed_in  # noqa: F401
from test_jobs import _hold, world  # noqa: F401
from test_web_auth import auth_on  # noqa: F401


def _h(signed_in):
    return {"cookie": signed_in["cookie"]}


def _page(client, signed_in, query=""):
    r = client.get("/acme/machines" + query, headers=_h(signed_in))
    assert r.status_code == 200
    return r.text


def _device_id(depl):
    return depl["dev"]["device_id"]


def test_one_card_per_machine_with_a_block_per_role(depl, client, signed_in):
    runners = _page(client, signed_in, "?tab=runners")                      # (#838) a tab per role: the runner pc on one, the server web-1 on the other
    servers = _page(client, signed_in, "?tab=servers")
    assert runners.count("data-machine=") == 1 and servers.count("data-machine=") == 1
    assert 'data-role-chip="jobs"' in runners and 'data-block="jobs"' in runners and 'data-block="deploys"' not in runners
    assert 'data-role-chip="deploys"' in servers and 'data-block="deploys"' in servers and 'data-block="jobs"' not in servers
    assert "connects out to Crewboss" in runners and "connects IN with a one-hour, single-command pass" in servers   # a trust line per role
    for html in (runners, servers):
        assert "data-deploy-flow" in html and "your OK" in html.lower() or "Your OK" in html     # the diagram, on both tabs
        assert "data-flow" not in html.replace("data-deploy-flow", "")                           # the old chip strip is gone
        for gone in ("Estate status", "data-next-steps", "data-reach-chip", "data-add-machine"):
            assert gone not in html


def test_join_is_suggested_and_never_automatic(depl, client, signed_in):
    dev_id, sid = _device_id(depl), depl["server_id"]
    assert "data-join-suggestion" not in _page(client, signed_in)           # 'pc' and 'web-1' share nothing
    devices.rename(depl["acme"]["id"], dev_id, "WEB-1", "test")             # the runner reports the server's host name
    html = _page(client, signed_in)
    assert "data-join-suggestion" in html and "Is <strong>WEB-1</strong> the same machine as <strong>web-1</strong>?" in html
    assert html.count("data-machine=") == 1 and "data-also" not in html     # asking is not joining
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT server_id FROM device_tokens WHERE id = %s", (dev_id,))
        assert cur.fetchone()["server_id"] is None
    r = client.post(f"/acme/machines/{dev_id}/join", data={"server_id": sid}, headers=_h(signed_in), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/acme/machines")
    runners, servers = _page(client, signed_in, "?tab=runners"), _page(client, signed_in, "?tab=servers")
    # one machine, listed on BOTH tabs with a mark, each tab showing its own block
    assert runners.count("data-machine=") == 1 and servers.count("data-machine=") == 1 and "data-join-suggestion" not in runners
    assert 'data-also="server"' in runners and "also a server" in runners and 'data-block="jobs"' in runners and "data-unjoin" in runners
    assert 'data-also="runner"' in servers and "also a runner" in servers and 'data-block="deploys"' in servers
    r = client.post(f"/acme/machines/{dev_id}/unjoin", data={"tab": "runners"}, headers=_h(signed_in), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/acme/machines?tab=runners#dev-%d" % dev_id
    assert "data-also" not in _page(client, signed_in, "?tab=runners") and "data-also" not in _page(client, signed_in, "?tab=servers")


def test_the_address_a_runner_connects_from_also_suggests(depl):
    server = server_access.get(depl["acme"]["id"], depl["server_id"])
    dev = {"id": 1, "name": "desktop", "server_id": None, "last_ip": server["address"]}
    assert devices.suggest_server(dev, [server])["id"] == server["id"]
    assert devices.suggest_server({**dev, "last_ip": "192.0.2.9"}, [server]) is None
    assert devices.suggest_server({**dev, "server_id": 5}, [server]) is None                      # already joined: nothing to ask


def test_only_the_owner_joins_and_the_server_must_be_the_accounts(depl, client, signed_in):
    other = server_access.add(depl["beta"], "beta-box", "203.0.113.50", 22, "mallen", "owner")
    r = client.post(f"/acme/machines/{_device_id(depl)}/join", data={"server_id": other["id"]}, headers=_h(signed_in),
                    follow_redirects=False)
    assert r.status_code in (303, 404)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT server_id FROM device_tokens WHERE id = %s", (_device_id(depl),))
        assert cur.fetchone()["server_id"] is None                                  # another account's server never joins


def test_role_filters_and_the_counts(depl, client, signed_in):
    # #838: the jobs/deploys role filter became the tabs; the old ?role= links still arrive and pick the tab
    html = _page(client, signed_in, "?role=jobs")
    assert 'data-role-chip="jobs"' in html and 'data-block="deploys"' not in html and 'data-tab="runners"' in html
    html = _page(client, signed_in, "?role=deploys")
    assert 'data-block="deploys"' in html and 'data-block="jobs"' not in html and 'data-tab="servers"' in html
    html = _page(client, signed_in, "?tab=servers&role=attention")
    assert "data-none-match" in html and "data-machine=" not in html                 # nothing needs attention yet
    with get_cursor() as cur:
        cur.execute("UPDATE commissioned_servers SET probe_misses = 3, unreachable_since = now() - interval '12 minutes' WHERE id = %s",
                    (depl["server_id"],))
    html = _page(client, signed_in, "?tab=servers&role=attention")
    assert 'data-state="not_answering"' in html and 'data-block="jobs"' not in html
    for key in ("all", "attention"):
        assert f'data-filter="{key}"' in html
    for gone in ("jobs", "deploys"):
        assert f'data-filter="{gone}"' not in html
    assert "data-saved-filters" in html and 'data-page="machines"' in html            # saveable per person (#772)


def test_the_role_filter_is_a_saveable_filter(depl, client, signed_in):
    h = _h(signed_in)
    r = client.post("/all/filters", data={"page": "machines", "name": "Servers only", "data": '{"role": "deploys", "junk": "x"}',
                                          "is_default": "1"}, headers=h)
    assert r.status_code == 201 and r.json()["filter"]["data"] == {"role": "deploys"}           # only the page's own keys are kept
    bare = client.get("/acme/machines", headers=h, follow_redirects=False)                     # a bare arrival opens the default
    assert bare.status_code == 302 and bare.headers["location"] == "/acme/machines?tab=servers"
    ok = client.get("/acme/machines?all=1", headers=h, follow_redirects=False)                 # "All" is a choice, never redirected
    assert ok.status_code == 200 and 'data-block="jobs"' in ok.text                            # a saved "Servers" lands on that tab; ?all=1 on Runners


def test_old_pages_redirect_here(depl, client, signed_in):
    h = _h(signed_in)
    r = client.get("/acme/servers", headers=h, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/acme/machines?tab=servers"
    r = client.get("/acme/runners", headers=h, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/acme/machines?tab=runners"


def test_labels_save_at_once_with_no_save_button(depl, client, signed_in):
    h, dev_id = _h(signed_in), _device_id(depl)
    html = _page(client, signed_in)
    assert "Save labels" not in html and "data-label-add" in html                      # a tag input, not a form with a button
    r = client.post(f"/acme/runners/{dev_id}/labels", data={"labels": "", "add": "Android-USB"}, headers=h, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/acme/runners/{dev_id}/labels", data={"labels": "android-usb", "add": "gpu"}, headers=h, follow_redirects=False)
    html = _page(client, signed_in)
    assert "data-label-remove" in html and ">android-usb <" in html and ">gpu <" in html
    r = client.post(f"/acme/runners/{dev_id}/labels", data={"labels": "gpu"}, headers=h, follow_redirects=False)   # the x on android-usb
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT labels FROM device_tokens WHERE id = %s", (dev_id,))
        assert cur.fetchone()["labels"] == ["gpu"]
    # the labels the profiles ask for are offered (android needs android-usb)
    assert '<option value="android-usb">needed by android</option>' in _page(client, signed_in)


def test_rename_and_revoke_sit_in_the_more_menu(depl, client, signed_in):
    h, dev_id = _h(signed_in), _device_id(depl)
    html = _page(client, signed_in)
    menu = html.split("data-more", 1)[1].split("</details>", 1)[0]
    assert "data-rename-form" in menu and "data-revoke" in menu
    assert client.post(f"/acme/machines/{dev_id}/rename", data={"name": "desk"}, headers=h, follow_redirects=False).status_code == 303
    assert 'data-machine-name>desk<' in _page(client, signed_in)
    r = client.post(f"/acme/machines/{dev_id}/rename", data={"name": "  "}, headers=h, follow_redirects=False)
    assert r.status_code == 400
    r = client.post(f"/acme/runners/{dev_id}/revoke", headers=h, follow_redirects=False)
    assert r.status_code == 303 and 'data-status="revoked"' in _page(client, signed_in)


def test_jobs_split_into_running_and_recent_and_orphaned_locks_are_a_banner(depl, client, signed_in):
    h = _h(signed_in)
    html = _page(client, signed_in)
    assert "Running and queued" in html and "data-recent-jobs" not in html
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, state FROM jobs")
        live = cur.fetchall()
    assert live and f'data-job="{live[0]["id"]}"' in html and 'data-live-jobs' in html            # the running job is on top
    assert "Fix the thing" in html and "acme-app" in html and "data-elapsed" in html              # the task title, its project, elapsed
    assert "data-job-cols" in html                                                                # fence / branch / attempts: a hidden column set
    assert "data-locks" not in html and "Locks" not in html                                       # no orphan: no banner
    _hold(depl["claim"], "deploy")
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'")
    jobs.Sweeper().run()
    html = _page(client, signed_in)
    assert "data-locks" in html and 'data-lock-state="orphaned"' in html and "data-clear-lock" in html
    assert "data-recent-jobs" in html                                                            # the failed job moved to the folded Recent


def test_empty_account_shows_the_how_to_open_inline(est, client, signed_in):
    html = client.get("/acme/machines", headers=_h(signed_in)).text
    assert "No runners enrolled yet" in html and 'data-section="howto"' in html and "crewboss-runner enrol" in html
    assert re.search(r'data-section="howto"[^>]*\bopen\b', html)                                  # open while there are no machines
    assert "data-add-machine" not in html and "+ Add a runner" in html                            # the combined sheet is gone (#838)


def test_each_tab_has_only_its_own_add_action(depl, client, signed_in):
    runners = _page(client, signed_in, "?tab=runners")
    assert 'data-add-btn="runner"' in runners and "+ Add a runner" in runners and "A PC that builds code: your laptop or desktop" in runners
    assert 'data-add-btn="server"' not in runners and "+ Add a server" not in runners and "data-add-server" not in runners
    assert "data-new-code-form" in runners
    servers = _page(client, signed_in, "?tab=servers")
    assert 'data-add-btn="server"' in servers and "+ Add a server" in servers and "Where your apps run and get deployed" in servers
    assert "data-add-server" in servers and 'data-add-btn="runner"' not in servers and "+ Add a runner" not in servers
    assert "data-new-code-form" not in servers and "data-live-jobs" not in servers and 'data-section="jobs"' not in servers   # jobs stay on Runners
    assert 'data-section="jobs"' in runners


def test_the_default_tab_is_runners_and_both_tabs_render(depl, client, signed_in):
    bare = _page(client, signed_in, "?all=1")
    assert 'data-tab="runners"' in bare and 'aria-selected="true"' in bare
    for key in ("runners", "servers"):
        html = _page(client, signed_in, f"?tab={key}")
        assert 'role="tablist"' in html and f'data-tab-panel="{key}"' in html and "data-deploy-flow" in html
        assert f'href="/acme/machines?tab={key}"' in html and 'role="tab"' in html           # each tab is a real link
        assert "Runners never touch servers." in html and "Only Crewboss deploys, and only after you say yes." in html
    assert _page(client, signed_in, "?tab=bogus").count('data-tab="runners"') == 1            # an unknown tab falls back


def test_a_server_link_sits_on_a_runner_card_and_joins_on_request(depl, client, signed_in):
    runners = _page(client, signed_in, "?tab=runners")
    assert "Also use this as a server" in runners and "data-link-server" in runners
    r = client.post(f"/acme/machines/{_device_id(depl)}/join", data={"server_id": depl["server_id"], "tab": "runners"},
                    headers=_h(signed_in), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/acme/machines?tab=runners#dev-%d" % _device_id(depl)
    r = client.post(f"/acme/machines/{_device_id(depl)}/unjoin", data={"tab": "javascript:x"}, headers=_h(signed_in), follow_redirects=False)
    assert r.headers["location"].startswith("/acme/machines#dev-")                                 # a tab value is only ever one of ours


def test_add_flows_land_on_their_tab(depl, client, signed_in):
    h = _h(signed_in)
    r = client.post("/acme/runners/codes", data={"mode": "subscription"}, headers=h)
    assert r.status_code == 200 and "data-new-code" in r.text and 'data-tab="runners"' in r.text and "data-sheet=\"runner\" data-add-panel open" in r.text
    r = client.post("/acme/servers", data={"name": "web02", "address": "-oProxyCommand=sh", "port": "22", "login_user": "mallen"}, headers=h)
    assert r.status_code == 400 and 'data-tab="servers"' in r.text and "data-add-error" in r.text and 'data-sheet="server" data-add-panel open' in r.text
    r = client.post("/acme/servers", data={"name": "web02", "address": "203.0.113.10", "port": "22", "login_user": "mallen"}, headers=h,
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/acme/machines?tab=servers&added=")


def test_pick_tab_defaults():
    from app.routers.machines_pages import pick_tab
    assert pick_tab("", "all", can_enrol=True, owner=True) == "runners"
    assert pick_tab("", "all", can_enrol=False, owner=True) == "servers"          # an owner who cannot enrol a runner starts on Servers
    assert pick_tab("", "all", can_enrol=False, owner=False) == "runners"
    assert pick_tab("servers", "jobs", can_enrol=True, owner=True) == "servers"   # an explicit tab wins over a legacy role
    assert pick_tab("", "deploys", can_enrol=True, owner=True) == "servers" and pick_tab("x", "jobs", True, True) == "runners"
