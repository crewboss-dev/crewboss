"""CA parity with the ERP PA (task #727): uploads (scoping, sniffing, caps, the 90-day purge), the runner fetching them over the socket,
the speech provider (hidden when unset, a short-lived signed address when set), chat history (own conversations only, search, paging),
the linked-item chip's server side (visibility per kind, unlink, many chats per item), the Discussions section (soft link, no content,
no admin read) and the agent <-> discussion link. The runner's content blocks are tests/test_ca_runner.py."""
import json
import struct
import zlib

import pytest

from app import ca, ca_files, config, jobs, speech
from app.db import get_cursor
from app.repo import BadRequest, NotFound

from test_ca import _ca_tables_clean, _say, _start, cw  # noqa: F401  (fixtures + helpers)
from test_devices import _user, est, signed_in  # noqa: F401
from test_runner_ws import Runner, _next
from test_web_auth import auth_on  # noqa: F401


def _png(w=4, h=4):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _up(cw, conv, name, data, who="bob"):
    return cw["client"].post(f"/ca/conversations/{conv}/uploads?name={name}", content=data, headers={**cw[who + "_h"], "content-type": "application/octet-stream"})


@pytest.fixture
def people(cw):
    """Bob = the instance owner (an administrator); Alice and Carol = designers, both members of acme-app (so all can see its task)."""
    app_id = cw["acme_app"]["id"] if isinstance(cw["acme_app"], dict) else cw["acme_app"]
    cw["carol"] = _user("cccccccc-0000-4000-8000-000000000003", "Carol")
    with get_cursor() as cur:
        cur.execute("UPDATE users SET role = 'owner', is_owner = true WHERE id = %s", (cw["bob"]["id"],))
        for u in (cw["alice"], cw["carol"]):
            cur.execute("UPDATE users SET role = 'designer', is_owner = false WHERE id = %s", (u["id"],))
            cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'member')", (app_id, u["id"]))
    return cw


# --- uploads -------------------------------------------------------------------------------------------------------------

def test_upload_types_are_decided_by_content_and_caps_hold(cw):
    conv = _start(cw)
    ok = _up(cw, conv, "shot.png", _png())
    assert ok.status_code == 200 and ok.json()["kind"] == "image" and ok.json()["mime"] == "image/png"
    assert _up(cw, conv, "notes.md", b"# from another AI\nhello").json()["kind"] == "text"
    assert _up(cw, conv, "doc.pdf", b"%PDF-1.4\n%fake").json()["kind"] == "pdf"
    # the NAME says png, the bytes say text/exe: refused (not text unless .md/.txt, and never an image by name)
    assert _up(cw, conv, "evil.png", b"MZ\x90\x00 not an image").status_code == 400
    assert _up(cw, conv, "run.sh", b"#!/bin/sh\nrm -rf /").status_code == 400
    assert _up(cw, conv, "bin.txt", b"abc\x00def").status_code == 400                     # a NUL byte: not text
    assert _up(cw, conv, "latin.txt", "caf\xe9".encode("latin-1")).status_code == 400       # not UTF-8
    assert _up(cw, conv, "svg.png", b"<svg onload=alert(1)></svg>").status_code == 400
    assert _up(cw, conv, "empty.txt", b"").status_code == 400
    big_text = b"a" * (ca_files.MAX_BYTES["text"] + 1)
    r = _up(cw, conv, "huge.md", big_text)
    assert r.status_code == 400 and "1 MB" in r.json()["detail"]
    assert _up(cw, conv, "big.png", _png() + b"\0" * (ca_files.MAX_BYTES["image"] + 1)).status_code in (400, 413)


def test_at_most_ten_files_go_with_a_message(cw):
    conv = _start(cw)
    for i in range(10):
        assert _up(cw, conv, f"n{i}.txt", b"x").status_code == 200
    assert _up(cw, conv, "n10.txt", b"x").status_code == 400


