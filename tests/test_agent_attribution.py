"""Task #167: the dashboard is multi-AI. A session token records WHICH AI holds it, so Claude Code and the
Gemini CLI can both drive it and every write is honestly attributed (actor '<agent>:<stream>'). `agent` is
attribution ONLY — a gemini token has exactly the same reach and isolation as a claude one."""
import pytest

from app import mcp_server, repo
from app.auth import actor_for, hash_token, issue_token, new_token, resolve_token
from app.db import get_cursor
from app.templating import _actor_label


# --- the token carries its agent; actor_for reflects it -----------------------------------------------------

def test_default_token_is_claude(project):
    tok = issue_token("bugs", "legacy")
    session = resolve_token(tok)
    assert session["agent"] == "claude"
    assert actor_for(session) == "claude:bugs"


def test_gemini_token_actor(project):
    tok = issue_token("bugs", "gem", agent="gemini")
    session = resolve_token(tok)
    assert session["agent"] == "gemini"
    assert actor_for(session) == "gemini:bugs"


def test_legacy_row_inserted_without_agent_reads_as_claude(project):
    """A token row written the pre-0021 way (no agent column) takes the DEFAULT 'claude' and resolves cleanly."""
    tok = new_token()
    with get_cursor() as cur:  # an insert that names no agent, as the old code did
        cur.execute("INSERT INTO session_tokens (stream, name, token_hash) VALUES ('reviewer', 'legacy', %s)",
                    (hash_token(tok),))
    session = resolve_token(tok)
    assert session["agent"] == "claude"
    assert actor_for(session) == "claude:reviewer"


def test_unknown_agent_is_refused(project):
    with pytest.raises(ValueError):
        issue_token("bugs", "x", agent="skynet")


def test_db_check_rejects_a_bad_agent(project):
    with pytest.raises(Exception):
        with get_cursor() as cur:
            cur.execute("INSERT INTO session_tokens (stream, name, token_hash, agent) VALUES "
                        "('bugs', 'bad', 'deadbeef', 'skynet')")


# --- a gemini token's writes are attributed to gemini -------------------------------------------------------

def test_gemini_task_create_and_comment_and_knowledge(project, mcp_caller):
    mcp_caller(issue_token("features", "gem-feat", agent="gemini"))
    task = mcp_server.add_task(title="Gemini built this", stream="feature", project=project["slug"])
    assert task["created_by"] == "gemini:features"

    comment = mcp_server.comment_task(task["id"], "and commented")
    assert comment["author"] == "gemini:features"

    k = mcp_server.add_knowledge(title="Gemini note", summary="s", project=project["slug"])
    assert k["created_by"] == "gemini:features"


def test_claude_token_still_attributes_to_claude(project, mcp_caller):
    mcp_caller(issue_token("features", "claude-feat"))
    task = mcp_server.add_task(title="Claude built this", stream="feature", project=project["slug"])
    assert task["created_by"] == "claude:features"


# --- the comment author-equality check accepts the gemini actor (REST) --------------------------------------

@pytest.fixture
def task(project):
    return repo.create_task(project["id"], "Talk", "", "should", "review", [], {}, "system")


def test_rest_comment_accepts_matching_gemini_author(client, task):
    tok = issue_token("reviewer", "gem-rev", agent="gemini")
    resp = client.post(f"/api/tasks/{task['id']}/comments",
                       json={"author": "gemini:reviewer", "body": "ok"},
                       headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 201 and resp.json()["author"] == "gemini:reviewer"


def test_rest_comment_refuses_a_different_author_for_a_gemini_token(client, task):
    tok = issue_token("reviewer", "gem-rev", agent="gemini")
    resp = client.post(f"/api/tasks/{task['id']}/comments",
                       json={"author": "claude:reviewer", "body": "spoof"},
                       headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 403


# --- agent is attribution only: reach and isolation are unchanged -------------------------------------------

def test_gemini_token_is_scoped_to_its_own_project_exactly_like_claude(mcp_caller):
    a = repo.create_project("proj-a", "Project A", "internal", "owner", short_name="pa", session_roles=["all"])
    b = repo.create_project("proj-b", "Project B", "client", "owner", short_name="pb", session_roles=["all"])
    bt = repo.create_task(b["id"], "B secret", "", "must", "bug", [], {}, "owner")
    # a gemini token of project A cannot touch project B — same NotFound a claude token gets
    mcp_caller(issue_token("all", "gem-a", project_id=a["id"], agent="gemini"))
    with pytest.raises(repo.NotFound):
        mcp_server.get_task(bt["id"])
    # and it works normally within its own project
    mine = mcp_server.add_task(title="A task", stream="bug", project="proj-a")
    assert mine["project_id"] == a["id"] and mine["created_by"] == "gemini:all"


def test_gemini_session_permissions_are_by_stream_not_agent(mcp_caller):
    """_session_stream must parse a gemini actor so stream-gated permissions behave identically."""
    p = repo.create_project("proj-s", "S", "internal", "owner", short_name="ps", session_roles=["all"])
    t = repo.create_task(p["id"], "a tweak", "", "could", "tweak", [], {}, "owner", assignee="owner")
    mcp_caller(issue_token("all", "gem", project_id=p["id"], agent="gemini"))
    # an 'all' session (gemini) may hand any stream's task of its project to the AI, like claude's
    assert mcp_server.assign_task(t["id"], "ai")["assignee"] == "ai"
    assert repo._session_stream("gemini:tweaks") == "tweaks"
    assert repo._session_stream("claude:bugs") == "bugs"
    assert repo._session_stream("client:Jo") is None


# --- the UI renders a gemini author cleanly (honest attribution) --------------------------------------------

def test_actor_label_names_each_ai():
    assert _actor_label("claude:bugs") == "AI · bugs"
    assert _actor_label("gemini:bugs") == "Gemini · bugs"
    assert _actor_label("owner") == "You"
    assert _actor_label("claude:overseer") == "dash (overseer)"
    assert _actor_label("client:Jo") == "Jo (client)"


def test_tasks_page_renders_a_gemini_author(client, project, mcp_caller):
    mcp_caller(issue_token("features", "gem", agent="gemini"))
    task = mcp_server.add_task(title="Gemini task", stream="feature", project=project["slug"])
    mcp_server.comment_task(task["id"], "by gemini")
    resp = client.get(f"/{project['slug']}/tasks")
    assert resp.status_code == 200
    assert "gemini:features" not in resp.text  # a gemini author never leaks as a raw actor string


# --- the tokens CLI can mint a gemini token -----------------------------------------------------------------

def test_tokens_cli_mints_a_gemini_token(capsys, project):
    rc = tokens_main(["create", "--project", project["slug"], "--stream", "bugs", "--agent", "gemini"])
    assert rc == 0
    out = capsys.readouterr().out
    token = [l for l in out.splitlines() if l.startswith("cb_")][0]
    session = resolve_token(token)
    assert session["agent"] == "gemini"
    assert actor_for(session) == "gemini:bugs"


def tokens_main(argv):
    from app import tokens
    return tokens.main(argv)
