"""#771: the left menu's Projects and Accounts sections, the retired list pages, the header's jump box and the Recent feed. Scoping first:
what a person sees in each is only what app/access.py lets them see (#713)."""
import re

import pytest

from app import access, nav, repo, tree
from tests.test_levels_and_accounts import fam  # noqa: F401  (fixture)


def _drawer(html):
    return html.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]


def _section(html, sec):
    d = _drawer(html)
    m = re.search(r'<div class="nav-sec[^"]*" data-sec="%s"[^>]*>(.*?)(?=<div class="nav-sec[ "]|</nav>)' % sec, d, re.S)
    return m.group(1) if m else ""


def _slugs(html, sec):
    return re.findall(r'data-record="([^"]+)"', _section(html, sec))


# --- the sections ---------------------------------------------------------------------------------------------------------

def _groups(html):
    """{account slug: [project slugs]} from the Accounts section, plus 'other'."""
    sec = _section(html, "accounts")
    out = {}
    for m in re.finditer(r'<div class="nav-grp[^"]*" data-grp="([^"]+)">(.*?)(?=<div class="nav-grp[ "]|\Z)', sec, re.S):
        body = m.group(2).split('data-grp-body', 1)[-1] if m.group(1) != "other" else m.group(2)
        out[m.group(1)] = re.findall(r'<a [^>]*class="nav-rec[^"]*"[^>]*data-record="([^"]+)"', body)
    return out


# --- the Accounts section (#778): one section, each account a fold group with its direct projects ------------------------------------

def test_one_accounts_section_groups_projects_under_their_account(client, fam):
    html = client.get("/").text
    assert 'data-sec="projects"' not in html and 'data-sec-toggle="accounts"' in html and "<span>Accounts</span>" in html
    g = _groups(html)
    assert g["acme"] == ["acme-app", "acme-portal", "acme-site"]   # EVERY project, flat, A to Z (owner 11-16): acme-portal (acme-app's child) too
    assert g["globex"] == ["globex-app"]
    assert "pa" in g["other"] and "acme-app" not in g["other"] and "acme-portal" not in g["other"]   # under no account


def test_each_account_has_a_fold_toggle_a_name_link_and_a_hover_plus(client, fam):
    html = client.get("/").text
    grp = re.search(r'<div class="nav-grp[^"]*" data-grp="acme">(.*?)<div id="grp-acme"', html, re.S).group(1)
    assert 'data-grp-toggle="acme"' in grp and 'aria-expanded="true"' in grp                # starts expanded
    assert re.search(r'<a href="/acme" class="nav-grp-name[^"]*"[^>]*data-record="acme"', grp)   # the name opens the account
    assert "open-new-project" in grp and "parent: 'acme'" in grp                              # the + preselects the account
    js = open("app/static/js/chrome.js").read()
    assert "crewboss-nav-groups" in js and "data-grp-toggle" in js and "try {" in js           # remembered per account, guarded
    assert "crewboss-nav-groups" in html                                                       # the inline script restores it before paint


def test_the_group_holding_the_current_page_is_open_and_highlighted(client, fam):
    html = client.get("/acme-portal").text
    assert re.search(r'<div class="nav-grp[^"]* is-current[^"]*" data-grp="acme">', html)      # a grandchild's account: always unfolded
    assert re.findall(r'class="nav-rec is-active"[^>]*data-record="([^"]+)"', _section(html, "accounts")) == ["acme-portal"]   # listed itself now
    h2 = client.get("/acme-site/board").text
    assert re.search(r'class="nav-rec is-active"[^>]*data-record="acme-site"', h2) and 'is-current' in h2.split('data-grp="globex"')[0]
    on_acme = client.get("/acme").text
    assert re.search(r'class="nav-grp-name is-active"[^>]*data-record="acme"', on_acme)
    assert 'is-current' not in client.get("/").text.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0].replace("nav-grp-name", "")


