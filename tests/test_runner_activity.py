"""The live runner conversation, runner side (#89): Claude Code's transcript -> small redacted, capped frames; Gemini's turns as a second speaker; a missing
or unreadable transcript says so once and never touches the job; a stale fence ends the stream."""
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import activity, gemini_mcp, spawn  # noqa: E402

SID = str(uuid.uuid4())


def A(*blocks):
    return {"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": list(blocks)}}


def U(*blocks):
    return {"type": "user", "message": {"role": "user", "content": list(blocks)}}


def tu(i, name, **inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def tr(i, content, err=False):
    return {"type": "tool_result", "tool_use_id": i, "content": content, "is_error": err}


class Link:
    def __init__(self, replies=None):
        self.sent, self.replies = [], list(replies or [])

    async def rpc(self, mtype, timeout=30.0, **fields):
        if self.replies and isinstance(self.replies[0], Exception):
            raise self.replies.pop(0)
        self.sent.append({"type": mtype, **fields})
        return self.replies.pop(0) if self.replies else {"ok": True, "accepted": len(fields["frames"])}


def streamer(tmp_path, link=None, gemini=None, clock=None, **kw):
    cwd = tmp_path / "wt"
    cwd.mkdir(exist_ok=True)
    home = tmp_path / "claude"
    (home / "projects" / activity.cwd_slug(str(cwd))).mkdir(parents=True, exist_ok=True)
    s = activity.Streamer(link or Link(), {"job_id": 7, "fence": 3}, str(cwd), SID, gemini, home, clock=clock or __import__("time").monotonic, **kw)
    path = home / "projects" / activity.cwd_slug(str(cwd)) / f"{SID}.jsonl"
    return s, path


def write(path, *entries, raw=()):
    with open(path, "a") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
        for r in raw:
            fh.write(r)


def run(coro):
    return asyncio.run(coro)


# --- transcript -> frames -------------------------------------------------------------------------------------------------------------

def test_a_transcript_becomes_frames(tmp_path):
    s, path = streamer(tmp_path)
    write(path, {"type": "queue-operation"}, {"type": "user", "message": {"role": "user", "content": "the job prompt"}},
          A({"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "I will read the file."}, tu("t1", "Read", file_path="/w/app/x.py")),
          U(tr("t1", "1\timport os\n2\t")),
          A(tu("t2", "Edit", file_path="/w/app/x.py", old_string="a\nb", new_string="a\nb\nc\nd")),
          U(tr("t2", [{"type": "text", "text": "File edited"}])),
          A(tu("t3", "Bash", command="pytest tests/test_x.py\n  -q")),
          U(tr("t3", "1 failed", err=True)),
          A({"type": "text", "text": "Done."}))
    s.poll()
    kinds = [(f["speaker"], f["kind"], f["text"]) for f in s.queue if f["kind"] != "status"]
    assert kinds == [("claude", "assistant_text", "I will read the file."), ("claude", "tool_call", "Read /w/app/x.py"),
                     ("claude", "tool_result", "1\timport os\n2\t"), ("claude", "tool_call", "Edit /w/app/x.py (+4 −2)"),
                     ("claude", "tool_result", "File edited"), ("claude", "tool_call", "$ pytest tests/test_x.py ↵ -q"),
                     ("claude", "tool_result", "1 failed"), ("claude", "assistant_text", "Done.")]
    res = [f for f in s.queue if f["kind"] == "tool_result"]
    assert res[0]["meta"] == {"tool": "Read", "status": "ok", "is_error": False} and res[2]["meta"]["is_error"] and res[2]["meta"]["tool"] == "Bash"
    calls = [f for f in s.queue if f["kind"] == "tool_call"]
    assert calls[0]["meta"] == {"tool": "Read", "path": "/w/app/x.py"} and calls[1]["meta"]["added"] == 4
    assert [f["seq"] for f in s.queue] == list(range(len(s.queue)))
    assert s.queue[0]["kind"] == "status" and s.queue[0]["meta"]["state"] == "working"


def test_a_half_written_line_waits_and_a_bad_line_is_skipped(tmp_path):
    s, path = streamer(tmp_path)
    write(path, raw=["not json at all\n", json.dumps(A({"type": "text", "text": "one"}))[:20]])
    s.poll()
    assert not [f for f in s.queue if f["kind"] == "assistant_text"]
    write(path, raw=[json.dumps(A({"type": "text", "text": "one"}))[20:] + "\n"])
    s.poll()
    assert [f["text"] for f in s.queue if f["kind"] == "assistant_text"] == ["one"]


def test_secrets_are_scrubbed_in_every_frame_kind(tmp_path):
    s, path = streamer(tmp_path)
    write(path, A({"type": "text", "text": "the token is ghp_" + "a1B2" * 8 + " and password=hunter2"},
                  tu("t1", "Bash", command="curl -H 'Authorization: Bearer abc.def.ghi' https://x.test?api_key=SECRET123")),
          U(tr("t1", "AWS key AKIA" + "ABCDEFGHIJKLMNOP\npassword: hunter3")))
    s.poll()
    blob = json.dumps(s.queue)
    for secret in ("ghp_" + "a1B2" * 8, "hunter2", "abc.def.ghi", "SECRET123", "AKIA" + "ABCDEFGHIJKLMNOP", "hunter3"):
        assert secret not in blob


def test_a_deep_path_is_kept_not_taken_for_a_secret():
    text, meta = activity.describe_tool("Read", {"file_path": "/home/u/work/app/templates/partials/job_activity_status_card_view.html"})
    assert text.endswith("job_activity_status_card_view.html") and "[removed]" not in text and meta["tool"] == "Read"


def test_text_and_results_are_capped_and_a_frame_always_fits_a_message(tmp_path):
    s, path = streamer(tmp_path)
    write(path, A({"type": "text", "text": ("word é " * 3000)}, tu("t1", "Bash", command="echo " + "x " * 500)), U(tr("t1", "line\n" * 1000)))
    s.poll()
    texts = [f for f in s.queue if f["kind"] != "status"]
    assert len(texts[0]["text"]) <= activity.MAX_TEXT + 1
    assert len(texts[1]["text"]) <= activity.COMMAND_CHARS + 3 and len(texts[2]["text"]) <= activity.RESULT_CHARS + 1
    for f in s.queue:
        assert activity.frame_size(f) <= activity.MAX_MESSAGE - 400 + 5
    assert all(len(json.dumps({"type": "job_activity", "frames": b}, separators=(",", ":")).encode()) < 8192 for b in [activity.pack(s.queue)[0]])


def test_the_job_total_is_capped_then_one_truncated_note(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "MAX_TOTAL", 3000)
    s, path = streamer(tmp_path)
    for i in range(10):
        write(path, A({"type": "text", "text": f"message {i} " + "lorem ipsum " * 60}))
    s.poll()
    notes = [f for f in s.queue if f["kind"] == "note"]
    assert len(notes) == 1 and notes[0]["meta"] == {"truncated": True} and "truncated" in notes[0]["text"]
    assert sum(len(f["text"]) for f in s.queue if f["kind"] == "assistant_text") <= 3000
    write(path, A({"type": "text", "text": "more"}))
    n = len(s.queue)
    s.poll()
    assert len(s.queue) == n                                              # nothing after the note


def test_pack_splits_a_backlog_into_messages_under_the_socket_limit():
    frames = [{"seq": i, "speaker": "claude", "kind": "assistant_text", "text": "x y " * 400, "meta": {}} for i in range(20)]
    sent = 0
    rest = frames
    while rest:
        batch, rest = activity.pack(rest)
        assert batch and len(json.dumps({"type": "job_activity", "id": "r99999999", "job_id": 99999999, "fence": 99999999, "seq": 99999, "frames": batch},
                                         separators=(",", ":")).encode()) <= 7168
        sent += len(batch)
    assert sent == 20


# --- gemini ---------------------------------------------------------------------------------------------------------------------------------

def test_gemini_turns_are_a_second_speaker(tmp_path):
    g = tmp_path / "gemini-activity.jsonl"
    g.write_text(json.dumps({"k": "question", "text": "[plan] Should we shard? token=abcd1234secretvalue"}) + "\n" +
                 json.dumps({"k": "reply", "text": "Yes, by tenant."}) + "\n" + json.dumps({"k": "error", "text": "Gemini did not answer: 429"}) + "\n" +
                 json.dumps({"k": "weird", "text": "x"}) + "\n")
    s, path = streamer(tmp_path, gemini=g)
    write(path, A({"type": "text", "text": "Asking Gemini."}))
    s.poll()
    gem = [(f["speaker"], f["kind"], f["text"]) for f in s.queue if f["speaker"] == "gemini"]
    assert [x[:2] for x in gem] == [("gemini", "tool_call"), ("gemini", "assistant_text"), ("gemini", "note")]
    assert "abcd1234secretvalue" not in gem[0][2] and gem[1][2] == "Yes, by tenant."


def test_the_gemini_mcp_logs_the_question_and_the_reply(tmp_path):
    log = tmp_path / "g.jsonl"
    seen = []
    srv = gemini_mcp.Server(env={"CB_GEMINI_ACTIVITY": str(log), "CB_GEMINI_FENCE": "3", "CB_GEMINI_KEY_FILE": str(tmp_path / "k")},
                            call=lambda name, args: seen.append(name) or {"structuredContent": {"use_id": 1}},
                            review=lambda prep, key, model: {"review": "<<<UNTRUSTED OUTPUT [ab]>>>\nShard by tenant.\n<<<END UNTRUSTED OUTPUT [ab]>>>", "bytes_out": 5})
    (tmp_path / "k").write_text("AIza" + "x" * 35)
    import os
    os.chmod(tmp_path / "k", 0o600)
    srv.second_opinion({"kind": "plan", "text": "Plan: shard the db. password=hunter2", "question": "ok?"})
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert [r["k"] for r in rows] == ["question", "reply"] and "Shard by tenant." in rows[1]["text"] and "<<<" not in rows[1]["text"]
    assert "hunter2" not in rows[0]["text"] and "Plan: shard" in rows[0]["text"]


# --- no transcript / unreadable / the socket ----------------------------------------------------------------------------------------------

def test_a_missing_transcript_says_so_once_and_never_raises(tmp_path):
    now = [0.0]
    s, _ = streamer(tmp_path, clock=lambda: now[0])
    s.poll()
    assert s.queue == []
    now[0] = activity.FIND_SECONDS + 1
    s.poll(), s.poll()
    assert [(f["kind"], f["text"], f["meta"]["state"]) for f in s.queue] == [("status", activity.UNAVAILABLE, "unavailable")]


def test_a_format_we_do_not_know_is_unavailable_not_a_crash(tmp_path):
    s, path = streamer(tmp_path)
    write(path, raw=["garbage\n"] * activity.UNREADABLE_LINES)
    s.poll()
    assert [f["meta"]["state"] for f in s.queue] == ["unavailable"]


def test_the_transcript_is_found_by_session_id_even_in_another_project_folder(tmp_path):
    home = tmp_path / "c"
    (home / "projects" / "-some-other-slug").mkdir(parents=True)
    f = home / "projects" / "-some-other-slug" / f"{SID}.jsonl"
    f.write_text("{}\n")
    assert activity.transcript_path("/w/x", SID, home) == f
    assert activity.transcript_path("/w/x", "../../etc/passwd", home) is None and activity.transcript_path("/w/x", str(uuid.uuid4()), home) is None
    assert activity.cwd_slug("/tmp/claude-1000/scratch89") == "-tmp-claude-1000-scratch89"


def test_frames_are_sent_with_the_fence_and_a_stale_fence_ends_the_stream(tmp_path):
    link = Link()
    s, path = streamer(tmp_path, link)
    write(path, A({"type": "text", "text": "hi"}))
    s.poll()
    assert run(s.flush()) is True
    msg = link.sent[0]
    assert msg["type"] == "job_activity" and msg["job_id"] == 7 and msg["fence"] == 3 and msg["seq"] == msg["frames"][0]["seq"] == 0
    link.replies = [{"ok": False, "status": 409}]
    write(path, A({"type": "text", "text": "late"}))
    s.poll()
    assert run(s.flush()) is False and s.stopped and s.queue == []
    write(path, A({"type": "text", "text": "later"}))
    s.poll()
    assert run(s.flush()) is True and len(link.sent) == 2 and s.queue == []   # the refused batch was the last one sent


def test_a_down_socket_keeps_the_frames_for_the_next_try(tmp_path):
    link = Link([ConnectionError("down")])
    s, path = streamer(tmp_path, link)
    write(path, A({"type": "text", "text": "hi"}))
    s.poll()
    assert run(s.flush()) is True and link.sent == [] and s.queue
    assert run(s.flush()) is True and link.sent and not s.queue


def test_a_failing_stream_never_raises_out_of_run(tmp_path):
    s, _ = streamer(tmp_path)
    s.poll = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    run(s.run())                                                           # logged, not raised


def test_a_permission_prompt_and_the_session_link_come_from_the_screen(tmp_path):
    screen = {"v": ("Bash", "https://claude.ai/code/session_abc12345")}
    s, _ = streamer(tmp_path, screen=lambda: screen["v"])
    s.look_at_screen()
    s.look_at_screen()
    kinds = [(f["kind"], f["meta"]) for f in s.queue]
    assert ("permission_request", {"tool": "Bash"}) in kinds and sum(k == "permission_request" for k, _ in kinds) == 1
    assert any(m.get("session_url") == "https://claude.ai/code/session_abc12345" for _, m in kinds)
    screen["v"] = ("", "javascript:alert(1)")
    s.look_at_screen()
    assert len(s.queue) == 2                                               # a link that is not claude.ai/code is never passed on


# --- how Claude Code is started --------------------------------------------------------------------------------------------------------

JOB = {"job_id": 5, "task_id": 9, "fence": 2, "branch": "dash/j5-e2", "profile": {"allowed_tools": ["Read"]}}


def test_the_session_is_named_so_its_transcript_is_known():
    api = spawn.argv_for("api", JOB, "/m.json", claude="claude", session_id=SID)
    assert api[api.index("--session-id") + 1] == SID and "--no-session-persistence" not in api
    sub = spawn.argv_for("subscription", JOB, "/m.json", claude="claude", session_id=SID)
    assert sub[sub.index("--session-id") + 1] == SID and sub[-1].startswith("You are running")
    assert "--no-session-persistence" in spawn.argv_for("api", JOB, "/m.json", claude="claude")      # unchanged without one
    assert "--session-id" not in spawn.argv_for("api", JOB, "/m.json", claude="claude", session_id="../x")


# --- sensitive files and the environment (review 1) ----------------------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.mark.parametrize("name,args", [("Read", {"file_path": "/w/.env"}), ("Read", {"file_path": "/home/u/.netrc"}), ("Read", {"file_path": "/home/u/.pgpass"}),
                                       ("Read", {"file_path": "/home/u/.docker/config.json"}), ("Read", {"file_path": "/w/certs/server.pem"}),
                                       ("Read", {"file_path": "/home/u/.ssh/id_ed25519"}), ("Grep", {"pattern": "PASS", "path": "/w/.env.production"}),
                                       ("Bash", {"command": "cat ~/.netrc"}), ("Bash", {"command": "printenv"}), ("Bash", {"command": "env | grep TOKEN"}),
                                       ("Bash", {"command": "export -p"}), ("Bash", {"command": "ls && grep -n pass .git-credentials"})])
def test_a_result_that_read_a_sensitive_file_or_the_environment_is_hidden(tmp_path, name, args):
    s, path = streamer(tmp_path)
    write(path, A(tu("t1", name, **args)), U(tr("t1", "machine example login bob password hunter2\nADMIN_PIN=4821\nPGPASSWORD=sekrit")))
    s.poll()
    res = [f for f in s.queue if f["kind"] == "tool_result"][0]
    assert res["text"] == activity.HIDDEN and res["meta"]["status"] == "hidden"
    assert "hunter2" not in json.dumps(s.queue) and "4821" not in json.dumps(s.queue)


def test_an_ordinary_read_and_command_still_show_their_output(tmp_path):
    s, path = streamer(tmp_path)
    write(path, A(tu("t1", "Read", file_path="/w/app/config.json"), tu("t2", "Bash", command="cat README.md && ls")), U(tr("t1", "{}"), tr("t2", "hello")))
    s.poll()
    assert [f["text"] for f in s.queue if f["kind"] == "tool_result"] == ["{}", "hello"]


def test_a_grep_result_that_lists_a_sensitive_file_is_hidden(tmp_path):
    s, path = streamer(tmp_path)
    write(path, A(tu("t1", "Grep", pattern="PIN", path="/w")), U(tr("t1", "/w/.env:3:ADMIN_PIN=4821\n/w/app/a.py:9:PIN = 1")))
    s.poll()
    assert [f["text"] for f in s.queue if f["kind"] == "tool_result"] == [activity.HIDDEN]


def test_a_secret_looking_assignment_loses_its_value_everywhere():
    for line in ("ADMIN_PIN=1234", "PGPASSWORD=hunter2 psql -h x", "wifi_psk='abc def'", "export API_KEY=\"x y\"", "db_secret=abc", "GITHUB_TOKEN=abc", "My_Auth=zzz", "SOME_CRED=q"):
        out = activity.clean(line)
        assert "[removed]" in out and not any(v in out for v in ("1234", "hunter2", "abc def", "x y", "=abc", "zzz", "=q")), out
    assert activity.clean("shipping=fast mapping=1 name=bob") == "shipping=fast mapping=1 name=bob"
    s_text, _ = activity.describe_tool("Bash", {"command": "PGPASSWORD=hunter2 psql"})
    assert "hunter2" not in s_text


# --- the session link (review 5) ----------------------------------------------------------------------------------------------------------

def test_a_realistic_session_link_is_kept_whole_and_anything_else_is_not(tmp_path):
    good = "https://claude.ai/code/session_01Ax3qtxi34QrcUFCpPkumxE"
    assert activity.SESSION_URL.fullmatch(good) and activity.SESSION_FIND.search("see " + good + " now").group(0) == good
    s, _ = streamer(tmp_path, screen=lambda: ("", good))
    s.look_at_screen()
    assert s.queue[0]["meta"]["session_url"] == good
    for bad in ("https://claude.ai/code/session_x", "https://claude.ai/code/session_abc1234545/../x", "https://evil.test/https://claude.ai/code/session_abc1234545",
                "http://claude.ai/code/session_abc1234545", "https://claude.ai/code/session_abc1234545?x=1"):
        assert not activity.SESSION_URL.fullmatch(bad)


# --- the screen probe (review 6) ------------------------------------------------------------------------------------------------------------

def test_the_permission_probe_reports_a_prompt_only_when_the_screen_has_stopped_changing(tmp_path, monkeypatch):
    from crewboss_runner import guard, worker
    log = tmp_path / "agent.log"
    log.write_text("Do you want to proceed? 1. Yes 2. No")
    monkeypatch.setattr(guard, "permission_prompt_tool", lambda text: "Bash")

    class Agent:
        log_path = str(log)

    probe = worker.Worker._screen_probe(Agent)
    assert probe()[0] == ""                       # first look: the screen just changed
    assert probe()[0] == "Bash"                   # unchanged since: waiting for a person
    log.write_text(log.read_text() + " more output")
    assert probe()[0] == ""                       # output is still arriving


# --- concurrency (review 7 and the queue) -------------------------------------------------------------------------------------------------

def test_drain_waits_for_a_poll_that_is_still_running_and_nothing_is_read_twice(tmp_path):
    import threading
    s, path = streamer(tmp_path)
    write(path, A({"type": "text", "text": "once"}))
    entered, release = threading.Event(), threading.Event()
    real = s._poll

    def slow():
        entered.set()
        release.wait(5)
        real()

    s._poll = slow
    t = threading.Thread(target=s.poll)
    t.start()
    entered.wait(5)
    res = []
    d = threading.Thread(target=lambda: res.append(run(s.drain())))
    d.start()
    d.join(0.3)
    assert d.is_alive()                           # drain is waiting for the first poll, not reading the same lines itself
    release.set()
    t.join(5), d.join(5)
    assert [f["text"] for f in s.queue + [x for m in s.link.sent for x in m["frames"]] if f["kind"] == "assistant_text"] == ["once"]


def test_frames_read_while_a_batch_is_in_flight_are_not_lost(tmp_path):
    s, path = streamer(tmp_path)
    write(path, A({"type": "text", "text": "first"}))
    s.poll()

    class SlowLink(Link):
        once = []

        async def rpc(self, mtype, timeout=30.0, **fields):
            if not self.once:
                self.once.append(1)
                write(path, A({"type": "text", "text": "second"}))
                s.poll()                          # the next frames arrive while this batch is being sent
            return await super().rpc(mtype, timeout, **fields)

    s.link = SlowLink()
    assert run(s.flush()) is True
    sent = [f["text"] for m in s.link.sent for f in m["frames"] if f["kind"] == "assistant_text"]
    assert sent == ["first", "second"] and not s.queue


# --- API mode keeps no transcript (review 2) ---------------------------------------------------------------------------------------------

def test_forget_removes_the_sessions_files_and_only_that_session(tmp_path):
    s, path = streamer(tmp_path)
    home = tmp_path / "claude"
    other = str(uuid.uuid4())
    d = path.parent
    write(path, A({"type": "text", "text": "x"}))
    (d / SID).mkdir()
    (d / SID / "tool-results.txt").write_text("output")
    (d / f"{other}.jsonl").write_text("{}\n")
    (home / "file-history" / SID).mkdir(parents=True)
    (home / "todos").mkdir()
    (home / "todos" / f"{SID}-agent.json").write_text("[]")
    assert s.forget() >= 4
    assert not path.exists() and not (d / SID).exists() and not (home / "file-history" / SID).exists() and not list((home / "todos").glob(f"{SID}*"))
    assert (d / f"{other}.jsonl").exists()                                   # another session is untouched
    s.session_id = "../x"
    assert s.forget() == 0