def test_uploads_are_for_their_owner_only(cw):
    conv = _start(cw)
    meta = _up(cw, conv, "shot.png", _png()).json()
    c = cw["client"]
    got = c.get(f"/ca/uploads/{meta['id']}", headers=cw["bob_h"])
    assert got.status_code == 200 and got.content == _png() and got.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got.headers["content-security-policy"]
    assert c.get(f"/ca/uploads/{meta['id']}", headers=cw["alice_h"]).status_code == 404       # another person: a 404
    assert _up(cw, conv, "x.png", _png(), who="alice").status_code == 404                     # not into Bob's conversation
    assert c.delete(f"/ca/uploads/{meta['id']}", headers=cw["alice_h"]).status_code == 404
    assert c.get("/ca/uploads/99999", headers=cw["bob_h"]).status_code == 404
    md = _up(cw, conv, "a.md", b"<script>alert(1)</script>").json()
    served = c.get(f"/ca/uploads/{md['id']}", headers=cw["bob_h"])
    assert served.headers["content-type"].startswith("text/plain") and "attachment" in served.headers["content-disposition"]
    assert c.delete(f"/ca/uploads/{md['id']}", headers=cw["bob_h"]).status_code == 200      # unsent: can be taken back
    assert c.get(f"/ca/uploads/{md['id']}", headers=cw["bob_h"]).status_code == 404


def test_attachments_ride_the_message_and_reach_the_runner_over_the_socket(cw):
    conv = _start(cw)
    a = _up(cw, conv, "shot.png", _png()).json()
    note = _up(cw, conv, "note.md", b"magic word").json()
    other = _start(cw, who="alice")
    foreign = _up(cw, other, "a.png", _png(), who="alice").json()
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET last_seen = now() WHERE id = %s", (cw["bob_dev"]["device_id"],))
    r = Runner(cw["client"], cw["bob_dev"], cw["bob_key"])
    r.handshake()
    try:
        # someone else's file (or one from another conversation) cannot be smuggled into my message
        bad = cw["client"].post(f"/ca/conversations/{conv}/messages", json={"text": "x", "attachments": [foreign["id"]]}, headers=cw["bob_h"])
        assert bad.status_code == 400
        sent = cw["client"].post(f"/ca/conversations/{conv}/messages", json={"text": "", "attachments": [a["id"], note["id"]]}, headers=cw["bob_h"])
        assert sent.status_code == 200 and [x["filename"] for x in sent.json()["attachments"]] == ["shot.png", "note.md"]
        got = cw["client"].get(f"/ca/conversations/{conv}", headers=cw["bob_h"]).json()
        assert [x["filename"] for x in got["messages"][0]["attachments"]] == ["shot.png", "note.md"] and got["title"] == "shot.png"
        assert _next(r)["type"] == "ca_available"
        r.ws.send_text(json.dumps({"type": "ca_claim", "id": "c1"}))
        claim = _next(r)
        turn = claim["turn"]
        assert [x["filename"] for x in turn["attachments"]] == ["shot.png", "note.md"] and turn["attachments"][0]["kind"] == "image"
        import base64
        r.ws.send_text(json.dumps({"type": "ca_attach", "id": "a1", "turn_id": turn["turn_id"], "fence": turn["fence"],
                                   "attachment_id": a["id"], "part": 0}))
        piece = _next(r)
        assert piece["ok"] and piece["last"] is True and base64.b64decode(piece["data"]) == _png()
        # only the running turn's own message's files; a stale fence or another file is refused
        r.ws.send_text(json.dumps({"type": "ca_attach", "id": "a2", "turn_id": turn["turn_id"], "fence": turn["fence"] + 1,
                                   "attachment_id": a["id"], "part": 0}))
        assert _next(r)["ok"] is False
        r.ws.send_text(json.dumps({"type": "ca_attach", "id": "a3", "turn_id": turn["turn_id"], "fence": turn["fence"],
                                   "attachment_id": foreign["id"], "part": 0}))
        assert _next(r)["ok"] is False
    finally:
        r.close()


def _sent_file(cw, conv, name="shot.png", data=None):
    """An upload attached to a sent message (the sweep only strips SENT files)."""
    meta = _up(cw, conv, name, data or _png()).json()
    with get_cursor() as cur:
        cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, 'user', 'look') RETURNING id", (conv, cw["bob"]["id"]))
        mid = cur.fetchone()["id"]
        cur.execute("UPDATE ca_attachments SET message_id = %s WHERE id = %s", (mid, meta["id"]))
    return meta["id"]


