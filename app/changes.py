"""The CHANGE LOG (owner, 2026-10-01; dashboard k33, task #134): record every significant change BEFORE it's made.

Each entry moves through three stages:
  recommended  an AI session (or the owner) records what, why, the options and their trade-offs
  decided      the OWNER's call: approved / rejected / changed (with a note), and when
  done         the tasks, commits and deploys that made it, linked; verified once the check passes

Rules: only the owner decides. A session may record a decision the owner gave it in the conversation
(record_decision, task #581): VERIFIED when the quote is in a prompt the owner typed in that session lately (it then
counts and tells the session), else UNVERIFIED (stored, no event, not an approval until the owner confirms). The owner
sees every one on Now and can acknowledge or override it. A session may recommend on any project it works (scope.py) and may mark a change
done only after an approved (or changed) decision. A decision tells the project's session with an ids-only outbox
event, like every owner event (no free text crosses into a session).
"""
from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from app import access, repo
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound, is_owner, record_activity

STAGES = ("recommended", "decided", "done")
DECISIONS = ("approved", "rejected", "changed")
TITLE_MAX = 200
RECOMMENDATION_MAX = 8000
OPTION_MAX = 300
MAX_OPTIONS = 6
LINK_KEYS = ("tasks", "commits", "questions", "knowledge", "docs", "deploys")


def _clean_links(links) -> dict:
    """Only known keys, each a short list of ints (tasks/questions/knowledge) or short strings (commits/docs/
    deploys). Anything else is dropped, so one odd shape can't break a page."""
    if not isinstance(links, dict):
        return {}
    out: dict = {}
    for k in LINK_KEYS:
        v = links.get(k)
        if not isinstance(v, list):
            continue
        if k in ("tasks", "questions", "knowledge"):
            vals = [int(x) for x in v if isinstance(x, int) and not isinstance(x, bool)
                    or isinstance(x, str) and x.isdigit()]
        else:
            vals = [repo._one_line(x, 300) for x in v if isinstance(x, str) and x.strip()]
        if vals:
            out[k] = vals[:50]
    return out


def _merge_links(old: dict, new: dict) -> dict:
    merged = dict(old or {})
    for k, v in (new or {}).items():
        merged[k] = list(dict.fromkeys((merged.get(k) or []) + v))[:50]
    return merged


def _clean_options(options) -> list[str] | None:
    if not options:
        return None
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise BadRequest("options must be a list of short strings")
    cleaned = [repo._one_line(o, OPTION_MAX) for o in options if o.strip()]
    if len(cleaned) > MAX_OPTIONS:
        raise BadRequest(f"at most {MAX_OPTIONS} options")
    return cleaned or None


def recommend(project_id: int, title: str, recommendation: str, actor: str, options=None, links=None,
              cur=None, backfill_key: str | None = None) -> dict:
    """Record a recommended change. `recommendation` = what, why, and the trade-offs (Markdown)."""
    title = repo._one_line(title or "", TITLE_MAX)
    if not title:
        raise BadRequest("give the change a one-line title")
    recommendation = (recommendation or "").strip()
    if len(recommendation) > RECOMMENDATION_MAX:
        raise BadRequest(f"keep the recommendation under {RECOMMENDATION_MAX} characters")
    opts = _clean_options(options)
    lk = _clean_links(links)

    def _do(c):
        c.execute(
            "INSERT INTO change_log (project_id, title, recommendation, options, recommended_by, links, backfill_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (project_id, title, recommendation, Jsonb(opts) if opts else None, actor, Jsonb(lk), backfill_key),
        )
        row = c.fetchone()
        record_activity(c, project_id, actor, "recommended", "change", row["id"],
                        f"Change #{row['id']} recommended: {title}")
        return row

    if cur is not None:
        return _do(cur)
    with get_cursor() as c:
        return _do(c)


def record_structural(project_id: int, title: str, actor: str, recommendation: str = "", links=None,
                      cur=None) -> dict:
    """A structural change the OWNER made directly — moving a project in the tree (task #166), say — logged straight
    to 'done': the owner recommends, decides and does it in one move, so it shows under the change log's Done. Owner
    only (the page's actor is always 'owner', behind the same-origin guard)."""
    if not is_owner(actor):
        raise Forbidden("only the owner records a structural change")
    title = repo._one_line(title or "", TITLE_MAX)
    if not title:
        raise BadRequest("give the change a one-line title")
    lk = _clean_links(links)

    def _do(c):
        c.execute(
            "INSERT INTO change_log (project_id, title, recommendation, recommended_by, stage, decision, "
            "decided_by, decided_at, links, done_at, verified) "
            "VALUES (%s, %s, %s, %s, 'done', 'approved', %s, now(), %s, now(), true) RETURNING *",
            (project_id, title, (recommendation or "").strip()[:RECOMMENDATION_MAX], actor, actor, Jsonb(lk)),
        )
        row = c.fetchone()
        record_activity(c, project_id, actor, "done", "change", row["id"],
                        f"Change #{row['id']} done: {title}")
        return row

    if cur is not None:
        return _do(cur)
    with get_cursor() as c:
        return _do(c)


