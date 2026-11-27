from app import repo
from app.auth import issue_token


def test_create_and_search_knowledge(project):
    repo.create_knowledge(project["id"], "decision", "Use Postgres 16", "because reasons",
                           ["ERP"], "meeting notes", False, "owner", summary="We use Postgres 16.")
    repo.create_knowledge(project["id"], "fact", "Xero tokens expire in 30 min", "",
                           ["ERP"], "", True, "system", summary="Refresh before they lapse.")

    results = repo.search_knowledge(project["id"], "Postgres")
    assert len(results) == 1
    assert results[0]["title"] == "Use Postgres 16"

    needing_review = repo.list_knowledge(project["id"], needs_review=True)
    assert len(needing_review) == 1
    assert needing_review[0]["title"] == "Xero tokens expire in 30 min"


def test_mark_reviewed_via_api(client, project):
    token = issue_token("bugs", "kb-writer")
    created = client.post("/api/knowledge", json={
        "project": project["slug"], "kind": "fact", "title": "Needs eyes", "summary": "Check this.", "needs_review": True,
    }, headers={"Authorization": f"Bearer {token}"}).json()
    assert created["needs_review"] is True

    patched = client.patch(f"/api/knowledge/{created['id']}", json={"needs_review": False},
                            headers={"Authorization": f"Bearer {token}"})
    assert patched.json()["needs_review"] is False


def test_knowledge_filtered_by_subproject(project):
    repo.create_knowledge(project["id"], "note", "AiOS note", "", ["AiOS"], "", False, "owner", summary="s")
    repo.create_knowledge(project["id"], "note", "ERP note", "", ["ERP"], "", False, "owner", summary="s")
    only_aios = repo.list_knowledge(project["id"], subproject="AiOS")
    assert [k["title"] for k in only_aios] == ["AiOS note"]