def _path_of(aid):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT path FROM ca_attachments WHERE id = %s", (aid,))
        return cur.fetchone()["path"]


def _set_task(task_id, status, days_ago=None):
    with get_cursor() as cur:
        cur.execute("UPDATE tasks SET status = %s, done_at = CASE WHEN %s::int IS NULL THEN NULL ELSE now() - make_interval(days => %s::int) END WHERE id = %s",
                    (status, days_ago, days_ago, task_id))


def _age_chat(conv, days):
    with get_cursor() as cur:
        cur.execute("UPDATE ca_conversations SET updated_at = now() - make_interval(days => %s) WHERE id = %s", (days, conv))


def test_files_are_stored_on_disk_not_in_the_database(cw):
    import os
    conv = _start(cw)
    aid = _up(cw, conv, "../../etc/passwd.png", _png()).json()["id"]
    rel = _path_of(aid)
    assert rel.startswith(f"{cw['bob']['id']}/{conv}/") and ".." not in rel and "passwd" not in rel      # the person's name is never a path
    full = os.path.join(config.CA_UPLOADS_DIR, rel)
    assert open(full, "rb").read() == _png() and oct(os.stat(full).st_mode & 0o777) == "0o600"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ca_attachments'")
        assert "data" not in [r["column_name"] for r in cur.fetchall()]


def test_files_on_a_task_chat_go_30_days_after_the_task_closes(cw):
    import os
    tid = cw["task"]["id"]
    conv = _start(cw, task_id=tid)
    aid = _sent_file(cw, conv, "screenshot.png")
    path = os.path.join(config.CA_UPLOADS_DIR, _path_of(aid))
    _age_chat(conv, 200)                                              # a very old chat on an OPEN task keeps its files
    assert ca_files.sweep()["stripped"] == 0 and os.path.exists(path)
    _set_task(tid, "done", 29)                                        # closed 29 days ago: kept
    assert ca_files.sweep()["stripped"] == 0 and os.path.exists(path)
    _set_task(tid, "done", 31)                                        # closed 31 days ago: stripped
    assert ca_files.sweep() == {"orphans": 0, "stripped": 1} and not os.path.exists(path)
    assert ca_files.sweep() == {"orphans": 0, "stripped": 0}          # idempotent
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT path, removed_reason FROM ca_attachments WHERE id = %s", (aid,))
        row = cur.fetchone()
    assert row["path"] is None and row["removed_reason"] == "removed after task closed"
    assert cw["client"].get(f"/ca/uploads/{aid}", headers=cw["bob_h"]).status_code == 404       # the file is gone
    got = cw["client"].get(f"/ca/conversations/{conv}", headers=cw["bob_h"]).json()             # the stub keeps the transcript readable
    stub = got["messages"][0]["attachments"][0]
    assert stub["filename"] == "screenshot.png" and stub["removed"] is True and stub["removed_reason"] == "removed after task closed"
    assert got["messages"][0]["content"] == "look"                                              # the chat text is kept
    # the stub is what a replay of the chat says: no file name pretending to be attached
    with get_cursor(commit=False) as cur:
        assert "attached" not in ca._transcript(cur, conv, None)


def test_reopening_a_task_keeps_its_files(cw):
    import os
    tid = cw["task"]["id"]
    conv = _start(cw, task_id=tid)
    aid = _sent_file(cw, conv)
    _set_task(tid, "done", 40)
    _set_task(tid, "doing", None)                                     # reopened before the sweep ran
    assert ca_files.sweep()["stripped"] == 0 and os.path.exists(os.path.join(config.CA_UPLOADS_DIR, _path_of(aid)))


def test_files_on_an_unlinked_chat_go_90_days_after_it_was_last_active(cw):
    import os
    conv = _start(cw)
    aid = _sent_file(cw, conv)
    path = os.path.join(config.CA_UPLOADS_DIR, _path_of(aid))
    _age_chat(conv, 89)
    assert ca_files.sweep()["stripped"] == 0 and os.path.exists(path)
    _age_chat(conv, 91)
    assert ca_files.sweep()["stripped"] == 1 and not os.path.exists(path)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT removed_reason FROM ca_attachments WHERE id = %s", (aid,))
        assert cur.fetchone()["removed_reason"] == "removed after 90 days idle"


