"""What a project keeps that must survive a rebuild (#726): its declared STATEFUL PARTS, stored per project and, where a stage differs,
per deploy target (db/migrations/0098). Names and locations only: a recipe never holds a secret VALUE, and anything that looks like one
is refused. Crewboss records and publishes this; it does not run or monitor backups (that belongs to the ops tool that reads the manifest).

Shape (every list is capped, every string is checked):
  databases       [{engine, name, container, db_user}]  STRUCTURED only: Crewboss generates the dump and restore commands from a fixed
                                                  template per engine (app/manifest.py). There is no free-text command field.
  volumes         [{name, path}]                      named volumes or data folders
  upload_folders  [{path}]                            files users upload
  config_files    [{name, path}]                      config files and where they live
  secrets         [{name, location}]                  the NAME of a secret and where it is kept: a path, "env var NAME", "secret file NAME" or
                                                  "env file /path"; never its value
  ports, domains, health_url, log_endpoint, rpo_minutes, rto_minutes, notes
  stateless       true = the project says it keeps nothing worth backing up (clears the release check)"""
from __future__ import annotations

import json
import re

from app import access, repo
from app.db import get_cursor
from app.repo import BadRequest, Forbidden, NotFound

ENGINES = ("postgres", "mysql", "mariadb", "sqlite", "mongodb", "redis", "none")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,62}$")             # a database or user name: never a shell word
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
MAX_ITEMS = 20
LISTS = ("databases", "volumes", "upload_folders", "config_files", "secrets")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,79}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{0,199}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[^\s?#@]*)?$")
LOCATION_RE = re.compile(r"^(env var [A-Za-z_][A-Za-z0-9_]{0,63}|secret file [A-Za-z0-9][A-Za-z0-9._-]{0,79}|(env file )?/[A-Za-z0-9._/-]{1,199})$")
NOTE_MAX = 300
RPO_MAX = RTO_MAX = 60 * 24 * 365
_SECRET_SHAPES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+\S{8,}"),
    re.compile(r"(?i)\b[A-Za-z0-9_.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|credential|auth)[A-Za-z0-9_.-]*\s*[=:]\s*\S+"),
    re.compile(r"(?<![\w-])(?:--password\b|-W\b|-p\S{2,})"),                  # mysql -pXXXX, --password, psql -W
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),                                       # user:password@host
    re.compile(r"\b(?:cb[a-z]?|mdj|mdd|mdt|sk|ghp|gho|ghs|xox[a-z]|AKIA|AIza)[_-]?[A-Za-z0-9_-]{16,}"),
)


class RecipeError(BadRequest):
    pass


def looks_like_secret(text: str) -> bool:
    """Best effort: a key block, a bearer token, name=value on a secret-ish name, user:pass@host, a known token shape, or a long opaque
    string with both letters and digits. Names and paths pass; a pasted value does not."""
    text = str(text or "")
    if any(p.search(text) for p in _SECRET_SHAPES):
        return True
    for chunk in re.split(r"[\s/=,:|\"']+", text):
        if len(chunk) >= 28 and re.fullmatch(r"[A-Za-z0-9+_-]+={0,2}", chunk) and re.search(r"\d", chunk) and re.search(r"[A-Za-z]", chunk):
            return True
    return False


def _text(value, what: str, limit: int = 200) -> str:
    v = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).split())
    if len(v) > limit:
        raise RecipeError(f"{what} is too long (at most {limit} characters)")
    if looks_like_secret(v):
        raise RecipeError(f"{what} looks like a secret value. Record only the NAME of a secret and where it is kept, never the value")
    return v


def _path(value, what: str, required: bool = True) -> str:
    v = str(value or "").strip()
    if not v and not required:
        return ""
    if not PATH_RE.match(v) or ".." in v.split("/"):
        raise RecipeError(f"{what} must be an absolute folder or file path such as /srv/app/data")
    return _text(v, what)


def _items(raw, what: str) -> list:
    raw = raw or []
    if not isinstance(raw, list) or not all(isinstance(i, dict) for i in raw):
        raise RecipeError(f"{what} must be a list of entries")
    if len(raw) > MAX_ITEMS:
        raise RecipeError(f"{what}: at most {MAX_ITEMS} entries")
    return raw


def _name(value, what: str) -> str:
    v = str(value or "").strip()
    if not NAME_RE.match(v):
        raise RecipeError(f"{what} must be a plain name (letters, digits, . _ -)")
    return v


def _int(value, what: str, top: int) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise RecipeError(f"{what} must be a whole number of minutes") from None
    if not 1 <= n <= top:
        raise RecipeError(f"{what} must be between 1 and {top} minutes")
    return n