def get(change_id: int) -> dict:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM change_log WHERE id = %s", (change_id,))
        row = cur.fetchone()
    if not row:
        raise NotFound(f"change {change_id} not found")
    return row


def _event(cur, row: dict, decision: str) -> None:
    """The ids-only 'change_decided' event to the recommending project's session."""
    project = repo.get_project(row["project_id"])
    repo.enqueue_outbox(cur, _stream_for(project), row["project_id"], None, "change_decided",
                        f"Owner decided change #{int(row['id'])} ({decision}); read it with list_changes")


def _unacknowledged_session(row: dict) -> bool:
    return row.get("decided_via") == "session" and row.get("acknowledged_at") is None


def decide(change_id: int, decision: str, actor: str, note: str = "") -> dict:
    """The owner's call on a recommended change. Tells the project's session (ids only).

    The owner may also re-decide a change a session recorded from the conversation (decided_via 'session'): while it
    is still 'decided', or even after it was made while the owner hasn't acknowledged it. The session's quote and
    name are kept (superseded_*) and named in the activity. The SAME decision only acknowledges (no second event,
    except that confirming an unverified one is the first time the session hears it). A change already made keeps
    its stage: rejecting it records the call but cannot undo the work (the row says so). One the owner decided on the
    page stands."""
    if not (is_owner(actor) or repo.is_person_actor(actor)):
        raise Forbidden("only the owner decides a change")
    if decision not in DECISIONS:
        raise BadRequest(f"decision must be one of {DECISIONS}")
    note = (note or "").strip()[:2000]
    if decision == "changed" and not note:
        raise BadRequest("say how you'd change it (a short note)")
    with get_cursor() as cur:
        cur.execute("SELECT * FROM change_log WHERE id = %s FOR UPDATE", (change_id,))
        row = cur.fetchone()
        if not row:
            raise NotFound(f"change {change_id} not found")
        repo.require_project_admin(actor, row["project_id"])   # a person other than the owner must be Admin/Owner of the project
        if row["stage"] == "recommended":
            cur.execute(
                "UPDATE change_log SET stage = 'decided', decision = %s, decision_note = %s, decided_by = %s, "
                "decided_at = now(), decided_via = 'dashboard', owner_words = '', updated_at = now(), decided_user_id = %s "
                "WHERE id = %s RETURNING *",
                (decision, note, actor, access.current_user_id(), change_id),
            )
            row = cur.fetchone()
            record_activity(cur, row["project_id"], actor, "decided", "change", change_id,
                            f"Change #{change_id} {decision}: {row['title']}")
            _event(cur, row, decision)
            return row
        overridable = row.get("decided_via") == "session" and (row["stage"] == "decided"
                                                                or row.get("acknowledged_at") is None)
        if not overridable:
            raise Conflict(f"change {change_id} was already decided")
        same = row["decision"] == decision and (decision != "changed" or note == (row["decision_note"] or ""))
        if same:
            return _acknowledge(cur, row, actor)
        done = row["stage"] == "done"
        cur.execute(
            "UPDATE change_log SET decision = %s, decision_note = %s, decided_by = %s, decided_at = now(), "
            "decided_via = 'dashboard', owner_words = '', words_verified = true, acknowledged_at = now(), "
            "superseded_owner_words = %s, superseded_by = %s, updated_at = now(), decided_user_id = %s WHERE id = %s RETURNING *",
            (decision, note, actor, row["owner_words"], row["decided_by"], access.current_user_id(), change_id),
        )
        new = cur.fetchone()
        tail = (f" It was already made, so this records the owner's call but does not undo the work."
                if done and decision == "rejected" else "")
        record_activity(cur, new["project_id"], actor, "decided", "change", change_id,
                        f"Change #{change_id} {decision} (owner override of {row['decision']} recorded by "
                        f"{row['decided_by']}, quoting \"{row['owner_words']}\"): {new['title']}.{tail}")
        _event(cur, new, decision)
        new["override_note"] = tail.strip()
        return new