def test_unsent_files_go_after_a_day_and_the_scheduler_runs_the_sweep(cw):
    import os
    conv = _start(cw)
    orphan = _up(cw, conv, "orphan.png", _png()).json()["id"]
    path = os.path.join(config.CA_UPLOADS_DIR, _path_of(orphan))
    assert ca_files.sweep()["orphans"] == 0 and os.path.exists(path)
    with get_cursor() as cur:
        cur.execute("UPDATE ca_attachments SET created_at = now() - interval '2 days' WHERE id = %s", (orphan,))
    from app import ca_notify
    tick = ca_notify.tick()
    assert tick["stripped_uploads"] == 1 and not os.path.exists(path)


def test_the_sweep_logs_counts_never_contents(cw, caplog):
    import logging
    conv = _start(cw)
    _sent_file(cw, conv, "secret-plans.png")
    _age_chat(conv, 120)
    with caplog.at_level(logging.INFO, logger="crewboss.ca.files"):
        ca_files.sweep()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "1 stripped" in text and "secret-plans" not in text


# --- speech --------------------------------------------------------------------------------------------------------------

def test_speech_is_hidden_and_404_without_a_provider(cw, monkeypatch):
    monkeypatch.setattr(config, "SPEECH_PROVIDER", "")
    assert cw["client"].get("/ca/state", headers=cw["bob_h"]).json()["speech"] is False
    assert cw["client"].get("/ca/speech/url", headers=cw["bob_h"]).status_code == 404
    monkeypatch.setattr(config, "SPEECH_PROVIDER", "aws_transcribe")        # a provider but no credentials: still no mic
    monkeypatch.setattr(config, "SPEECH_CREDENTIALS_FILE", "/nonexistent/creds")
    assert cw["client"].get("/ca/state", headers=cw["bob_h"]).json()["speech"] is False
    assert cw["client"].get("/ca/speech/url", headers=cw["bob_h"]).status_code == 404
    monkeypatch.setattr(config, "SPEECH_PROVIDER", "nonsense")
    assert speech.provider() is None


