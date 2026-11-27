"""The knowledge link graph (task #550, app/knowledge_graph.py): link extraction ([[wiki links]], #task references,
supersedes / superseded by, code skipped), resolution and broken links, orphans, stale entries, the "Linked from"
backlinks under an entry, the Knowledge page's ?view=graph, and GET /api/knowledge/graph (Bearer + project scope).
Read-only throughout: nothing here may change an entry."""
from datetime import datetime, timedelta, timezone

from app import knowledge_graph as kg
from app import repo
from app.auth import issue_token
from app.db import get_cursor


def _cookie(project):
    return {"Cookie": f"current_project={project['slug']}"}


def _entry(project, title, body="", kind="fact", summary="A summary of it."):
    return repo.create_knowledge(project["id"], kind, title, body, [], "", False, "owner", summary=summary)


def _task(project, title="A task", body="", links=None, status=None):
    return repo.create_task(project["id"], title, body, "should", "feature", [], links or {}, "owner", status=status)


def _age(kid, days):
    with get_cursor() as cur:
        cur.execute("UPDATE knowledge SET updated_at = now() - %s * interval '1 day' WHERE id = %s", (days, kid))


def _nodes(graph):
    return {n["id"]: n for n in graph["nodes"]}


def _edges(graph):
    return {(e["source"], e["target"], e["rel"]) for e in graph["edges"]}


# --- extraction (pure) ---------------------------------------------------------------------------------------------

def test_extract_wiki_links_with_label_heading_and_dedup():
    found = kg.extract_links("See [[Xero tokens expire]] and [[xero-tokens-expire|the Xero note]], "
                             "[[Theme page#colours]] and [[ Theme   page ]] again.")
    assert found["wiki"] == [("Xero tokens expire", "link"), ("Theme page", "link")]


def test_extract_task_refs_not_entities_urls_or_words():
    found = kg.extract_links("Done in #548 (task #12), see #548 again. Not &#38; nor /page#77 nor abc#5 nor ##9 nor #0.")
    assert found["tasks"] == [548, 12]


def test_extract_skips_code_spans_and_fences():
    body = "Real [[One]] and #1.\n\n`[[Two]] #2`\n\n```\n[[Three]] #3\n```\n"
    found = kg.extract_links(body)
    assert found["wiki"] == [("One", "link")] and found["tasks"] == [1]


def test_extract_supersedes_relations():
    found = kg.extract_links("This supersedes [[Old plan]]. It was superseded by: [[New plan]]; "
                             "reverses [[Rule A]]; replaced by [[Rule B]]; see [[Plain]].")
    assert found["wiki"] == [("Old plan", "supersedes"), ("New plan", "superseded_by"), ("Rule A", "supersedes"),
                             ("Rule B", "superseded_by"), ("Plain", "link")]


def test_link_key_ignores_case_and_punctuation():
    assert kg.link_key("Xero tokens: Expire!") == "xero-tokens-expire" == kg.link_key("xero-tokens-expire")


def test_compute_pure_orphan_broken_and_stale():
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    project = {"id": 1, "slug": "p"}

    def row(i, title, body="", days=1):
        return {"id": i, "project_id": 1, "project_slug": "p", "kind": "fact", "title": title, "summary": "",
                "body": body, "archive_reason": "", "archived_at": None, "subprojects": [],
                "updated_at": now - timedelta(days=days)}

    entries = [row(1, "Alpha", "Links [[Beta]] and [[Nowhere]]"), row(2, "Beta"), row(3, "Lonely"),
               row(4, "Old one", days=120)]
    tasks = [{"id": 9, "title": "t", "body": "", "status": "doing", "links": {"knowledge": [4]}, "project_id": 1,
              "project_slug": "p"}]
    g = kg.compute(project, entries, tasks, now=now)
    assert ("k1", "k2", "link") in _edges(g) and ("t9", "k4", "task") in _edges(g)
    assert g["broken"] == [{"source": "k1", "target": "Nowhere"}]
    assert g["housekeeping"] == {"orphans": ["k3"], "broken": ["k1"], "stale": ["k4"]}


# --- the graph over the database -----------------------------------------------------------------------------------

def test_graph_nodes_edges_tasks_and_broken(project):
    a = _entry(project, "Xero tokens expire", "Refresh them. See [[Theme page]] and [[Gone page]]. Built in #TASK.")
    b = _entry(project, "Theme page", kind="note")
    t = _task(project, "Build the refresh")
    repo.update_knowledge(a["id"], {"body": f"See [[theme-page]] and [[Gone page]]. Built in #{t['id']}."}, "owner")
    g = kg.build(project)
    nodes = _nodes(g)
    assert nodes[f"k{a['id']}"]["kind"] == "fact" and nodes[f"k{b['id']}"]["kind"] == "note"
    assert nodes[f"t{t['id']}"]["type"] == "task"
    assert nodes[f"t{t['id']}"]["url"] == f"/{project['slug']}/tasks?open={t['id']}"
    assert (f"k{a['id']}", f"k{b['id']}", "link") in _edges(g)
    assert (f"k{a['id']}", f"t{t['id']}", "task") in _edges(g)
    assert g["broken"] == [{"source": f"k{a['id']}", "target": "Gone page"}]
    assert g["housekeeping"]["broken"] == [f"k{a['id']}"]
    assert sorted(g["kinds"]) == ["fact", "note"]


