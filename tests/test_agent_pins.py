"""Owner-reviewed pins for a project's agent, command and skill files (#829): the runner's agent_file frames, the pending files, the Owner's
read-and-pin on the Machines page (fresh factor), the pins in the claim, the job flagged for files its agent changed. Nothing is approved by
parsing: `declared` only displays."""
import base64
import hashlib
import json

import pytest

from app import access, agent_pins, jobs
from app.db import get_cursor
from app.repo import BadRequest

from test_devices import est, signed_in  # noqa: F401
from test_jobs import _queue, world  # noqa: F401
from test_runner_socket_jobs import _enrol_alice, _rpc
from test_runner_ws import Runner
from test_web_auth import auth_on  # noqa: F401

AGENT = "---\nname: erp-builder\ndescription: builds\ntools: Read, Grep\n---\nDo the work.\n"
b64 = lambda t: base64.b64encode(t.encode()).decode()  # noqa: E731
sha = lambda t: hashlib.sha256(t.encode()).hexdigest()  # noqa: E731


@pytest.fixture
def running(world):
    _queue(world)
    world["claim"] = jobs.claim(world["dev"]["device_id"])
    return world


def _send(w, path, text, source="preflight", sha256=None, raw_b64=None):
    c = w["claim"]
    return jobs.agent_file_reported(w["dev"]["device_id"], c["job_id"], c["fence"], path, sha256 or sha(text), raw_b64 or b64(text), source)


def _rows(state=None):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM project_agent_files WHERE (%s::text IS NULL OR state = %s) ORDER BY path", (state, state))
        return cur.fetchall()


def test_a_claim_carries_no_pins_until_the_owner_pins_and_then_exactly_those(running, client, signed_in):
    assert running["claim"]["pins"] == []
    _send(running, ".claude/agents/erp-builder.md", AGENT)
    _send(running, "pkg/.claude/commands/go.md", "go\n")
    assert [r["state"] for r in _rows()] == ["pending", "pending"]
    ids = [r["id"] for r in _rows()]
    r = client.post("/acme/machines/agent-files/pin", data={"ids": ids}, headers={"cookie": signed_in["cookie"]}, follow_redirects=False)
    assert r.status_code == 303
    assert [r["state"] for r in _rows()] == ["pinned", "pinned"]
    # the next claim of a job on that project carries both
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'done' WHERE id = %s", (running["claim"]["job_id"],))
    _queue(running)
    nxt = jobs.claim(running["dev"]["device_id"])
    assert nxt["pins"] == [{"path": ".claude/agents/erp-builder.md", "sha256": sha(AGENT)}, {"path": "pkg/.claude/commands/go.md", "sha256": sha("go\n")}]


def test_the_review_card_shows_the_text_read_only_and_what_it_declares_(running, client, signed_in):
    _send(running, ".claude/agents/erp-builder.md", AGENT)
    html = client.get("/acme/machines", headers={"cookie": signed_in["cookie"]}).text
    assert "data-agent-card=\"acme-app\"" in html and "Agent files to review" in html and "Pin all 1 after reading" in html
    assert "Do the work." in html and "tools Read, Grep" in html and "<textarea" not in html.split("data-agent-card")[1].split("</article>")[0]
    assert [r["state"] for r in _rows()] == ["pending"]


def test_the_review_card_is_only_computed_for_the_runners_tab(running, client, signed_in, monkeypatch):
    _send(running, ".claude/agents/erp-builder.md", AGENT)
    calls = []
    real = agent_pins.card
    monkeypatch.setattr(agent_pins, "card", lambda *a: calls.append(1) or real(*a))
    client.get("/acme/machines?tab=servers", headers={"cookie": signed_in["cookie"]})
    assert calls == []
    html = client.get("/acme/machines?tab=runners", headers={"cookie": signed_in["cookie"]}).text
    assert calls == [1] and "Pin all 1 after reading" in html