def test_speech_hands_out_a_signed_short_lived_address_and_rate_limits(cw, monkeypatch, tmp_path):
    creds = tmp_path / "speech-credentials"
    creds.write_text("access_key_id=AKIATESTKEY\nsecret_access_key=SECRETSECRET123\n")
    monkeypatch.setattr(config, "SPEECH_PROVIDER", "aws_transcribe")
    monkeypatch.setattr(config, "SPEECH_REGION", "ap-southeast-2")
    monkeypatch.setattr(config, "SPEECH_CREDENTIALS_FILE", str(creds))
    speech._buckets.clear()
    assert cw["client"].get("/ca/state", headers=cw["bob_h"]).json()["speech"] is True
    r = cw["client"].get("/ca/speech/url?lang=en-AU", headers=cw["bob_h"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert j["url"].startswith("wss://transcribestreaming.ap-southeast-2.amazonaws.com:8443/stream-transcription-websocket?")
    assert "X-Amz-Signature=" in j["url"] and "AKIATESTKEY" in j["url"] and "SECRETSECRET123" not in r.text
    assert "X-Amz-Expires=300" in j["url"] and "language-code=en-AU" in j["url"] and j["expires_in"] == 300
    assert cw["client"].get("/ca/speech/url?lang=xx-XX", headers=cw["bob_h"]).json()["language"] in speech.LANGS   # an unknown language is not passed on
    seen = [cw["client"].get("/ca/speech/url", headers=cw["bob_h"]).status_code for _ in range(8)]
    assert 429 in seen
    assert cw["client"].get("/ca/speech/url", headers=cw["alice_h"]).status_code == 200            # per person
    # deterministic signature
    import datetime as dt
    now = dt.datetime(2026, 11, 9, 1, 2, 3, tzinfo=dt.timezone.utc)
    a = speech.presign_transcribe({"key_id": "K", "secret": "S"}, "ap-southeast-2", 16000, "en-AU", now)
    assert a == speech.presign_transcribe({"key_id": "K", "secret": "S"}, "ap-southeast-2", 16000, "en-AU", now)
    assert a != speech.presign_transcribe({"key_id": "K", "secret": "T"}, "ap-southeast-2", 16000, "en-AU", now)


# --- history -------------------------------------------------------------------------------------------------------------

def test_history_is_own_conversations_with_search_paging_and_archive(cw):
    c = cw["client"]
    convs = [_start(cw) for _ in range(3)]
    with get_cursor() as cur:
        for i, cid in enumerate(convs):
            cur.execute("UPDATE ca_conversations SET title = %s, updated_at = now() - make_interval(mins => %s) WHERE id = %s", (f"Chat number {i}", i, cid))
        cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, 'user', 'the pineapple pizza question')", (convs[2], cw["bob"]["id"]))
        cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, 'system', 'pineapple in a system note')", (convs[0], cw["bob"]["id"]))
    alice_conv = _start(cw, who="alice")
    with get_cursor() as cur:
        cur.execute("UPDATE ca_conversations SET title = 'Alice private pineapple' WHERE id = %s", (alice_conv,))
        cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, 'user', 'pineapple secret')", (alice_conv, cw["alice"]["id"]))
    all_ = c.get("/ca/history.json", headers=cw["bob_h"]).json()
    assert [x["id"] for x in all_["items"]] == convs and all_["total"] == 3                  # newest first, only Bob's
    hit = c.get("/ca/history.json?q=pineapple", headers=cw["bob_h"]).json()
    assert [x["id"] for x in hit["items"]] == [convs[2]]                                      # message text; a system note and Alice's don't count
    assert [x["id"] for x in c.get("/ca/history.json?q=number 1", headers=cw["bob_h"]).json()["items"]] == [convs[1]]   # title
    assert c.get("/ca/history.json?q=%25", headers=cw["bob_h"]).json()["total"] == 0          # % is not a wildcard
    assert [x["id"] for x in c.get("/ca/history.json?q=pineapple", headers=cw["alice_h"]).json()["items"]] == [alice_conv]
    page = ca.history(cw["bob"]["id"], "", 2, 2)
    assert page["pages"] == 2 and [x["id"] for x in page["items"]] == [convs[2]]
    html = c.get("/ca/history", headers=cw["bob_h"])
    assert html.status_code == 200 and "Chat number 0" in html.text and "Alice private" not in html.text
    assert c.post(f"/ca/conversations/{convs[0]}/archive", headers=cw["bob_h"]).status_code == 200
    assert convs[0] not in [x["id"] for x in c.get("/ca/history.json", headers=cw["bob_h"]).json()["items"]]
    assert [x["id"] for x in c.get("/ca/history.json?archived=1", headers=cw["bob_h"]).json()["items"]] == [convs[0]]


# --- the linked item -----------------------------------------------------------------------------------------------------

def _knowledge(project_id, title="A note"):
    with get_cursor() as cur:
        cur.execute("INSERT INTO knowledge (project_id, kind, title, created_by, updated_by) VALUES (%s, 'note', %s, 'owner', 'owner') RETURNING id", (project_id, title))
        return cur.fetchone()["id"]


def _change(project_id, title="A change"):
    with get_cursor() as cur:
        cur.execute("INSERT INTO change_log (project_id, title, recommended_by) VALUES (%s, %s, 'owner') RETURNING id", (project_id, title))
        return cur.fetchone()["id"]


