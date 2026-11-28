"""ONE TIMELINE for the task window (#91): everything that happened on a task, oldest first, chat-style.

`build(task, viewer_id)` -> {items, waiting_ids, first_waiting}. Each item is a dict with `kind`, `at` (a datetime), an `id` for the DOM, and what its
partial prints. Kinds: comment, ai_group (consecutive AI comments folded into one row), question (and its answer), run, change (status / priority /
assignee / title changes from the activity log), decision (a Change-log entry linked to the task), release (a merge request's steps), deploy, thread
(a Discussion with its messages).

Who may see it: this is the STAFF task window only. A client's mini dashboard is a separate router that never includes it (title, status and
progress only); `viewer_id` gates the Discussions (a task's conversations are shared with everyone who can see the task, #830).

All text here is data: templates autoescape it, and `render_text` is the one place a little Markdown is turned into HTML, escaped first.

`job_events(task_id)` is THE function that returns a task's job events for the timeline. Change #987 owns the job card and may replace its body
(same name, same row shape: id, state, at, finished_at, profile, device, branch, summary, user_name); until then it reads the jobs table."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from markupsafe import Markup

from app import repo
from app.db import get_cursor

CLAMP_CHARS = 320
CLAMP_LINES = 5
AI_GROUP_MIN = 2


# THE AI WRITING RULE for task comments (#91): the timeline shows the first paragraph and folds the rest under "Details".
WRITING_RULE = ("Lead with the outcome in 1-2 short lines; put details in a folded section: after those lines, a line holding only <details>, "
                "then the details (a blank line separates paragraphs; Markdown is fine).")

_HUMAN = re.compile(r"(?:owner|user:[0-9]{1,10}|ca:[0-9]{1,10}|client:.*)")
_CHANGE = re.compile(r"^(status|priority|assigned to|title|stream|category)\b", re.I)


def is_human(author: str) -> bool:
    return bool(_HUMAN.fullmatch(author or ""))


def _utc(dt):
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- text: a folded section and safe Markdown ----------------------------------------------------------------------------

_FOLD = re.compile(r"^[ \t]*(?:<details[^>\n]*>|:::[ \t]*details|\+\+\+)[ \t]*$", re.I | re.M)
_FOLD_END = re.compile(r"^[ \t]*(?:</details>|:::|\+\+\+)[ \t]*$", re.I | re.M)
_SUMMARY_TAG = re.compile(r"</?summary[^>\n]*>", re.I)


def split_fold(body: str) -> tuple[str, str]:
    """(lead, details). The AI writing rule: the outcome in 1-2 short lines, then the details after a `<details>` line (or `:::details`, or
    `+++`). No marker = no fold."""
    text = body or ""
    m = _FOLD.search(text)
    if not m:
        return text.strip(), ""
    lead, rest = text[: m.start()].strip(), text[m.end():]
    e = _FOLD_END.search(rest)
    if e:
        rest = rest[: e.start()]
    rest = _SUMMARY_TAG.sub("", rest).strip()
    return lead, rest


_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```[ \t]*$", re.S | re.M)
_INLINE = [
    (re.compile(r"`([^`\n]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])"), r"<em>\1</em>"),
]
_BULLET = re.compile(r"^\s*[-*] ")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)<>\"']+)\)")


def render_text(text: str | None) -> Markup:
    """A small, SAFE Markdown: paragraphs, line breaks, **bold**, *italic*, `code`, fenced code, "- " lists and [text](http/https url) links. The text is
    HTML-escaped FIRST, so nothing a person or an agent wrote can become markup; the only tags are the ones built here."""
    src = (text or "").replace("\r\n", "\n").strip()
    if not src:
        return Markup("")
    blocks: list[str] = []

    def _fence(m):
        blocks.append("<pre><code>" + html.escape(m.group(1).rstrip("\n")) + "</code></pre>")
        return f"\x00{len(blocks) - 1}\x00"

    src = _FENCE.sub(_fence, src)
    out: list[str] = []
    for para in re.split(r"\n{2,}", src):
        lines = para.split("\n")
        if all(re.match(r"^\s*[-*] ", ln) for ln in lines if ln.strip()):
            out.append("<ul>" + "".join("<li>" + _inline(_BULLET.sub("", ln)) + "</li>" for ln in lines if ln.strip()) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(_inline(ln) for ln in lines) + "</p>")
    res = "".join(out)
    res = re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], res)
    return Markup(res)


def _inline(line: str) -> str:
    s = html.escape(line, quote=True)
    s = _LINK.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer nofollow">{m.group(1)}</a>', s)
    for pat, rep in _INLINE:
        s = pat.sub(rep, s)
    return s


def _long(text: str) -> bool:
    return len(text) > CLAMP_CHARS or text.count("\n") >= CLAMP_LINES


# --- the sources ---------------------------------------------------------------------------------------------------------

def job_events(task_id: int) -> list[dict]:
    """Every job (run) on the task, oldest first, one row each, with #987's event lines for it (app.job_card.job_events: started, claimed,
    finished / needs more work / waiting on you / failed / stopped, each {at, kind, title, body, link}) under `events`."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT j.id, j.state, j.branch, j.summary, j.created_at AS at, j.finished_at, p.name AS profile, d.name AS device, "
                    "COALESCE(NULLIF(u.display_name, ''), 'Someone') AS user_name "
                    "FROM jobs j JOIN agent_profiles p ON p.id = j.profile_id JOIN users u ON u.id = j.user_id "
                    "LEFT JOIN device_tokens d ON d.id = j.device_id WHERE j.task_id = %s ORDER BY j.id LIMIT 100", (task_id,))
        rows = [dict(r) for r in cur.fetchall()]
    from app import job_card
    by_job: dict[int, list[dict]] = {}
    for e in job_card.job_events(task_id, 100):
        by_job.setdefault(e["job_id"], []).append(e)
    for r in rows:
        r["events"] = by_job.get(r["id"], [])
        last = r["events"][-1] if r["events"] else None
        r["headline"] = last["title"] if last else r["state"]
        r["link"] = next((e["link"] for e in reversed(r["events"]) if e["link"]), "")
    return rows