def test_pin_only_pins_what_was_listed_and_a_changed_file_is_a_new_pending_row(running, client, signed_in):
    _send(running, ".claude/agents/erp-builder.md", AGENT)
    first = _rows()[0]["id"]
    _send(running, ".claude/agents/erp-builder.md", AGENT + "more\n")
    assert len(_rows("pending")) == 2
    client.post("/acme/machines/agent-files/pin", data={"ids": [first]}, headers={"cookie": signed_in["cookie"]}, follow_redirects=False)
    states = {r["sha256"]: r["state"] for r in _rows()}
    assert states == {sha(AGENT): "pinned", sha(AGENT + "more\n"): "pending"}     # the unlisted newer version stays unreviewed
    client.post("/acme/machines/agent-files/pin", data={"ids": [r["id"] for r in _rows("pending")]}, headers={"cookie": signed_in["cookie"]}, follow_redirects=False)
    assert sorted(r["state"] for r in _rows()) == ["pinned", "pinned"]
    pid = _rows()[0]["id"]
    client.post(f"/acme/machines/agent-files/{pid}/unpin", headers={"cookie": signed_in["cookie"]}, follow_redirects=False)
    assert len(_rows()) == 1


def test_bytes_that_do_not_match_their_hash_or_are_not_text_or_too_big_are_refused(running):
    for kw in ({"sha256": sha("other")}, {"raw_b64": base64.b64encode(b"\x00\x01").decode(), "sha256": hashlib.sha256(b"\x00\x01").hexdigest()},
               {"raw_b64": base64.b64encode(b"\xff\xfe").decode(), "sha256": hashlib.sha256(b"\xff\xfe").hexdigest()},
               {"raw_b64": "!!notbase64"}):
        with pytest.raises(BadRequest):
            _send(running, ".claude/agents/a.md", "x", **kw)
    big = "x" * (agent_pins.MAX_BYTES + 1)
    with pytest.raises(BadRequest):
        _send(running, ".claude/agents/big.md", big)
    for bad in ("README.md", "/etc/passwd", ".claude/agents/../../x", ".claude/settings.json.bak", "a/.claude/rules/r.md"):
        with pytest.raises(BadRequest):
            _send(running, bad, "x")
    with pytest.raises(BadRequest):
        _send(running, ".claude/agents/a.md", "x", source="elsewhere")
    assert _rows() == []


def test_the_database_itself_refuses_a_hash_that_is_not_the_stored_bytes(running):
    with get_cursor() as cur:
        cur.execute("SELECT id FROM projects WHERE slug = 'acme-app'")
        pid = cur.fetchone()["id"]
    with pytest.raises(Exception):
        with get_cursor() as cur:
            cur.execute("INSERT INTO project_agent_files (project_id, path, sha256, content) VALUES (%s, '.claude/agents/a.md', %s, %s)",
                        (pid, sha("not it"), b"the bytes"))


def test_files_an_agent_changed_flag_the_job_and_its_task(running):
    _send(running, ".claude/agents/new.md", "---\nname: new\n---\n", source="job")
    _send(running, ".claude/skills/x/SKILL.md", "x", source="job")
    c = running["claim"]
    jobs.agent_file_reported(running["dev"]["device_id"], c["job_id"], c["fence"], ".claude/agents/link.md", "", None, "job")   # a link: named only
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT review_paths FROM jobs WHERE id = %s", (c["job_id"],))
        assert sorted(cur.fetchone()["review_paths"]) == [".claude/agents/link.md", ".claude/agents/new.md", ".claude/skills/x/SKILL.md"]
    assert len(_rows("pending")) == 2                                              # the link has no bytes: nothing to pin
    jobs.finish(running["dev"]["device_id"], c["job_id"], c["fence"], True, "NEEDS OWNER REVIEW: pushed")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT body FROM task_comments WHERE task_id = %s ORDER BY id DESC LIMIT 1", (c["task_id"],))
        assert "3 agent, command or skill file(s)" in cur.fetchone()["body"]
    assert any(j["review_paths"] for j in jobs.list_for(running["acme"]["id"]))


def test_a_stale_fence_or_another_device_cannot_report_files(running):
    c = running["claim"]
    with pytest.raises(jobs.StaleFence):
        jobs.agent_file_reported(running["dev"]["device_id"], c["job_id"], c["fence"] + 1, ".claude/agents/a.md", sha("x"), b64("x"), "preflight")
    with pytest.raises(jobs.StaleFence):
        jobs.agent_file_reported(running["dev"]["device_id"] + 99, c["job_id"], c["fence"], ".claude/agents/a.md", sha("x"), b64("x"), "preflight")