def test_an_invisible_item_is_dropped_on_create_and_404_on_link(people):
    cw = people
    c = cw["client"]
    bk, bc = _knowledge(cw["beta_app"]["id"], "Beta note"), _change(cw["beta_app"]["id"], "Beta change")
    ak, ac = _knowledge(cw["acme_app"]["id"], "Acme note"), _change(cw["acme_app"]["id"], "Acme change")
    for kind, ident in (("task", cw["btask"]["id"]), ("knowledge", bk), ("change", bc), ("nonsense", 1), ("task", 99999)):
        r = c.post("/ca/conversations", json={"path": "/x", "project": "acme-app", "entity": {"kind": kind, "id": ident}}, headers=cw["alice_h"])
        assert r.status_code == 200 and "entity" not in r.json()["context"], (kind, ident)          # dropped, the chat still starts
    ok = c.post("/ca/conversations", json={"path": "/x", "project": "acme-app", "entity": {"kind": "knowledge", "id": ak}}, headers=cw["alice_h"]).json()
    assert ok["context"]["entity"] == {"kind": "knowledge", "id": ak, "title": "Acme note", "label": "Knowledge"}
    conv = _start(cw, who="alice")
    assert c.patch(f"/ca/conversations/{conv}", json={"entity": {"kind": "task", "id": cw["btask"]["id"]}}, headers=cw["alice_h"]).status_code == 404
    assert c.patch(f"/ca/conversations/{conv}", json={"entity": {"kind": "change", "id": bc}}, headers=cw["alice_h"]).status_code == 404
    link = c.patch(f"/ca/conversations/{conv}", json={"entity": {"kind": "change", "id": ac}}, headers=cw["alice_h"])
    assert link.status_code == 200 and link.json()["context"]["entity"]["label"] == f"Change #{ac}"
    assert c.patch(f"/ca/conversations/{conv}", json={"entity": {"kind": "task", "id": cw["task"]["id"]}}, headers=cw["alice_h"]).json()["context"]["task_id"] == cw["task"]["id"]
    # another person's conversation can't be (un)linked
    assert c.patch(f"/ca/conversations/{conv}", json={"entity": None}, headers=cw["bob_h"]).status_code == 404
    un = c.patch(f"/ca/conversations/{conv}", json={"entity": None}, headers=cw["alice_h"])
    assert un.status_code == 200 and "entity" not in un.json()["context"] and "task_id" not in un.json()["context"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT entity_kind, entity_id FROM ca_conversations WHERE id = %s", (conv,))
        assert dict(cur.fetchone()) == {"entity_kind": None, "entity_id": None}
    assert c.patch(f"/ca/conversations/{conv}", json={}, headers=cw["alice_h"]).status_code == 400


def test_many_chats_can_link_to_one_task_and_each_start_is_new(people):
    cw = people
    tid = cw["task"]["id"]
    ids = [cw["client"].post("/ca/conversations", json={"path": "/x", "project": "acme-app", "entity": {"kind": "task", "id": tid}},
                             headers=cw[w + "_h"]).json()["id"] for w in ("alice", "alice", "alice")]
    ids.append(_start(cw, who="alice", task_id=tid))                           # the pre-#727 way still links
    assert len(set(ids)) == 4
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM ca_conversations WHERE entity_kind = 'task' AND entity_id = %s", (str(tid),))
        assert cur.fetchone()["n"] == 4


def test_discussions_soft_link_no_content_and_no_admin_read(people):
    """The AiOS rule still holds for every item EXCEPT a task, whose discussion is shared by the people on it (#830, tests/test_ca_discussion.py):
    here a project's discussions: the starter opens theirs, everyone else, an administrator included, sees a soft link."""
    cw = people
    c = cw["client"]
    slug = "acme-app"
    mine = c.post("/ca/conversations", json={"path": "/x", "project": slug, "entity": {"kind": "project", "id": slug}}, headers=cw["alice_h"]).json()["id"]
    with get_cursor() as cur:
        cur.execute("UPDATE ca_conversations SET title = 'Alice thinks the cache is stale' WHERE id = %s", (mine,))
        cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, 'user', 'TOPSECRET reasoning')", (mine, cw["alice"]["id"]))
    own = c.get(f"/ca/items/project/{slug}/discussions", headers=cw["alice_h"])
    assert own.status_code == 200 and "Alice thinks the cache is stale" in own.text and f'data-ca-open-conv="{mine}"' in own.text
    for who in ("carol", "bob"):                      # another member, and the instance owner / administrator
        d = ca.discussions_of(cw[who]["id"], "project", slug)
        assert d["discussions"] == [{"mine": False, "open": False, "by": "Alice", "updated_at": d["discussions"][0]["updated_at"]}]
    html = ca.discussions_of(cw["bob"]["id"], "project", slug)
    assert "Alice thinks" not in json.dumps(html, default=str) and "TOPSECRET" not in json.dumps(html, default=str)
    # via the page fragment: a soft link line, never the title or an open link
    bob_html = c.get(f"/ca/items/project/{slug}/discussions", headers=cw["bob_h"])
    assert bob_html.status_code == 200 and "Discussion by Alice" in bob_html.text
    assert "Alice thinks" not in bob_html.text and f'data-ca-open-conv="{mine}"' not in bob_html.text and "TOPSECRET" not in bob_html.text
    # no admin read: every read is by (id, user)
    assert c.get(f"/ca/conversations/{mine}", headers=cw["bob_h"]).status_code == 404
    assert c.post(f"/ca/conversations/{mine}/messages", json={"text": "x"}, headers=cw["bob_h"]).status_code == 404
    assert c.post(f"/ca/conversations/{mine}/archive", headers=cw["bob_h"]).status_code == 404
    # an item the viewer can't see has no Discussions section at all
    assert c.get(f"/ca/items/task/{cw['btask']['id']}/discussions", headers=cw["alice_h"]).status_code == 404
    assert c.get("/ca/items/project/beta-app/discussions", headers=cw["alice_h"]).status_code == 404