def _who(cur, actor: str) -> str:
    """Who to name in the activity line: 'the owner', or the signed-in person's display name (an Admin acknowledging is not the owner)."""
    if is_owner(actor) or not repo.is_person_actor(actor):
        return "the owner"
    cur.execute("SELECT display_name FROM users WHERE id = %s", (int(actor.split(":")[1]),))
    r = cur.fetchone()
    return (r or {}).get("display_name") or actor


def _acknowledge(cur, row: dict, actor: str) -> dict:
    cur.execute("UPDATE change_log SET acknowledged_at = now(), updated_at = now() WHERE id = %s RETURNING *",
                (row["id"],))
    new = cur.fetchone()
    unverified = not row.get("words_verified", True)
    record_activity(cur, new["project_id"], actor, "acknowledged", "change", new["id"],
                    f"Change #{new['id']} {new['decision']} acknowledged by {_who(cur, actor)}"
                    f"{' (the quote was not found in the owner prompts: now confirmed)' if unverified else ''}: "
                    f"{new['title']}")
    if unverified:  # the owner's confirmation is the first time the session may act on it
        _event(cur, new, new["decision"])
    return new


def acknowledge(change_id: int, actor: str) -> dict:
    """The owner has seen a decision a session recorded in chat (Now > Decided in chat). Confirms an unverified one."""
    if not (is_owner(actor) or repo.is_person_actor(actor)):
        raise Forbidden("only the owner acknowledges a decision")
    with get_cursor() as cur:
        cur.execute("SELECT * FROM change_log WHERE id = %s FOR UPDATE", (change_id,))
        row = cur.fetchone()
        if not row:
            raise NotFound(f"change {change_id} not found")
        repo.require_project_admin(actor, row["project_id"])   # a person other than the owner must be Admin/Owner of the project
        if row.get("decided_via") != "session":
            raise Conflict(f"change {change_id} wasn't decided in chat")
        if row.get("acknowledged_at") is not None:
            return row
        return _acknowledge(cur, row, actor)


OWNER_WORDS_MAX = 1000
PROMPT_KEEP_MINUTES = 30
PROMPT_MAX = 2000          # a longer prompt is never a decision quote: not stored
PROMPTS_PER_TOKEN = 200    # rows kept per token inside the window (oldest dropped)
QUOTE_MIN_ANYWHERE = 15    # a quote this long may be found in ANY recent prompt
QUOTE_MIN_LATEST = 2       # a shorter one must be in the token's LATEST prompt