def test_declared_only_displays_what_a_file_says_with_a_real_yaml_reader():
    d = agent_pins.declared(b'---\nname: x\n"allowed-tool\\x73": Bash, Read\n"hook\\x73":\n  Stop: []\nmcpServers:\n  k: {}\npermissionMode: bypassPermissions\n---\n')
    assert d["tools"] == ["Bash", "Read"] and d["hooks"] == ["Stop"] and d["mcp_servers"] == ["k"] and d["permission_mode"] == "bypassPermissions"
    assert agent_pins.declared(b"no frontmatter")["tools"] == []
    assert "not valid YAML" in agent_pins.declared(b"---\n: : [\n---\n")["note"]
    assert "unusual line breaks" in agent_pins.declared("---\nname: x\u2028tools: Bash\n---\n".encode())["note"]


def test_a_person_who_does_not_own_the_project_cannot_pin(running):
    _send(running, ".claude/agents/a.md", "x")
    nobody = access.Principal(user_id=987654, role=access.DESIGNER) if hasattr(access, "Principal") else None
    with pytest.raises(Exception):
        agent_pins.pin(nobody, 987654, [_rows()[0]["id"]], running["acme"]["id"])
    assert _rows("pending")


def _chunks(text_or_bytes, size=4096):
    data = text_or_bytes if isinstance(text_or_bytes, bytes) else text_or_bytes.encode()
    return [data[i:i + size] for i in range(0, len(data), size)]


def _send_chunks(r, claim, path, data, start_id=10, **over):
    """What the runner does: one frame per chunk, each answered. -> the replies."""
    parts = _chunks(data)
    replies = []
    for i, c in enumerate(parts):
        frame = {"type": "agent_file", "id": start_id + i, "job_id": claim["job_id"], "fence": claim["fence"], "path": path,
                 "sha256": hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest(), "source": "preflight",
                 "part": i, "parts": len(parts), "b64": base64.b64encode(c).decode(), **over}
        assert len(json.dumps(frame, separators=(",", ":"))) < 6 * 1024
        replies.append(_rpc(r, **frame))
    return replies