def test_an_agent_reads_only_its_starters_discussions_and_the_job_links_back(people):
    cw = people
    tid = cw["task"]["id"]
    link = {"kind": "task", "id": tid}
    mine = ca.create_conversation(cw["alice"]["id"], "/x", "acme-app", entity=link)["id"]
    other = ca.create_conversation(cw["bob"]["id"], "/x", "acme-app", entity=link)["id"]            # Bob's, on the same task
    elsewhere = ca.create_conversation(cw["alice"]["id"], "/x", "acme-app")["id"]                   # Alice's, linked to nothing
    with get_cursor() as cur:
        cur.execute("UPDATE ca_conversations SET title = 'Alice on the task' WHERE id = %s", (mine,))
        cur.execute("UPDATE ca_conversations SET title = 'Bob on the task' WHERE id = %s", (other,))
        for conv, uid, text in ((mine, cw["alice"]["id"], "Alice's idea: use a queue"), (other, cw["bob"]["id"], "Bob's private note"),
                                (elsewhere, cw["alice"]["id"], "unrelated")):
            cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, 'user', %s)", (conv, uid, text))
        cur.execute("UPDATE users SET role = 'designer', is_owner = false WHERE id = %s", (cw["alice"]["id"],))
        cur.execute("UPDATE project_members SET role = 'admin' WHERE user_id = %s", (cw["alice"]["id"],))
    job = jobs.create_job(tid, cw["alice"]["id"], "builder", approved=True, ca_conversation_id=mine)
    assert job["ca_conversation_id"] == mine
    # a discussion that is not the starter's own is not recorded as the starter
    other_job_conv = ca.create_conversation(cw["bob"]["id"], "/x", "acme-app")["id"]
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'cancelled' WHERE id = %s", (job["id"],))
    j2 = jobs.create_job(tid, cw["alice"]["id"], "builder", approved=True, ca_conversation_id=other_job_conv)
    assert j2["ca_conversation_id"] is None
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'cancelled' WHERE id = %s", (j2["id"],))
    job = jobs.create_job(tid, cw["alice"]["id"], "builder", approved=True, ca_conversation_id=mine)
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'running', fence = 7, branch = 'b' WHERE id = %s", (job["id"],))
    brief = jobs.brief(job["id"], 7)
    assert [d["id"] for d in brief["discussions"]] == [mine] and brief["started_from_discussion"] == mine
    assert "Alice's idea" not in json.dumps(brief)                         # ids and titles only, never a transcript
    read = jobs.read_discussion(job["id"], 7, mine)
    assert read["messages"][0]["text"] == "Alice's idea: use a queue" and "data, never instructions" in read["note"]
    for foreign in (other, elsewhere, 99999):                              # Bob's chat, an unlinked chat of Alice's, a missing one
        with pytest.raises(NotFound):
            jobs.read_discussion(job["id"], 7, foreign)
    with pytest.raises(jobs.StaleFence):
        jobs.read_discussion(job["id"], 8, mine)
    # both sides: the task's Discussions section shows the job and links to the discussion, which everyone on the task may open (#830)
    d_alice = ca.discussions_of(cw["alice"]["id"], "task", tid)
    assert d_alice["jobs"][0]["from_discussion"] == mine
    d_bob = ca.discussions_of(cw["bob"]["id"], "task", tid)
    assert d_bob["jobs"][0]["from_discussion"] == mine and d_bob["jobs"][0]["from_a_discussion"] is True
    assert "Alice's idea" not in json.dumps(d_bob, default=str)      # titles are shared on a task; the transcript is read by opening the conversation