def _url(value, what: str) -> str:
    v = str(value or "").strip()
    if not v:
        return ""
    if v.startswith("/"):
        return _path(v, what)
    if not URL_RE.match(v) or len(v) > 200:
        raise RecipeError(f"{what} must be an http(s) address without a login in it, or a path")
    return _text(v, what)


def _list_of(raw, what: str) -> list:
    raw = raw or []
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[\s,]+", raw) if p]
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise RecipeError(f"{what}: a short list")
    return raw


def clean(raw: dict) -> dict:
    """Validate and normalise a recipe. Raises RecipeError (a BadRequest) naming the first problem."""
    if not isinstance(raw, dict):
        raise RecipeError("a recipe is a set of fields")
    out: dict = {"stateless": bool(raw.get("stateless"))}
    dbs = []
    for d in _items(raw.get("databases"), "databases"):
        engine = str(d.get("engine") or "").strip().lower()
        if engine not in ENGINES:
            raise RecipeError(f"database engine: one of {', '.join(ENGINES)}")
        name = str(d.get("name") or "").strip()
        user = str(d.get("db_user") or "").strip()
        container = str(d.get("container") or "").strip()
        if not IDENT_RE.match(name):
            raise RecipeError("a database name is letters, digits, _ and - only")
        if user and not IDENT_RE.match(user):
            raise RecipeError("a database user is letters, digits, _ and - only")
        if container and not CONTAINER_RE.match(container):
            raise RecipeError("a container name is letters, digits, . _ and - only")
        dbs.append({"engine": engine, "name": name, "container": container, "db_user": user})
    out["databases"] = dbs
    out["volumes"] = [{"name": _name(v.get("name"), "volume name"), "path": _path(v.get("path"), "volume path", required=False)}
                      for v in _items(raw.get("volumes"), "volumes")]
    out["upload_folders"] = [{"path": _path(u.get("path"), "upload folder")} for u in _items(raw.get("upload_folders"), "upload folders")]
    out["config_files"] = [{"name": _name(c.get("name"), "config file name"), "path": _path(c.get("path"), "config file path")}
                           for c in _items(raw.get("config_files"), "config files")]
    secrets = []
    for s in _items(raw.get("secrets"), "secrets"):
        name = str(s.get("name") or "").strip()
        if not SECRET_NAME_RE.match(name) or looks_like_secret(name):
            raise RecipeError("a secret is recorded by NAME (such as DATABASE_URL), never by value")
        loc = str(s.get("location") or "").strip()
        if not LOCATION_RE.match(loc):
            raise RecipeError(f"where {name} is kept: a short place such as 'env file /srv/app/.env' or 'vault path app/prod'")
        secrets.append({"name": name, "location": _text(loc, "secret location")})
    out["secrets"] = secrets
    ports = []
    for p in _list_of(raw.get("ports"), "ports"):
        try:
            n = int(p)
        except (TypeError, ValueError):
            raise RecipeError("ports are numbers between 1 and 65535") from None
        if not 1 <= n <= 65535:
            raise RecipeError("ports are numbers between 1 and 65535")
        ports.append(n)
    out["ports"] = sorted(set(ports))
    domains = _list_of(raw.get("domains"), "domains")
    for d in domains:
        if not DOMAIN_RE.match(str(d).strip().lower()):
            raise RecipeError(f"{str(d)[:60]!r} is not a host name such as app.example.com")
    out["domains"] = sorted({str(d).strip().lower() for d in domains})
    out["health_url"] = _url(raw.get("health_url"), "health endpoint")
    out["log_endpoint"] = _url(raw.get("log_endpoint"), "log endpoint")
    out["rpo_minutes"] = _int(raw.get("rpo_minutes"), "RPO", RPO_MAX)
    out["rto_minutes"] = _int(raw.get("rto_minutes"), "RTO", RTO_MAX)
    out["notes"] = _text(raw.get("notes"), "notes", NOTE_MAX)
    return out


def parts(recipe: dict | None) -> int:
    """How many stateful parts the recipe declares (databases, volumes, uploads, config files, secrets)."""
    return sum(len((recipe or {}).get(k) or []) for k in LISTS)


def declared(recipe: dict | None) -> bool:
    """True when the project has said something about its state: it lists parts, or says it has none."""
    return bool(recipe) and (parts(recipe) > 0 or bool(recipe.get("stateless")))