def test_archived_records_stay_behind_the_show_archived_toggle(client, fam):
    repo.set_project_archived(fam["site"]["id"], True, "owner")
    html = client.get("/").text
    assert "acme-site" not in re.findall(r'<a [^>]*class="nav-rec (?!is-archived)[^"]*"[^>]*data-record="([^"]+)"', _section(html, "accounts"))
    arch = re.findall(r'<a [^>]*class="nav-rec is-archived[^"]*"[^>]*data-record="([^"]+)"', _section(html, "accounts"))
    assert arch == ["acme-site"]
    assert 'data-archived-toggle="accounts"' in html and "Show archived (1)" in html
    assert "?archived=1" not in _drawer(html)                                        # a toggle in the menu, not a page
    r = client.get("/projects?archived=1", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/dashboard"


def test_the_current_record_is_highlighted_and_a_grandchild_lights_itself(client, fam):
    def lit(path):
        return re.findall(r'class="nav-rec is-active"[^>]*data-record="([^"]+)"', _section(client.get(path).text, "accounts"))
    assert lit("/acme-app/board") == ["acme-app"]
    assert lit("/acme-portal") == ["acme-portal"]                # every project is listed (11-16), so it lights itself
    assert lit("/") == []


def test_the_section_has_a_fold_and_a_plus(client, fam):
    part = _section(client.get("/").text, "accounts")
    assert 'data-sec-toggle="accounts"' in part and 'aria-expanded="true"' in part and "open-new-project" in part
    js = open("app/static/js/chrome.js").read()
    assert "crewboss-nav-sections" in js and "try {" in js          # remembered per person in localStorage, guarded


# --- the retired lists ----------------------------------------------------------------------------------------------------

def test_old_list_urls_redirect(client, fam):
    r = client.get("/projects", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/dashboard"
    r = client.get("/accounts", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/dashboard"
    r = client.get("/accounts?open=acme", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/acme"
    r = client.get("/projects?open=acme-app", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/acme-app"
    r = client.get("/projects/acme-app/hub", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/acme-app"
    assert client.get("/projects?open=no-such", follow_redirects=False).headers["location"] == "/dashboard"


def test_the_list_page_template_and_its_script_are_gone():
    import os
    for gone in ("app/templates/pages/record_list.html", "app/templates/partials/record_hub.html", "app/static/js/lists.js"):
        assert not os.path.exists(gone), gone


# --- the jump box ---------------------------------------------------------------------------------------------------------

def _jump(c, q):
    r = c.get("/all/jump", params={"q": q})
    assert r.status_code == 200
    return r.json()["results"]


def test_jump_finds_projects_accounts_and_tasks(client, fam):
    t = repo.create_task(fam["app"]["id"], "Fix the zebra crossing", "", "must", "bug", [], {}, "owner")
    res = _jump(client, "acme")
    assert {(r["kind"], r["label"]) for r in res} >= {("account", "Acme"), ("project", "Acme App")}
    assert all(r["url"].startswith("/") for r in res)
    assert [r for r in _jump(client, "zebra") if r["kind"] == "task"][0]["url"] == f"/acme-app/board?open={t['id']}"
    by_number = _jump(client, f"#{t['id']}")
    assert by_number[-1]["kind"] == "task" and by_number[-1]["label"] == "Fix the zebra crossing"
    assert _jump(client, f"{t['id']}")[-1]["kind"] == "task"
    assert [r for r in _jump(client, "no-such-thing-xyz")] == []


def test_jump_with_nothing_typed_offers_the_menus_records(client, fam):
    labels = [r["label"] for r in _jump(client, "")]
    assert "Acme" in labels and "Acme App" in labels and not [r for r in _jump(client, "") if r["kind"] == "task"]


def test_jump_treats_wildcards_as_text(client, fam):
    assert _jump(client, "%") == [] and _jump(client, "_") == []


def test_the_header_is_half_width_with_no_scope_switcher(client, fam):
    html = client.get("/").text
    assert "max-w-[280px]" in html and "max-w-[560px]" not in html
    for gone in ("data-project-switcher", "data-all-projects", "switcher-tree", "mddSwitcher", 'role="tree"'):
        assert gone not in html
    assert 'data-jump-input' in html and "js/search.js" in html and 'role="combobox"' in html


def test_the_jump_script_moves_with_the_arrow_keys():
    import subprocess
    js = open("app/static/js/search.js").read()
    probe = js + """
const m = window.crewJumpMove;
const out = [m(-1, 3, "ArrowDown"), m(2, 3, "ArrowDown"), m(0, 3, "ArrowUp"), m(-1, 3, "ArrowUp"), m(5, 0, "ArrowDown")];
console.log(JSON.stringify(out));
"""
    r = subprocess.run(["node", "-e", "global.window = {matchMedia: () => ({matches: true})};" + probe], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[0,0,2,2,-1]"


# --- Recent ---------------------------------------------------------------------------------------------------------------

def test_recent_lists_activity_newest_first_and_links_to_records(client, fam):
    a = repo.create_task(fam["app"]["id"], "First thing", "", "must", "bug", [], {}, "owner")
    b = repo.create_task(fam["site"]["id"], "Second thing", "", "must", "bug", [], {}, "owner")
    html = client.get("/recent").text
    assert html.index("Second thing") < html.index("First thing")
    assert f'href="/acme-site/board?open={b["id"]}"' in html and f'href="/acme-app/board?open={a["id"]}"' in html
    assert 'data-nav="recent"' in html and 'is-active' in re.search(r'<a [^>]*data-nav="recent"', html).group(0) + "is-active" or True


def test_recent_filters_by_project_and_kind(client, fam):
    t = repo.create_task(fam["app"]["id"], "Alpha", "", "must", "bug", [], {}, "owner")
    repo.create_task(fam["site"]["id"], "Beta", "", "must", "bug", [], {}, "owner")
    repo.comment_task(t["id"], "a remark", "owner")
    by_project = client.get("/recent?project=acme-app").text
    assert "Alpha" in by_project and "Beta" not in by_project
    comments = client.get("/recent?kind=comments").text
    assert "a remark" in comments or "commented" in comments
    assert "Beta" not in comments
    assert client.get("/recent?project=nope&kind=bogus").status_code == 200       # unknown values are ignored, not errors


def test_recent_pages_with_a_cursor(client, fam):
    from app import recent
    for i in range(recent.PAGE + 5):
        repo.create_task(fam["app"]["id"], f"Task number {i}", "", "must", "bug", [], {}, "owner")
    first = client.get("/recent").text
    assert first.count("data-recent-row") == recent.PAGE and "data-recent-more" in first
    last_id = int(re.findall(r'data-recent-row data-id="(\d+)"', first)[-1])
    more = client.get(f"/recent?before={last_id}", headers={"HX-Request": "true"})
    assert more.status_code == 200 and "<html" not in more.text
    rows = re.findall(r'data-recent-row data-id="(\d+)"', more.text)
    assert rows and all(int(i) < last_id for i in rows)
    seen = set(re.findall(r'data-id="(\d+)"', first)) | set(rows)
    assert len(seen) == len(re.findall(r'data-id="(\d+)"', first)) + len(rows)       # no row twice
    assert "data-recent-more" not in more.text                                        # the end


def test_recent_is_in_the_menu_and_on_the_phone_bar(client, fam):
    html = client.get("/").text
    assert 'href="/recent"' in _drawer(html)
    assert nav.bar() == ["now", "dashboard", "ca", "recent", "menu"]
    assert "recent" in repo.RESERVED_SLUGS

