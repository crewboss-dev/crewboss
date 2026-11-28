"""Owner-reviewed pins for a project's agent, command and skill files (#829).

A job's runner hashes every file Claude Code would load from the repository (`.claude/{agents,commands,skills}/**` at any depth and
`.crewboss/skills/**`) and starts the job only if each (repo path, sha256) is PINNED here. NO security decision rests on parsing: the
Owner reads the bytes and pins them; `declared()` only DISPLAYS what a file says it asks for (tools, hooks, MCP servers, permission mode).

Flow: a runner finds an unpinned file -> sends its bytes in an `agent_file` frame (verified socket, fence-checked) -> a `pending` row ->
the Owner reads the file on the Machines page and pins it with a fresh factor (`pin`) -> the next claim carries the pin (`pins_for`) -> the
runner lets the job start. A changed file has a new hash, so it is unpinned again. The table's CHECK ties a hash to the stored bytes.
Files created or changed DURING a job (source 'job') flag the job (`jobs.review_paths`) and arrive here the same way."""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re

import yaml

from app import access, jobs, repo
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound
from app.skill_md import FRONT_RE

log = logging.getLogger("crewboss.agent_pins")

MAX_BYTES = 96 * 1024          # the same cap as the runner's MAX_REVIEW_BYTES: a larger file cannot be shown, so it cannot be pinned
MAX_PENDING = 200              # per project; beyond this new files are not recorded (the runner's job is blocked regardless)
PENDING_DAYS = 30
MAX_REVIEW_PATHS = 50
SOURCES = ("preflight", "job")
PATH_RE = re.compile(r"^(?:(?:.+/)?\.claude/(?:(?:agents|commands|skills)/.+|settings(?:\.local)?\.json)|\.crewboss/skills/.+)$", re.I)
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def valid_path(path) -> bool:
    return (isinstance(path, str) and 0 < len(path) <= 300 and path.isprintable() and not path.startswith("/")
            and ".." not in path.split("/") and "\\" not in path and bool(PATH_RE.match(path)))


def pins_for(cur, project_id: int) -> list[dict]:
    """What a claim carries to the runner: [{path, sha256}] of the project's pinned files."""
    cur.execute("SELECT path, sha256 FROM project_agent_files WHERE project_id = %s AND state = 'pinned' ORDER BY path, sha256", (project_id,))
    return [{"path": r["path"], "sha256": r["sha256"]} for r in cur.fetchall()]