def test_a_big_agent_file_arrives_in_chunks_and_is_reassembled_and_verified(world, client):
    dev, key = _enrol_alice(world, "sock-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        _queue(world)
        claim = _rpc(r, type="claim", id=1)["job"]
        text = "line of agent text\n" * 3000           # ~57 KB: 14 chunks
        replies = _send_chunks(r, claim, ".claude/agents/big.md", text)
        assert len(replies) == 14 and all(x["ok"] is True for x in replies) and replies[0].get("part") == 0
        assert [row["path"] for row in _rows()] == [".claude/agents/big.md"] and bytes(_rows()[0]["content"]).decode() == text
        assert _rpc(r, type="ping", id=99)["type"] == "pong"
    finally:
        r.close()


def test_a_chunked_report_that_goes_wrong_gets_a_reason_and_keeps_the_socket(world, client):
    dev, key = _enrol_alice(world, "sock-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        _queue(world)
        claim = _rpc(r, type="claim", id=1)["job"]
        text = "x" * 9000
        # a wrong sha256: refused on the LAST chunk with a plain reason, nothing stored
        replies = _send_chunks(r, claim, ".claude/agents/a.md", text, sha256="0" * 64)
        assert replies[-1]["ok"] is False and replies[-1]["status"] == 400 and "sha256" in replies[-1]["detail"] and _rows() == []
        # out of order / a chunk of nothing in progress
        bad = _rpc(r, type="agent_file", id=20, job_id=claim["job_id"], fence=claim["fence"], path=".claude/agents/a.md", sha256=sha("x"),
                   source="preflight", part=1, parts=3, b64=b64("x"))
        assert bad["ok"] is False and bad["status"] == 400 and "out of order" in bad["detail"]
        # too many bytes in total (more than the review cap), reassembly never grows past it
        huge = "y" * (agent_pins.MAX_BYTES + 5000)
        replies = _send_chunks(r, claim, ".claude/agents/huge.md", huge, start_id=30)
        assert replies[-1]["ok"] is False and any(x["ok"] is False and "too large" in x["detail"] for x in replies) and _rows() == []
        # a file that is not text: the bytes are checked by the same rules as before
        replies = _send_chunks(r, claim, ".claude/agents/bin.md", b"\x00\x01" * 100, start_id=300)
        assert replies[-1]["ok"] is False and replies[-1]["status"] == 400
        # a bad chunk number, an over-long chunk
        assert _rpc(r, type="agent_file", id=400, job_id=claim["job_id"], fence=claim["fence"], path=".claude/agents/a.md", sha256=sha("x"),
                    source="preflight", part=5, parts=2, b64=b64("x"))["ok"] is False
        assert _rpc(r, type="agent_file", id=401, job_id=claim["job_id"], fence=claim["fence"], path=".claude/agents/a.md", sha256=sha("x"),
                    source="preflight", part=0, parts=1, b64="A" * 6200)["detail"] == "chunk too large"
        assert _rpc(r, type="ping", id=99)["type"] == "pong"               # the socket survived every one of them
        # and a good one still works afterwards
        assert _send_chunks(r, claim, ".claude/agents/a.md", "fine\n", start_id=500)[-1]["ok"] is True and len(_rows()) == 1
    finally:
        r.close()


def test_a_chunk_of_another_file_or_job_drops_the_assembly(world, client):
    dev, key = _enrol_alice(world, "sock-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        _queue(world)
        claim = _rpc(r, type="claim", id=1)["job"]
        data = "z" * 9000
        first = {"type": "agent_file", "id": 2, "job_id": claim["job_id"], "fence": claim["fence"], "path": ".claude/agents/a.md", "sha256": sha(data),
                 "source": "preflight", "part": 0, "parts": 3, "b64": b64(data[:4096])}
        assert _rpc(r, **first)["ok"] is True
        other = {**first, "id": 3, "path": ".claude/agents/b.md", "part": 1}      # chunk 1 of a different file
        assert _rpc(r, **other)["ok"] is False
        assert _rpc(r, **{**first, "id": 4, "part": 1})["ok"] is False             # the assembly is gone: even the right chunk is refused now
        assert _rows() == []
    finally:
        r.close()


def test_a_frame_over_the_chunk_limit_still_closes_the_socket(world, client):
    dev, key = _enrol_alice(world, "sock-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        _queue(world)
        claim = _rpc(r, type="claim", id=1)["job"]
        r.ws.send_text(json.dumps({"type": "agent_file", "id": 2, "job_id": claim["job_id"], "fence": claim["fence"], "path": ".claude/agents/big.md",
                                   "sha256": sha("x"), "b64": "A" * 20000, "source": "preflight"}, separators=(",", ":")))
        from test_runner_ws import _expect_close
        _expect_close(r, 4400)                           # one big frame is not a thing any more (uvicorn would close it at 8 KiB anyway)
    finally:
        r.close()


def test_any_other_frame_over_4096_bytes_closes_the_socket(world, client):
    dev, key = _enrol_alice(world, "sock-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        _queue(world)
        claim = _rpc(r, type="claim", id=1)["job"]
        r.ws.send_text(json.dumps({"type": "heartbeat", "id": 3, "job_id": claim["job_id"], "fence": claim["fence"], "pad": "x" * 6000}))
        from test_runner_ws import _expect_close
        _expect_close(r, 4400)
    finally:
        r.close()


def test_a_settings_file_can_be_pinned_like_an_agent_file(running):
    _send(running, ".claude/settings.json", '{"permissions": {"allow": ["Read"]}}\n')
    _send(running, "pkg/.claude/settings.local.json", "{}\n")
    assert sorted(r["path"] for r in _rows("pending")) == [".claude/settings.json", "pkg/.claude/settings.local.json"]
    assert agent_pins.declared(b'{"permissions": {}}')["tools"] == []        # not frontmatter: nothing to display, nothing raised


def test_the_card_loads_no_bytes_for_pinned_files_and_parses_nothing_until_asked(running, monkeypatch):
    _send(running, ".claude/agents/a.md", AGENT)
    _send(running, ".claude/agents/b.md", AGENT + "two\n")
    with get_cursor() as cur:
        cur.execute("UPDATE project_agent_files SET state = 'pinned', pinned_at = now() WHERE path = '.claude/agents/b.md'")
    parsed = []
    real = agent_pins.declared
    monkeypatch.setattr(agent_pins, "declared", lambda c, path="": parsed.append(1) or real(c, path))
    who = access.OWNER_PRINCIPAL
    monkeypatch.setattr(access, "role_in", lambda w, pid: access.PROJECT_OWNER)
    cards = agent_pins.card(who, running["acme"]["id"])
    pend, pinned = cards[0]["pending"], cards[0]["pinned"]
    assert parsed == []                                                         # listing parses nothing
    assert pinned[0]["path"] == ".claude/agents/b.md" and pinned[0]["text"] is None and pinned[0]["size"] == len((AGENT + "two\n").encode())
    assert pend[0]["text"] == AGENT and pend[0]["declared"]["name"] == "erp-builder" and parsed == [1]
    assert pend[0]["declared"] is pend[0]["declared"] and parsed == [1]            # parsed once, on demand


def test_a_yaml_alias_bomb_is_not_parsed_or_expanded_and_the_page_says_to_read_the_raw_text():
    import time
    bomb = "---\nname: x\na0: &a0 [lol, lol, lol, lol, lol, lol, lol, lol, lol, lol]\n" + "".join(
        f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]\n" for i in range(1, 12)) + "tools: [*a11]\n---\nbody\n"
    assert len(bomb) < 1000
    t = time.monotonic()
    d = agent_pins.declared(bomb.encode())
    assert time.monotonic() - t < 1.0
    assert "anchors or aliases" in d["note"] and "raw text" in d["note"] and d["tools"] == [] and d["name"] == ""
    # a merge key needs an alias: the same
    assert "anchors or aliases" in agent_pins.declared(b"---\nbase: &b {k: v}\nname: x\nm:\n  <<: *b\n---\n")["note"]
    # nested values are never stringified: a list inside the tools list shows as (nested), not expanded
    d = agent_pins.declared(b"---\nname: x\ntools: [[a, [b, c]], Read]\nhooks: {Stop: [[1, 2]]}\n---\n")
    assert d["tools"] == ["(nested)", "Read"] and d["hooks"] == ["Stop"]
    # absurd nesting without aliases fails closed instead of raising
    deep = b"---\nname: x\ntools: " + b"[" * 3000 + b"]" * 3000 + b"\n---\n"
    assert "raw text" in agent_pins.declared(deep)["note"]
    # a long scalar is cut
    assert len(agent_pins.declared(("---\nname: " + "n" * 5000 + "\n---\n").encode())["name"]) == 80


def test_deep_flow_nesting_returns_fast_and_says_to_read_the_raw_text():
    """#829 review 4: a 98 KB "[[[...]]]" took ~94 s per file; nesting past 32 now stops the event reader at once."""
    import time
    n = (agent_pins.MAX_BYTES - 40) // 2
    cases = [b"---\ntools: " + b"[" * n + b"]" * n + b"\n---\nbody\n",
             b"---\ntools: " + b"{a: " * (agent_pins.MAX_BYTES // 8) + b"1" + b"}" * (agent_pins.MAX_BYTES // 8) + b"\n---\n",
             b"---\n" + "\n".join(" " * i + "a:" for i in range(430)).encode() + b" 1\n---\n"]
    for c in cases:
        t = time.monotonic()
        d = agent_pins.declared(c)
        assert time.monotonic() - t < 1.0, len(c)
        assert "nested too deeply" in d["note"] and "raw text" in d["note"]
    assert agent_pins.declared(b"---\nname: x\ntools: [[[a]]]\n---\n")["name"] == "x"          # ordinary nesting still shows


def test_only_the_first_8kb_of_frontmatter_is_read_for_display():
    big = b"---\nname: x\n" + b"".join(b"k%d: v\n" % i for i in range(5000)) + b"---\n"
    d = agent_pins.declared(big)
    assert "8 KB" in d["note"] and "raw text" in d["note"]


def test_a_json_settings_file_shows_no_frontmatter_defaults():
    d = agent_pins.declared(b'{"permissions": {"allow": ["Read"]}}', ".claude/settings.json")
    assert d["note"] == "settings file: read the raw text" and d["tools"] == [] and d["permission_mode"] == ""
    d = agent_pins.declared(b'---\nname: x\n---\n', ".claude/Settings.LOCAL.JSON")
    assert "settings file" in d["note"]