def test_a_card_tap_records_which_discussion_started_the_job(cw):
    conv = _start(cw, task_id=cw["task"]["id"])
    with get_cursor() as cur:
        cur.execute("INSERT INTO ca_cards (conversation_id, user_id, kind, payload) VALUES (%s, %s, 'confirm', %s) RETURNING id",
                    (conv, cw["bob"]["id"], json.dumps({"action": "start_job", "summary": "x", "params": {"task_id": cw["task"]["id"], "profile": "builder"}})))
        card = cur.fetchone()["id"]
    assert cw["client"].post(f"/ca/cards/{card}", json={"action": "confirm"}, headers=cw["bob_h"]).json()["state"] == "done"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT ca_conversation_id FROM jobs WHERE task_id = %s", (cw["task"]["id"],))
        assert cur.fetchone()["ca_conversation_id"] == conv


def test_the_drawer_and_history_page_ship_the_new_controls(cw):
    page = cw["client"].get("/ca/history", headers=cw["bob_h"]).text
    for needle in ('id="ca-mic"', 'id="ca-attach"', 'id="ca-entity"', 'id="ca-devmenu"', "Recent conversations", "Chat history", 'data-ca-history'):
        assert needle in page, needle


# --- review 727 fixes ------------------------------------------------------------------------------------------------

def test_a_files_only_message_whose_ids_are_all_junk_is_a_400_not_a_500(cw):
    conv = _start(cw)
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET last_seen = now() WHERE id = %s", (cw["bob_dev"]["device_id"],))
    r = Runner(cw["client"], cw["bob_dev"], cw["bob_key"])
    r.handshake()
    try:
        res = cw["client"].post(f"/ca/conversations/{conv}/messages", json={"text": "", "attachments": ["x", None]}, headers=cw["bob_h"])
        assert res.status_code == 400
    finally:
        r.close()


def test_out_of_range_item_ids_are_not_found_not_a_500(people):
    cw = people
    c = cw["client"]
    assert c.get("/ca/items/task/99999999999/discussions", headers=cw["alice_h"]).status_code == 404
    conv = _start(cw, who="alice")
    r = c.patch(f"/ca/conversations/{conv}", json={"entity": {"kind": "task", "id": 99999999999}}, headers=cw["alice_h"])
    assert r.status_code == 404
    r = c.post("/ca/conversations", json={"path": "/x", "project": "acme-app", "entity": {"kind": "task", "id": 99999999999}}, headers=cw["alice_h"])
    assert r.status_code == 200 and "entity" not in r.json()["context"]


def test_no_uploads_onto_an_archived_conversation(cw):
    conv = _start(cw)
    with get_cursor() as cur:
        cur.execute("UPDATE ca_conversations SET archived_at = now() WHERE id = %s", (conv,))
    assert _up(cw, conv, "a.txt", b"hello").status_code == 400


def test_each_person_has_a_disk_quota_for_files(cw, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "CA_UPLOAD_USER_QUOTA_MB", 1)
    conv = _start(cw)
    assert _up(cw, conv, "a.txt", b"x" * (700 * 1024)).status_code == 200
    r = _up(cw, conv, "b.txt", b"y" * (700 * 1024))
    assert r.status_code == 400 and "MB of files" in r.text