def _fold(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def prune_owner_prompts(cur=None) -> None:
    """Drop expired owner prompts (transient data: also run by the periodic sweep in main.py)."""
    def _do(c):
        c.execute("DELETE FROM owner_prompts WHERE created_at < now() - make_interval(mins => %s)",
                  (PROMPT_KEEP_MINUTES,))
    if cur is not None:
        return _do(cur)
    with get_cursor() as c:
        _do(c)


def note_owner_prompt(token_id: int, prompt: str) -> None:
    """The UserPromptSubmit hook hands us each prompt the owner typed in a session; kept ~30 minutes per token (at most
    200) so record_decision can tell a real quote from an invented one. Normalised the same way as the quote.
    A prompt over 2,000 characters is not stored."""
    text = repo._one_line(prompt or "", PROMPT_MAX + 1)
    if not text or len(text) > PROMPT_MAX:
        return
    with get_cursor() as cur:
        prune_owner_prompts(cur)
        cur.execute("INSERT INTO owner_prompts (token_id, prompt) VALUES (%s, %s)", (token_id, text))
        cur.execute("DELETE FROM owner_prompts WHERE token_id = %s AND id NOT IN "
                    "(SELECT id FROM owner_prompts WHERE token_id = %s ORDER BY id DESC LIMIT %s)",
                    (token_id, token_id, PROMPTS_PER_TOKEN))


def _words_verified(cur, token_id, words: str, change_created_at=None) -> bool:
    """Is `words` really the owner's? Case-folded, whitespace-normalised, from this token's prompts in the last 30
    minutes (expired rows are deleted first). Verified if EITHER (a) it is at least 15 characters and inside one of
    them, or (b) it (2+ characters) is inside the token's LATEST prompt and that prompt arrived after the change was
    recommended. So a bare "yes" never matches an unrelated earlier prompt."""
    if token_id is None:
        return False
    cur.execute("SELECT agent FROM session_tokens WHERE id = %s", (token_id,))
    tok = cur.fetchone()
    if not tok or tok["agent"] == "claude-desktop":
        return False  # an external agent's (#574) own "owner prompt" posts are never trusted: the owner confirms on the page
    prune_owner_prompts(cur)
    needle = _fold(words)
    if len(needle) < QUOTE_MIN_LATEST:
        return False
    cur.execute("SELECT prompt, created_at FROM owner_prompts WHERE token_id = %s ORDER BY id DESC", (token_id,))
    rows = cur.fetchall()
    if len(needle) >= QUOTE_MIN_ANYWHERE and any(needle in _fold(r["prompt"]) for r in rows):
        return True
    if rows and needle in _fold(rows[0]["prompt"]) and \
            (change_created_at is None or rows[0]["created_at"] > change_created_at):
        return True
    return False


def record_session_decision(change_id: int, decision: str, owner_words: str, actor: str, note: str = "",
                            token_id: int | None = None, comment_tasks=None) -> dict:
    """The owner decided a recommended change TO A SESSION in the conversation; the session records it with their
    verbatim words (the twin of record_answer). Stored decided_via='session'. VERIFIED when the quote appears in a
    prompt the owner typed in that session in the last 30 minutes (note_owner_prompt): then it behaves like the
    owner's own decision and tells the session. Otherwise it is stored UNVERIFIED: no event, and not an approval for
    mark_done until the owner confirms it on the page. `comment_tasks` = the linked task ids the caller may see
    (the only ones commented on). The owner sees every one (Now > Decided in chat) and can override it."""
    if is_owner(actor):
        raise BadRequest("the owner decides on the Changes page")
    if decision not in DECISIONS:
        raise BadRequest(f"decision must be one of {DECISIONS}")
    words = repo._one_line(owner_words or "", OWNER_WORDS_MAX)
    if not words:
        raise BadRequest("owner_words is required: the owner's exact words from the conversation")
    note = (note or "").strip()[:2000]
    if decision == "changed" and not note:
        raise BadRequest("say how the owner would change it (a short note)")
    with get_cursor() as cur:
        cur.execute("SELECT * FROM change_log WHERE id = %s FOR UPDATE", (change_id,))
        row = cur.fetchone()
        if not row:
            raise NotFound(f"change {change_id} not found")
        # A session may re-record (replace) its OWN decision while it is still unverified and unacknowledged, to
        # correct a case/typo slip. The earlier quote is kept as superseded.
        redo = (row["stage"] == "decided" and row.get("decided_via") == "session" and row["decided_by"] == actor
                and not row.get("words_verified", True) and row.get("acknowledged_at") is None)
        if row["stage"] != "recommended" and not redo:
            raise Conflict(f"change {change_id} was already decided")
        verified = _words_verified(cur, token_id, words, row["created_at"])
        prev_words, prev_by = (row["owner_words"], row["decided_by"]) if redo else (row["superseded_owner_words"],
                                                                                  row["superseded_by"])
        cur.execute(
            "UPDATE change_log SET stage = 'decided', decision = %s, decision_note = %s, decided_by = %s, "
            "decided_at = now(), decided_via = 'session', owner_words = %s, words_verified = %s, "
            "acknowledged_at = NULL, superseded_owner_words = %s, superseded_by = %s, updated_at = now(), decided_user_id = %s "
            "WHERE id = %s RETURNING *",
            (decision, note, actor, words, verified, prev_words, prev_by, access.current_user_id(), change_id),
        )
        row = cur.fetchone()
        flag = "" if verified else " UNVERIFIED"
        again = f" [re-recorded; earlier unverified quote: \"{prev_words}\"]" if redo else ""
        record_activity(cur, row["project_id"], actor, "decided", "change", change_id,
                        f"Change #{change_id} {decision} (owner, in chat,{flag or ' verified'}){again}: {row['title']}")
        for tid in comment_tasks or ():
            cur.execute("SELECT id FROM tasks WHERE id = %s", (tid,))
            if cur.fetchone():
                cur.execute("INSERT INTO task_comments (task_id, author, body) VALUES (%s, %s, %s)",
                            (tid, actor, f"Owner {decision} change #{change_id} in the session"
                                         f"{'' if verified else ' (unverified, awaiting the owner)'}: {words}"))
        if verified:
            _event(cur, row, decision)
    row["pushed"] = _push_decision(row, actor)
    return row


def _push_decision(row: dict, actor: str) -> bool:
    """One push per session decision (the owner must see every one). Never raises."""
    try:
        from app import config, push
        project = repo.get_project(row["project_id"])
        payload = {"title": "Decided in chat",
                   "body": f"{actor} recorded: {row['decision']} change #{row['id']}"
                           f"{'' if row['words_verified'] else ' (unverified)'}: {row['title']}",
                   "url": f"{config.PUBLIC_URL}/{project['slug']}/changes", "tag": f"mdd-chat-decision-{row['id']}"}
        return push.deliver(payload).get("sent", 0) > 0
    except Exception:  # a push problem must never fail the decision
        return False


def unacknowledged(project_id: int | None = None, project_ids: list[int] | None = None) -> list[dict]:
    """Decisions sessions recorded in chat that the owner hasn't acknowledged or overridden (Now > Decided in chat)."""
    ids = list(project_ids) if project_ids is not None else [project_id] if project_id is not None else None
    rows = list_for(ids, where="c.decided_via = 'session' AND c.acknowledged_at IS NULL AND c.stage <> 'recommended'")
    return sorted(rows, key=lambda r: r["decided_at"])


def _stream_for(project: dict) -> str:
    """The outbox stream a change event goes on: 'features' (an 'all' session receives every stream)."""
    return "features"


def mark_done(change_id: int, actor: str, links=None, verified: bool | None = None) -> dict:
    """The change was made: link the tasks / commits / deploys. Only after an approved or changed decision."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM change_log WHERE id = %s FOR UPDATE", (change_id,))
        row = cur.fetchone()
        if not row:
            raise NotFound(f"change {change_id} not found")
        if row["stage"] == "recommended":
            raise Conflict(f"change {change_id} hasn't been decided yet: wait for the owner's decision")
        if row["decision"] == "rejected":
            raise Conflict(f"change {change_id} was rejected; it can't be marked done")
        if row.get("decided_via") == "session" and not row.get("words_verified", True) \
                and row.get("acknowledged_at") is None:
            raise Conflict(f"change {change_id}: the decision recorded in chat is unverified; "
                           f"wait for the owner to confirm it on the dashboard")
        merged = _merge_links(row["links"] or {}, _clean_links(links))
        ver = row["verified"] if verified is None else bool(verified)
        cur.execute(
            "UPDATE change_log SET stage = 'done', links = %s, verified = %s, done_at = COALESCE(done_at, now()), "
            "updated_at = now() WHERE id = %s RETURNING *",
            (Jsonb(merged), ver, change_id),
        )
        row = cur.fetchone()
        record_activity(cur, row["project_id"], actor, "done", "change", change_id,
                        f"Change #{change_id} done{' and verified' if ver else ''}: {row['title']}")
    return row


def list_for(project_ids: list[int] | None, stage: str | None = None, where: str = "") -> list[dict]:
    """Changes for the given projects (None = every active project), newest first within a stage."""
    if stage is not None and stage not in STAGES:
        raise BadRequest(f"stage must be one of {STAGES}")
    sql = ("SELECT c.*, p.slug AS project_slug, p.name AS project_name FROM change_log c "
           "JOIN projects p ON p.id = c.project_id AND p.archived_at IS NULL WHERE TRUE")
    params: list = []
    if project_ids is None:
        project_ids = access.ambient_ids()
    if project_ids is not None:
        sql += " AND c.project_id = ANY(%s)"
        params.append(list(project_ids))
    if stage:
        sql += " AND c.stage = %s"
        params.append(stage)
    if where:  # a fixed SQL fragment from this module, never user text
        sql += f" AND ({where})"
    sql += (" ORDER BY CASE c.stage WHEN 'recommended' THEN 0 WHEN 'decided' THEN 1 ELSE 2 END, "
            "COALESCE(c.done_at, c.decided_at, c.created_at) DESC, c.id DESC")
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def waiting(project_id: int | None = None, project_ids: list[int] | None = None) -> list[dict]:
    """Recommended changes waiting for the owner's decision (one project, several: an account's rollup, or every
    active one), oldest first."""
    ids = list(project_ids) if project_ids is not None else [project_id] if project_id is not None else None
    rows = list_for(ids, stage="recommended")
    return sorted(rows, key=lambda r: r["created_at"])


def grouped(project_id: int | None, project_ids: list[int] | None = None) -> dict:
    """Waiting for your decision / Decided, not done / Done (a rejected change is closed: it shows under Done). One
    project, or several (an account and every project under it, task #148)."""
    rows = list_for(list(project_ids) if project_ids is not None else [project_id])
    return {"waiting": [r for r in rows if r["stage"] == "recommended"],
            "decided": [r for r in rows if r["stage"] == "decided" and r["decision"] != "rejected"],
            "done": [r for r in rows if r["stage"] == "done" or r["decision"] == "rejected"]}


def now() -> datetime:
    return datetime.now(timezone.utc)