def from_form(form) -> dict:
    """The settings form's text boxes (one entry per line, `a | b | rest`) -> the recipe dict that clean() checks."""
    def lines(key):
        return [l.strip() for l in str(form.get(key) or "").splitlines() if l.strip()]

    def split(line, n):
        bits = [b.strip() for b in line.split("|", n - 1)]
        return bits + [""] * (n - len(bits))

    dbs = []
    for l in lines("databases"):
        e, n, c, u = split(l, 4)
        dbs.append({"engine": e, "name": n, "container": c, "db_user": u})
    return {"stateless": str(form.get("stateless") or "") in ("1", "on", "true"), "databases": dbs,
            "volumes": [dict(zip(("name", "path"), split(l, 2))) for l in lines("volumes")],
            "upload_folders": [{"path": l} for l in lines("upload_folders")],
            "config_files": [dict(zip(("name", "path"), split(l, 2))) for l in lines("config_files")],
            "secrets": [dict(zip(("name", "location"), split(l, 2))) for l in lines("secrets")],
            "ports": form.get("ports") or "", "domains": form.get("domains") or "", "health_url": form.get("health_url") or "",
            "log_endpoint": form.get("log_endpoint") or "", "rpo_minutes": form.get("rpo_minutes") or "",
            "rto_minutes": form.get("rto_minutes") or "", "notes": form.get("notes") or ""}


def to_form(recipe: dict | None) -> dict:
    """The recipe as the form's text boxes (the inverse of from_form)."""
    r = recipe or {}
    return {"databases": "\n".join(f"{d['engine']} | {d['name']} | {d.get('container', '')} | {d.get('db_user', '')}" for d in r.get("databases") or []),
            "volumes": "\n".join(f"{v['name']} | {v['path']}" for v in r.get("volumes") or []),
            "upload_folders": "\n".join(u["path"] for u in r.get("upload_folders") or []),
            "config_files": "\n".join(f"{c['name']} | {c['path']}" for c in r.get("config_files") or []),
            "secrets": "\n".join(f"{s['name']} | {s['location']}" for s in r.get("secrets") or []),
            "ports": ", ".join(str(p) for p in r.get("ports") or []), "domains": ", ".join(r.get("domains") or []),
            "health_url": r.get("health_url") or "", "log_endpoint": r.get("log_endpoint") or "",
            "rpo_minutes": r.get("rpo_minutes") or "", "rto_minutes": r.get("rto_minutes") or "", "notes": r.get("notes") or "",
            "stateless": bool(r.get("stateless"))}


# --- storage ------------------------------------------------------------------------------------------------------

def get(project_id: int, target_id: int | None = None) -> dict | None:
    """The recipe stored for exactly this scope (the project default when target_id is None), or None."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT recipe FROM state_recipes WHERE project_id = %s AND COALESCE(target_id, 0) = %s", (project_id, target_id or 0))
        row = cur.fetchone()
    return row["recipe"] if row else None


def effective(project_id: int, target_id: int | None) -> dict | None:
    """The target's own recipe where it differs, else the project's."""
    return (get(project_id, target_id) if target_id else None) or get(project_id)


def all_for(project_id: int) -> dict:
    """{target_id or None: recipe} for the project."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT target_id, recipe FROM state_recipes WHERE project_id = %s", (project_id,))
        return {r["target_id"]: r["recipe"] for r in cur.fetchall()}


def _actor(who) -> str:
    return f"user:{who.user_id}" if who.user_id else "owner"


def can_manage(who, project: dict) -> bool:
    """Who writes a recipe: the same rule as deploy targets (the project's Owner or the instance owner). A recipe becomes a server skill an ops
    tool acts on with server rights, so a Member or Admin only reads it."""
    from app import deploys
    return deploys.can_manage_targets(who, project)


def require_manage(who, project: dict) -> None:
    if not can_manage(who, project):
        raise Forbidden("only the project's Owner changes what a server keeps and backs up: it becomes instructions for that server")


def save(who, project: dict, raw: dict, target_id: int | None = None) -> dict:
    """Validate and store a recipe. Project Owner or instance owner only (can_manage); a target must be the project's own."""
    require_manage(who, project)
    recipe = clean(raw)
    with get_cursor() as cur:
        if target_id is not None:
            cur.execute("SELECT 1 FROM deploy_targets WHERE id = %s AND project_id = %s", (target_id, project["id"]))
            if not cur.fetchone():
                raise NotFound("no such deploy target")
        cur.execute("INSERT INTO state_recipes (project_id, target_id, recipe, updated_by) VALUES (%s, %s, %s::jsonb, %s) "
                    "ON CONFLICT (project_id, COALESCE(target_id, 0)) DO UPDATE SET recipe = EXCLUDED.recipe, "
                    "updated_by = EXCLUDED.updated_by, updated_at = now()",
                    (project["id"], target_id, json.dumps(recipe), _actor(who)))
        repo.record_activity(cur, project["id"], _actor(who), "set", "state_recipe", target_id or project["id"],
                             "Updated what this project keeps (backup recipe)" + (f" for target {target_id}" if target_id else ""))
    return recipe


def remove_override(who, project: dict, target_id: int) -> None:
    require_manage(who, project)
    with get_cursor() as cur:
        cur.execute("DELETE FROM state_recipes WHERE project_id = %s AND target_id = %s", (project["id"], target_id))