def test_missing_task_number_draws_nothing(project):
    a = _entry(project, "Mentions a ghost", "Fixed in #99999.")
    g = kg.build(project)
    assert not [n for n in g["nodes"] if n["type"] == "task"] and not g["edges"]
    assert g["housekeeping"]["orphans"] == [f"k{a['id']}"]


def test_orphans_are_live_entries_with_no_links_either_way(project):
    a = _entry(project, "Links out", "To [[Linked to]].")
    b = _entry(project, "Linked to")
    c = _entry(project, "Alone")
    d = _entry(project, "Alone but archived")
    repo.archive_knowledge(d["id"], "not needed", "owner")
    t_ref = _entry(project, "Only a task links me")
    _task(project, "Uses it", links={"knowledge": [t_ref["id"]]})
    g = kg.build(project)
    assert g["housekeeping"]["orphans"] == [f"k{c['id']}"]
    assert _nodes(g)[f"k{d['id']}"]["archived"] is True  # archived: drawn (greyed), never listed as an orphan
    assert not _nodes(g)[f"k{a['id']}"]["orphan"] and not _nodes(g)[f"k{b['id']}"]["orphan"]


def test_stale_means_old_and_referenced_by_an_open_task(project):
    old_used = _entry(project, "Old but used")
    old_unused = _entry(project, "Old and unused")
    fresh_used = _entry(project, "Fresh and used")
    old_done = _entry(project, "Old, only a done task")
    _task(project, "Open one", body="Read [[Old but used]] first.")
    _task(project, "Open two", links={"knowledge": [fresh_used["id"]]})
    _task(project, "Finished", links={"knowledge": [old_done["id"]]}, status="done")
    for e in (old_used, old_unused, old_done):
        _age(e["id"], 120)
    g = kg.build(project)
    assert g["housekeeping"]["stale"] == [f"k{old_used['id']}"]
    assert _nodes(g)[f"k{old_used['id']}"]["open_tasks"]


def test_superseded_entries_are_marked_and_the_edge_points_from_the_newer(project):
    old = _entry(project, "Old remote access plan")
    new = _entry(project, "Remote access plan v2", "This supersedes [[Old remote access plan]].")
    older = _entry(project, "First draft", "Superseded by [[Old remote access plan]].")
    g = kg.build(project)
    nodes = _nodes(g)
    assert nodes[f"k{old['id']}"]["superseded"] and nodes[f"k{older['id']}"]["superseded"]
    assert not nodes[f"k{new['id']}"]["superseded"]
    assert (f"k{new['id']}", f"k{old['id']}", "supersedes") in _edges(g)
    assert (f"k{old['id']}", f"k{older['id']}", "supersedes") in _edges(g)


def test_links_resolve_own_entries_before_inherited_and_live_before_archived(project):
    acct = repo.create_project("acct", "Acct", "internal", "owner")
    from app import tree
    tree.set_place(acct["id"], "owner", None, "account")
    tree.set_place(project["id"], "owner", acct["id"])
    shared_one = repo.create_knowledge(acct["id"], "fact", "Shared fact", "", [], "", False, "owner", summary="s.")
    dup_old = _entry(project, "Same name")
    repo.archive_knowledge(dup_old["id"], "replaced", "owner")
    dup_live = _entry(project, "Same name")
    src = _entry(project, "Source", "See [[Shared fact]] and [[Same name]].")
    g = kg.build(project)
    assert (f"k{src['id']}", f"k{shared_one['id']}", "link") in _edges(g)
    assert (f"k{src['id']}", f"k{dup_live['id']}", "link") in _edges(g)
    assert _nodes(g)[f"k{shared_one['id']}"]["inherited"] is True
    assert _nodes(g)[f"k{shared_one['id']}"]["url"].startswith("/acct/knowledge?open=")


def test_another_accounts_task_is_never_a_node(project):
    other = repo.create_project("other-acct", "Other", "client", "owner")
    from app import tree
    tree.set_place(other["id"], "owner", None, "account")
    child = repo.create_project("other-child", "Other child", "client", "owner")
    tree.set_place(child["id"], "owner", other["id"])
    secret = _task(child, "Other account's secret")
    _entry(project, "Probe", f"Is there a #{secret['id']}?")
    g = kg.build(project)
    assert not [n for n in g["nodes"] if n["type"] == "task"]
    assert "secret" not in str(g)


def test_cache_rebuilds_after_an_edit_and_archive(project):
    a = _entry(project, "A", "Nothing yet.")
    b = _entry(project, "B")
    first = kg.build(project)
    assert kg.build(project) is first  # unchanged rows: the cached graph
    repo.update_knowledge(a["id"], {"body": "Now [[B]]."}, "owner")
    second = kg.build(project)
    assert second is not first and (f"k{a['id']}", f"k{b['id']}", "link") in _edges(second)
    repo.archive_knowledge(b["id"], "done with", "owner")
    assert _nodes(kg.build(project))[f"k{b['id']}"]["archived"] is True