def report(job: dict, project_id: int, path, sha256, b64, source) -> dict:
    """An `agent_file` frame from a runner holding `job` (already fence-checked). With bytes: stored as a pending file for the Owner (when not
    already pinned); a 'job' source also flags the job. Without bytes (too big, not text, or a link): only a 'job' source flags the job."""
    if source not in SOURCES or not valid_path(path):
        raise BadRequest("bad agent file report")
    data = None
    if b64 or sha256 == EMPTY_SHA:   # an EMPTY file carries no b64 (an older runner sends none at all): its hash alone says it is empty
        try:
            data = base64.b64decode(str(b64 or ""), validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest("bad agent file bytes") from None
        if len(data) > MAX_BYTES or b"\x00" in data or not isinstance(sha256, str) or hashlib.sha256(data).hexdigest() != sha256:
            raise BadRequest("agent file bytes do not match their hash or are not reviewable")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise BadRequest("agent file is not text") from None
    with get_cursor() as cur:
        if source == "job":
            cur.execute("UPDATE jobs SET review_paths = (SELECT COALESCE(array_agg(p), '{}') FROM (SELECT DISTINCT p FROM unnest(review_paths || %s::text[]) p "
                        "LIMIT %s) q) WHERE id = %s", ([path], MAX_REVIEW_PATHS, job["id"]))
        if data is not None:
            cur.execute("DELETE FROM project_agent_files WHERE state = 'pending' AND seen_at < now() - make_interval(days => %s)", (PENDING_DAYS,))
            cur.execute("SELECT count(*) AS n FROM project_agent_files WHERE project_id = %s AND state = 'pending'", (project_id,))
            if cur.fetchone()["n"] >= MAX_PENDING:
                log.warning("agent pins: project %s has %s pending files; not recording %s", project_id, MAX_PENDING, path)
            else:
                cur.execute("INSERT INTO project_agent_files (project_id, path, sha256, content, source, seen_job_id) VALUES (%s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT (project_id, path, sha256) DO UPDATE SET seen_at = now(), seen_job_id = EXCLUDED.seen_job_id "
                            "WHERE project_agent_files.state = 'pending'", (project_id, path, sha256, data, source, job["id"]))
    return {}


# --- what the page shows ------------------------------------------------------------------------------------------------------
MAX_DEPTH = 32                 # nesting (flow or block) deeper than this is not shown
SHOW_BYTES = 8 * 1024          # only the first 8 KB of a frontmatter is read for display


def _no_aliases(text: str) -> bool:
    """True when the YAML text has no anchor (&a) and no alias (*a). Aliases are what make a tiny file expand to billions of values when it is
    loaded and printed (the "billion laughs" bomb), so a file that uses them is not parsed for display at all. Reads EVENTS only: nothing is
    constructed, so nothing can expand."""
    depth = 0
    for ev in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None):
            return False
        if isinstance(ev, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
            depth += 1
            if depth > MAX_DEPTH:      # stop at once: the parser is lazy, so a 98 KB "[[[[..." costs a few dozen events, not a minute
                raise RecursionError("frontmatter nested too deeply")
        elif isinstance(ev, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
            depth -= 1
    return True


def _s(v, n: int = 80) -> str:
    """A scalar as short display text; a nested value (list, mapping) is never stringified (str() of a big structure is the other bomb)."""
    return str(v)[:n] if isinstance(v, (str, int, float, bool)) or v is None else "(nested)"


def declared(content: bytes, path: str = "") -> dict:
    """DISPLAY ONLY: what a file's frontmatter says it declares, read with a real YAML parser (never approves anything; the Owner reads the
    text). -> {name, tools, hooks, mcp_servers, permission_mode, note}; the lists are plain short strings, capped. A frontmatter that uses YAML
    anchors or aliases, is nested absurdly deep, or does not parse is NOT shown ("read the raw text"); only scalars are ever stringified."""
    text = content.decode("utf-8", "replace")
    out = {"name": "", "tools": [], "hooks": [], "mcp_servers": [], "permission_mode": "", "note": ""}
    if any(ch in text[:4000] for ch in ("\x85", "\u2028", "\u2029")):
        out["note"] = "has unusual line breaks: read the raw text"
    if path.lower().endswith(".json"):       # a settings file is JSON, not frontmatter: show no defaults that look like a verdict
        out["note"] = "settings file: read the raw text"
        return out
    m = FRONT_RE.match(text)
    if not m:
        return out

    def cannot(why: str) -> dict:
        out["note"] = (out["note"] + "; " if out["note"] else "") + why + ": read the raw text"
        return out

    block = m.group(1)[:SHOW_BYTES]
    long = len(m.group(1)) > SHOW_BYTES
    try:
        if not _no_aliases(block):
            return cannot("frontmatter uses YAML anchors or aliases, so it is not shown")
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return cannot("frontmatter is longer than 8 KB" if long else "frontmatter is not valid YAML")
    except RecursionError:
        return cannot("frontmatter is nested too deeply")
    if long:
        cannot("frontmatter is longer than 8 KB, so only its start is shown")
    if not isinstance(data, dict):
        return out

    def key(*names):
        low = {_s(k).lower().replace("_", "").replace("-", ""): v for k, v in data.items()}
        for n in names:
            if n in low:
                return low[n]
        return None

    def names_of(v) -> list[str]:
        return [_s(k, 40) for k in list(v)[:10]] if isinstance(v, dict) else ["(declared)"]

    out["name"] = _s(key("name") or "")
    tools = key("tools", "allowedtools")
    if isinstance(tools, str):
        tools = [t.strip() for t in re.split(r"[,\n]", tools[:2000]) if t.strip()]
    out["tools"] = [_s(t) for t in tools[:20]] if isinstance(tools, list) else []
    if key("hooks"):
        out["hooks"] = names_of(key("hooks"))
    if key("mcpservers"):
        out["mcp_servers"] = names_of(key("mcpservers"))
    out["permission_mode"] = _s(key("permissionmode") or "", 40)
    return out


class _File(dict):
    """One file on the card. `declared` (the parse of its frontmatter) is computed the first time something asks for it, so a page that never
    shows a file never parses it; a pinned file carries no bytes at all (path and hash only)."""

    def __missing__(self, key):
        if key == "declared":
            self[key] = declared(self["text"].encode("utf-8", "replace"), self.get("path") or "") if self.get("text") is not None else None
            return self[key]
        raise KeyError(key)


def card(who: "access.Principal", account_id: int) -> list[dict]:
    """The review cards for one account: per project the viewer owns that has unpinned files, plus its pinned files.
    -> [{project_id, slug, name, pending: [file], pinned: [file]}]; file = {id, path, sha256, text, size, source, declared, seen_at}.
    Cost: the viewer's role is checked per PROJECT first, so nothing of a project they do not own is loaded; only PENDING files carry their bytes
    (the Owner reads them); a pinned file is listed by path and hash only (text None, declared None) and nothing is parsed until asked for."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT p.id, p.slug, p.name FROM projects p WHERE (p.partition_id = %s OR p.id = %s) "
                    "AND EXISTS (SELECT 1 FROM project_agent_files f WHERE f.project_id = p.id) ORDER BY p.slug", (account_id, account_id))
        owned = [p for p in cur.fetchall() if access.role_in(who, p["id"]) == access.PROJECT_OWNER]
        if not owned:
            return []
        cur.execute("SELECT f.id, f.project_id, f.path, f.sha256, f.state, f.source, f.seen_at, f.pinned_at, f.seen_job_id, "
                    "octet_length(f.content) AS size, CASE WHEN f.state = 'pending' THEN f.content END AS content "
                    "FROM project_agent_files f WHERE f.project_id = ANY(%s) ORDER BY f.path, f.seen_at DESC", ([p["id"] for p in owned],))
        rows = cur.fetchall()
    out = {p["id"]: {"project_id": p["id"], "slug": p["slug"], "name": p["name"], "pending": [], "pinned": []} for p in owned}
    for r in rows:
        pending = r["state"] == "pending"
        f = _File(id=r["id"], path=r["path"], sha256=r["sha256"], short=r["sha256"][:12],
                  text=bytes(r["content"]).decode("utf-8", "replace") if pending and r["content"] is not None else None,
                  size=r["size"], source=r["source"], seen_at=r["seen_at"], pinned_at=r["pinned_at"], job_id=r["seen_job_id"])
        out[r["project_id"]]["pending" if pending else "pinned"].append(f)
    return [c for c in out.values() if c["pending"] or c["pinned"]]


# --- the Owner's actions ------------------------------------------------------------------------------------------------------
def pin(who: "access.Principal", user_id: int | None, ids: list[int], account_id: int) -> int:
    """Pin the listed pending files (the ones the Owner was shown). Each must belong to this account and to a project `who` owns. -> how many."""
    if not ids:
        raise BadRequest("nothing to pin")
    n = 0
    touched: set[int] = set()
    with get_cursor() as cur:
        for fid in sorted(set(ids))[:MAX_PENDING]:
            cur.execute("SELECT f.*, p.partition_id FROM project_agent_files f JOIN projects p ON p.id = f.project_id WHERE f.id = %s FOR UPDATE", (fid,))
            r = cur.fetchone()
            if not r or account_id not in (r["partition_id"], r["project_id"]) or access.role_in(who, r["project_id"]) != access.PROJECT_OWNER:
                raise NotFound("not found")
            if r["state"] != "pending":
                continue
            cur.execute("UPDATE project_agent_files SET state = 'pinned', pinned_by_user_id = %s, pinned_at = now() WHERE id = %s", (user_id, fid))
            cur.execute("DELETE FROM project_agent_files WHERE project_id = %s AND path = %s AND state = 'pending' AND id < %s",
                        (r["project_id"], r["path"], fid))   # the OLDER unreviewed versions of this path are superseded; a newer one stays pending
            repo.record_activity(cur, account_id, f"user:{user_id}", "pinned", "agent_file", fid,
                                 f"Pinned {r['path']} ({r['sha256'][:12]}) for project {r['project_id']}: the Owner read it; jobs may load it")
            n += 1
            touched.add(r["project_id"])
        freed: list[int] = []
        for pid in sorted(touched):   # #987: a job held for unreviewed files may run once its project has none left
            freed += jobs.release_holds(cur, pid)
    if freed:
        jobs.nudge(freed)
    return n


def unpin(who: "access.Principal", user_id: int | None, fid: int, account_id: int) -> None:
    with get_cursor() as cur:
        cur.execute("SELECT f.*, p.partition_id FROM project_agent_files f JOIN projects p ON p.id = f.project_id WHERE f.id = %s FOR UPDATE", (fid,))
        r = cur.fetchone()
        if not r or account_id not in (r["partition_id"], r["project_id"]) or access.role_in(who, r["project_id"]) != access.PROJECT_OWNER:
            raise NotFound("not found")
        if r["state"] != "pinned":
            raise Forbidden("that file is not pinned")
        cur.execute("DELETE FROM project_agent_files WHERE id = %s", (fid,))
        repo.record_activity(cur, account_id, f"user:{user_id}", "unpinned", "agent_file", fid,
                             f"Unpinned {r['path']} ({r['sha256'][:12]}) of project {r['project_id']}: jobs that carry it are blocked again")


def pending_count(project_id: int) -> int:
    """How many agent / command / skill files of this project wait for the Owner's review (#987: the 'waiting for you' banner)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM project_agent_files WHERE project_id = %s AND state = 'pending'", (project_id,))
        return cur.fetchone()["n"]


def pending_in_account(account_id: int) -> int:
    """Pending files across the account's projects (the project Machines page's link adds #agent-files when this is non-zero)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM project_agent_files f JOIN projects p ON p.id = f.project_id "
                    "WHERE f.state = 'pending' AND (p.partition_id = %s OR p.id = %s)", (account_id, account_id))
        return cur.fetchone()["n"]


_EXEC_NAME = re.compile(r"(^|/)(settings(\.local)?\.json|\.mcp\.json)$", re.I)
_fallback_key = None


def _sign(payload: str) -> str:
    """A server-signed tag (HMAC-SHA256 under the instance's sign-in key) so a form can only carry back a snapshot this server rendered."""
    global _fallback_key
    import hashlib
    import hmac
    from app import config, local_auth
    try:
        key = local_auth._key_bytes(config.LOCAL_AUTH_KEY_FILE)
    except Exception:  # noqa: BLE001 - no key file (e.g. a read-only instance): a per-process key still binds the form to this server
        import os
        if _fallback_key is None:
            log.warning("agent pins: the sign-in key file can't be read, so 'Trust these files' forms are signed with a per-process key; with more than "
                        "one worker every submit will say 'The list changed'. Fix CREWBOSS_LOCAL_AUTH_KEY_FILE.")
        _fallback_key = _fallback_key or os.urandom(32)
        key = _fallback_key
    return hmac.new(key, b"crewboss-trust-agent-files-v1|" + payload.encode(), hashlib.sha256).hexdigest()


def _snapshot(project_id: int) -> list[tuple[int, str, str, bytes]]:
    """The NEWEST pending version of each path of this project: [(id, path, sha256, bytes)]. An older pending version of a path is never offered."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT DISTINCT ON (path) id, path, sha256, content FROM project_agent_files WHERE project_id = %s AND state = 'pending' "
                    "ORDER BY path, id DESC", (project_id,))
        return [(r["id"], r["path"], r["sha256"], bytes(r["content"] or b"")) for r in cur.fetchall()]


def _snap_text(project_id: int, user_id, rows) -> str:
    return f"{project_id}|{user_id}|" + ",".join(f"{i}:{h}" for i, _p, h, _c in sorted(rows))


_HOOK_KEY = re.compile(r"^[ \t]*[\"']?(hooks|mcpServers|mcp_servers|mcp-servers)[\"']?[ \t]*:", re.M | re.I)


_LEAD = "\ufeff \t\r\n\u00a0"
_HOOK_WORD = re.compile(r"hook|mcp", re.I)
_ESCAPED_KEY = re.compile(r"""^[ \t]*(?:"[^"\n]*\\[^"\n]*"|'[^'\n]*\\[^'\n]*')[ \t]*:""", re.M)
_COMPLEX_KEY = re.compile(r"^[ \t]*\?", re.M)
_TOP_KEY = re.compile(r"""^["']?([^\s#:?\-"'][^:\n]*?)["']?[ \t]*:""", re.M)


def _executes(path: str, content: bytes) -> bool:
    """The "can run things" LABEL (a highlight, never the security control: the owner can read every file's exact bytes). It fails closed: settings
    and .mcp.json files; anything declared() reports as hooks / MCP servers or could not fully read; a hook-key line anywhere in the file; a file
    that opens with --- and mentions hook or mcp anywhere (any case); and frontmatter that has an escaped quoted key, a `?` complex key, a duplicate
    top-level key, or a --- inside it. A file that opens with --- but is not read as frontmatter by FRONT_RE counts too."""
    if _EXEC_NAME.search(path):
        return True
    d = declared(content, path)
    if d["hooks"] or d["mcp_servers"] or d["note"]:
        return True
    text = content.decode("utf-8", "replace")
    if _HOOK_KEY.search(text):
        return True
    if not text.lstrip(_LEAD).startswith("---"):
        return False
    m = FRONT_RE.match(text)
    if not m or _HOOK_WORD.search(text):
        return True
    block = m.group(1)
    if "---" in block or _ESCAPED_KEY.search(block) or _COMPLEX_KEY.search(block):
        return True
    keys = [k.strip().lower() for k in _TOP_KEY.findall(block)]
    return len(keys) != len(set(keys))


def trust_block(who: "access.Principal", project_id: int) -> dict | None:
    """What the task window's NOW block needs to let the person deal with unreviewed agent files RIGHT THERE (0.1.2). None when this project has no
    pending file. -> {n, can_trust, groups: [(folder, [{name, text|None, flag, size, too_big}])], snap, sig}. can_trust = the project Owner only
    (the same rule as pin()). The owner gets EVERY file's full text (each in its own collapsible row, up to MAX_BYTES), so they can read exactly what
    the snapshot pins; `flag` ("can run things") only decides which rows open by default. Non-owners get paths. `snap` + `sig` pin the exact set the
    person is shown: trust_project() refuses when the pending set is not that set any more."""
    rows = _snapshot(project_id)
    if not rows:
        return None
    groups: dict[str, list[dict]] = {}
    can = access.role_in(who, project_id) == access.PROJECT_OWNER
    flagged = 0
    for _id, pth, _sha, content in rows:
        folder, _, name = pth.rpartition("/")
        entry = {"name": name, "text": None, "flag": False, "size": len(content), "too_big": False}
        if can:      # file text goes to the project owner only: the exact bytes the signed snapshot pins
            entry["flag"] = _executes(pth, content)
            flagged += entry["flag"]
            if len(content) > MAX_BYTES:
                entry["too_big"] = True
            else:
                entry["text"] = content.decode("utf-8", "replace")
        groups.setdefault(folder or "(top level)", []).append(entry)
    snap = _snap_text(project_id, who.user_id, rows)
    return {"n": len(rows), "can_trust": can, "groups": sorted(groups.items()), "snap": snap, "sig": _sign(snap), "executable": flagged}


class ListChanged(Conflict):
    """The files waiting for review are not the ones the person was shown."""


def trust_project(who: "access.Principal", user_id: int | None, project_id: int, account_id: int, snap: str = "", sig: str = "") -> int:
    """"Trust these files and run": pin EXACTLY the set the person was shown (the signed snapshot of the newest pending version of each path of THIS
    project), with pin()'s own authorization (project Owner only). Anything added or changed since the page rendered -> ListChanged, nothing pinned.
    -> how many were pinned. Held jobs are released by pin() itself."""
    import hmac
    if access.role_in(who, project_id) != access.PROJECT_OWNER:
        raise Forbidden("only the project owner can trust a project's agent files")
    rows = _snapshot(project_id)
    now = _snap_text(project_id, who.user_id, rows)
    if not snap or not sig or not hmac.compare_digest(sig, _sign(snap)) or snap != now:
        raise ListChanged("The list changed. Have a look again.")
    return pin(who, user_id, [r[0] for r in rows], account_id) if rows else 0