def _changes(task_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, actor, summary, at FROM activity WHERE entity = 'task' AND entity_id = %s AND verb IN ('updated', 'assigned', 'moved') "
                    "ORDER BY at, id LIMIT 200", (task_id,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        tail = r["summary"].split("': ", 1)[-1] if "': " in r["summary"] else r["summary"]
        parts = [p.strip() for p in tail.split(";") if _CHANGE.match(p.strip())]
        if parts:
            out.append({"kind": "change", "id": f"h{r['id']}", "at": r["at"], "actor": r["actor"], "text": "; ".join(parts)})
    return out


def _decisions(task_id: int, viewer_id: int | None = None) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, project_id, title, stage, decision, decided_at, created_at FROM change_log WHERE links->'tasks' @> to_jsonb(%s::int) "
                    "ORDER BY id LIMIT 20", (task_id,))
        rows = cur.fetchall()
    if viewer_id:       # (no viewer = sign-in is off on this instance, so there are no per-person rights) a Change-log entry may belong to a project the viewer cannot see: only the ones they can
        from app import access
        p = access.principal_for_user_id(viewer_id)
        rows = [r for r in rows if access.can_view(p, r["project_id"])]
    return [{"kind": "decision", "id": f"c{r['id']}", "at": r["decided_at"] or r["created_at"], "change_id": r["id"], "title": r["title"],
             "stage": r["stage"], "decision": r["decision"] or ""} for r in rows]


def _releases(task_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT m.id, m.branch, m.state, m.requested_at, m.audit_passed_at, m.tests_green_at, m.merged_at, m.decided_at "
                    "FROM merge_requests m JOIN jobs j ON j.id = m.job_id WHERE j.task_id = %s ORDER BY m.id LIMIT 20", (task_id,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({"kind": "release", "id": f"m{r['id']}-1req", "at": r["requested_at"], "step": "requested", "branch": r["branch"]})
        if r["audit_passed_at"]:
            out.append({"kind": "release", "id": f"m{r['id']}-2aud", "at": r["audit_passed_at"], "step": "reviewed", "branch": r["branch"]})
        if r["merged_at"]:
            out.append({"kind": "release", "id": f"m{r['id']}-3mrg", "at": r["merged_at"], "step": "merged", "branch": r["branch"]})
    return out


def _deploys(task_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, target_name, state, requested_at AS created_at FROM server_jobs WHERE task_id = %s AND NOT rollback ORDER BY id LIMIT 20", (task_id,))
        rows = cur.fetchall()
    return [{"kind": "deploy", "id": f"d{r['id']}", "at": r["created_at"], "target": r["target_name"], "state": r["state"]} for r in rows]


def _threads(task_id: int, viewer_id: int | None) -> list[dict]:
    """Discussions as threads: each conversation bound to the task, with its messages, for the people who may open it. A conversation that is only
    a soft link (someone else's chat that changed the task) is one line with no content."""
    if not viewer_id:
        return []
    from app import ca
    try:
        found = ca.task_discussion(viewer_id, task_id, rights_checked=True)["items"]
    except Exception:
        return []
    out = []
    for c in found:
        if not c.get("open") or not c.get("id"):
            out.append({"kind": "thread", "id": f"v{c.get('id') or 'x'}-{len(out)}", "at": c.get("updated_at"), "open": False, "by": c.get("by", "Someone"),
                        "linked": bool(c.get("linked")), "title": "", "messages": [], "conv": None})
            continue
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT role, user_id, content, created_at FROM ca_messages WHERE conversation_id = %s ORDER BY id LIMIT 60", (c["id"],))
            msgs = [{"role": m["role"], "user_id": m["user_id"], "mine": m["user_id"] == viewer_id, "author": _display(m["user_id"]), "text": m["content"] or "", "at": m["created_at"]} for m in cur.fetchall() if (m["content"] or "").strip()]
        out.append({"kind": "thread", "id": f"v{c['id']}", "at": (msgs[0]["at"] if msgs else c.get("updated_at")), "open": True, "by": c.get("by", "Someone"),
                    "linked": bool(c.get("linked")), "title": c.get("title") or "Discussion", "messages": msgs, "conv": c["id"],
                    "last_at": c.get("updated_at")})
    return out


def _display(user_id) -> str:
    if not user_id:
        return "Someone"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT COALESCE(NULLIF(display_name, ''), 'Someone') AS n FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    return row["n"] if row else "Someone"


# --- build ---------------------------------------------------------------------------------------------------------------

def build(task: dict, viewer_id: int | None = None) -> dict:
    tid = task["id"]
    items: list[dict] = []
    for c in task.get("comments", []):
        lead, more = split_fold(c["body"])
        items.append({"kind": "comment", "id": f"k{c['id']}", "at": c["created_at"], "author": c["author"], "human": is_human(c["author"]),
                      "lead": lead, "more": more, "clamp": _long(lead)})
    for q in task.get("questions", []):
        items.append({"kind": "question", "id": f"q{q['id']}", "at": q["asked_at"], "q": q, "open": q.get("answered_at") is None})
        if q.get("answered_at"):
            items.append({"kind": "answer", "id": f"a{q['id']}", "at": q["answered_at"], "q": q})
    for j in job_events(tid):
        items.append({"kind": "run", "id": f"r{j['id']}", "job": j, "at": j["at"]})
    items += _changes(tid) + _decisions(tid, viewer_id) + _releases(tid) + _deploys(tid) + _threads(tid, viewer_id)
    items = [i for i in items if i.get("at") is not None]
    items.sort(key=lambda i: (_utc(i["at"]), i["id"]))
    items = merge_ai_runs(items)
    waiting = [i["id"] for i in items if i["kind"] == "question" and i.get("open")]
    return {"items": items, "waiting_ids": waiting, "first_waiting": waiting[0] if waiting else None}


def merge_ai_runs(items: list[dict]) -> list[dict]:
    """Consecutive AI comments (nothing else between them) become one folded row: "4 updates from Claude"."""
    out: list[dict] = []
    run: list[dict] = []

    def flush():
        if len(run) >= AI_GROUP_MIN:
            out.append({"kind": "ai_group", "id": "g" + run[0]["id"], "at": run[0]["at"], "last_at": run[-1]["at"], "items": list(run),
                        "who": _who(run), "n": len(run)})
        else:
            out.extend(run)
        run.clear()

    for i in items:
        if i["kind"] == "comment" and not i["human"]:
            run.append(i)
        else:
            flush()
            out.append(i)
    flush()
    return out


def _who(run: list[dict]) -> str:
    names = {("Claude" if r["author"].startswith(("claude", "claude-desktop")) else "Gemini" if r["author"].startswith("gemini") else "Crewboss") for r in run}
    return names.pop() if len(names) == 1 else "the AI"


# --- the code line -------------------------------------------------------------------------------------------------------

def compare_url(remote: str | None, branch: str | None) -> str:
    """The branch on GitHub when the remote is a GitHub one (a compare page against the default branch); '' otherwise (the name is shown, not linked)."""
    m = re.match(r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?$", (remote or "").strip())
    if not (m and branch and re.fullmatch(r"[\w./-]+", branch)):
        return ""
    return f"https://github.com/{m.group(1)}/compare/{branch}?expand=1"


def code_line(task: dict, project: dict | None) -> dict | None:
    """The pinned code line: the latest job branch, the "View changes" link, and the chips Branch -> Reviewed -> Merged -> Deployed, each lit from
    the data that exists (a job branch; a merge request's audit and merge; a deploy that finished) and grey where there is none. None = no branch yet."""
    tid = task["id"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT j.id, j.branch FROM jobs j WHERE j.task_id = %s AND j.branch <> '' ORDER BY j.id DESC LIMIT 1", (tid,))
        job = cur.fetchone()
        if not job:
            return None
        cur.execute("SELECT audit_passed_at, merged_at, state FROM merge_requests WHERE job_id IN (SELECT id FROM jobs WHERE task_id = %s) "
                    "ORDER BY id DESC LIMIT 1", (tid,))
        mr = cur.fetchone()
        cur.execute("SELECT 1 FROM server_jobs WHERE task_id = %s AND state = 'done' AND NOT rollback LIMIT 1", (tid,))
        deployed = bool(cur.fetchone())
    branch = job["branch"]
    chips = [("Branch", True), ("Reviewed", bool(mr and mr["audit_passed_at"])), ("Merged", bool(mr and mr["merged_at"])), ("Deployed", deployed)]
    return {"branch": branch, "url": compare_url((project or {}).get("git_remote"), branch), "chips": chips}


def now_sentence(task: dict, run: dict | None, waiting: bool) -> str:
    """The NOW line's words (the stub of partials/task_now.html; #987 owns the real wording of the job states)."""
    job = (run or {}).get("job")
    if waiting:
        return "Waiting on you: there is a question to answer"
    if job and job["state"] == "running":
        return f"Running on {job.get('device') or 'a runner'}"
    if job and job["state"] == "queued":
        return "Queued, waiting for a runner"
    if task["status"] == "done":
        return "Done"
    if task["status"] == "review":
        return "Ready for you to review"
    if job and job["state"] == "done":
        return "The last run finished: review it, or run it again"
    if job and job["state"] in ("failed", "cancelled"):
        return f"The last run {job['state']}"
    return "Ready to run" if task["status"] in ("ready", "backlog", "doing") else ""