def test_linked_from_lists_entries_both_supersede_directions_and_open_tasks(project):
    target = _entry(project, "Target")
    linker = _entry(project, "Linker", "See [[Target]].")
    newer = _entry(project, "Newer", "Supersedes [[Target]].")
    wrote_by = _entry(project, "Older", "Superseded by [[Target]].")
    t = _task(project, "Open work", links={"knowledge": [target["id"]]})
    _task(project, "Closed work", links={"knowledge": [target["id"]]}, status="done")
    back = kg.linked_from(kg.build(project), target["id"])
    rels = {n["title"]: n["rel"] for n in back["entries"]}
    assert rels == {"Linker": "link", "Newer": "supersedes", "Older": "superseded_by"}
    assert [n["ref"] for n in back["tasks"]] == [t["id"]]
    assert kg.linked_from(kg.build(project), linker["id"]) == {"entries": [], "tasks": []}
    assert newer and wrote_by


# --- pages ---------------------------------------------------------------------------------------------------------

def test_card_body_shows_linked_from(client, project):
    target = _entry(project, "Target entry")
    _entry(project, "Points at it", "See [[Target entry]].")
    part = client.get(f"/test-proj/knowledge/{target['id']}/body", headers=_cookie(project)).text
    assert "data-backlinks" in part and "Linked from" in part and "Points at it" in part
    lonely = _entry(project, "Nobody links me")
    part = client.get(f"/test-proj/knowledge/{lonely['id']}/body", headers=_cookie(project)).text
    assert "Nothing links here yet" in part and "[[Nobody links me]]" in part


def test_opened_card_on_the_list_shows_linked_from(client, project):
    target = _entry(project, "Target entry")
    _entry(project, "Points at it", "See [[Target entry]].")
    html = client.get(f"/test-proj/knowledge?open={target['id']}", headers=_cookie(project)).text
    assert "data-backlinks" in html and "Points at it" in html


def test_graph_view_renders_data_lens_and_housekeeping(client, project):
    a = _entry(project, "Has a broken link", "See [[Nothing by this name]].")
    _entry(project, "An orphan </script><b>x</b>")
    html = client.get("/test-proj/knowledge?view=graph", headers=_cookie(project)).text
    assert "data-knowledge-graph" in html and "data-kg-data" in html and "knowledge-graph.js" in html
    assert 'name="kg-lens"' in html and "Broken links" in html and "Orphans" in html and "Stale" in html
    assert "[[Nothing by this name]]" in html and f"/test-proj/knowledge?open={a['id']}" in html
    assert "</script><b>" not in html  # entry text inside the inline JSON is escaped
    assert 'aria-current="page" data-view-graph' in html
    listed = client.get("/test-proj/knowledge", headers=_cookie(project)).text
    assert "data-knowledge-graph" not in listed and "data-view-graph" in listed


def test_graph_view_of_an_empty_project(client, project):
    html = client.get("/test-proj/knowledge?view=graph", headers=_cookie(project)).text
    assert "nothing to draw" in html


def test_graph_script_is_served(client):
    r = client.get("/static/js/knowledge-graph.js")
    assert r.status_code == 200 and "kg-lens" in r.text


# --- REST ----------------------------------------------------------------------------------------------------------

def test_api_graph_needs_a_token_and_reads_the_sessions_project(client, project):
    assert client.get("/api/knowledge/graph").status_code == 401
    a = _entry(project, "One", "To [[Two]].")
    b = _entry(project, "Two")
    tok = issue_token("bugs", "kg-reader")  # legacy project = the `project` fixture's
    r = client.get("/api/knowledge/graph", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"] == project["slug"]
    assert {"source": f"k{a['id']}", "target": f"k{b['id']}", "rel": "link", "by": f"k{a['id']}"} in body["edges"]
    assert set(body["housekeeping"]) == {"orphans", "broken", "stale"}
    named = client.get(f"/api/knowledge/graph?project={project['slug']}", headers={"Authorization": f"Bearer {tok}"})
    assert named.json()["edges"] == body["edges"]


def test_api_graph_of_another_project_is_a_404(client, project):
    other = repo.create_project("proj-other", "Other", "client", "owner", session_roles=["all"])
    _entry(other, "Other secret")
    tok = issue_token("bugs", "kg-reader")
    r = client.get("/api/knowledge/graph?project=proj-other", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 404 and "Other secret" not in r.text


def test_reading_the_graph_changes_nothing(client, project):
    a = _entry(project, "Stays as it is", "Links [[Missing]] and #1.")
    before = repo.get_knowledge(a["id"])
    client.get("/test-proj/knowledge?view=graph", headers=_cookie(project))
    client.get(f"/test-proj/knowledge/{a['id']}/body", headers=_cookie(project))
    after = repo.get_knowledge(a["id"])
    assert (before["updated_at"], before["body"], before["updated_by"]) == \
        (after["updated_at"], after["body"], after["updated_by"])
