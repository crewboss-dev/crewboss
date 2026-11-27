#!/usr/bin/env python3
"""Migration lint (task #589): a migration changes the SCHEMA only.

Rules, applied to every db/migrations/*.sql (comments and string contents are understood, so a word in a comment or a literal never
trips a rule):

  1. No INSERT, UPDATE, DELETE, COPY, TRUNCATE or MERGE, except an INSERT with literal VALUES into a table in SEED_TABLES.
  2. An INSERT into a seed table must be generic: no instance-owner name (the reference ratchet's word), no internal address (RFC1918 / CGNAT), no email
     address outside the reserved example domains, and no entry of the private denylist ($CREWBOSS_HOME/denylist.txt) when one exists.
     INSERT ... SELECT is refused too: it copies instance data, it does not seed.
  3. A DO block (or a WITH ... statement) must not write data: INSERT INTO, UPDATE ... SET, DELETE FROM, COPY, TRUNCATE, EXECUTE or
     PERFORM inside it is refused. DO blocks that only add a constraint or index guarded by an IF are fine.

  4. A .py migration is refused (app/db.py still applies old ones; a .py can run anything, so no new one is allowed).
  5. CALL, a function/procedure body that writes data, and a SELECT of a function created in the same file are refused.
  6. ON CONFLICT ... DO UPDATE into a seed table is refused (it overwrites rows an install edited): only DO NOTHING.

  7. ALLOWLIST: a statement must start with CREATE, ALTER, DROP, COMMENT, GRANT, REVOKE, SET, BEGIN/COMMIT, or be one of the handled
     cases above (a seed INSERT, a DO block, a plain SELECT). Anything else (PREPARE, EXECUTE, EXPLAIN, VACUUM, ...) is refused.
  8. Also refused: CREATE TABLE ... AS, SELECT ... INTO, CREATE RULE, and a top-level SELECT that calls any function that is not on the
     short list of pure built-ins (BUILTIN_FUNCS), whether or not this file created it.
  9. A seed INSERT must say ON CONFLICT ... DO NOTHING (it must be safe to re-run).
 10. DO blocks are read whether quoted with $$ or with single quotes. The instance-content checks (rule 2) also apply to a DEFAULT in
     CREATE and ALTER, and a DEFAULT may call only built-ins.

Instance data goes through the app or the private instance repo, never a migration.

Applied migrations are not rewritten. The ones that already break a rule are listed in scripts/migration_lint_grandfather.json with a
reason and the sha256 of the file as it stood: a grandfathered file that is edited, or that no longer breaks a rule, fails the lint
(remove the entry). New migrations are never added to that list.

    python3 scripts/migration_lint.py            # lint db/migrations, exit 1 on any finding
    python3 scripts/migration_lint.py FILE...    # lint just those files (still honours the grandfather list)
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "db" / "migrations"
GRANDFATHER = ROOT / "scripts" / "migration_lint_grandfather.json"

# Generic seed tables: rows every install needs and that name no instance. Adding a table here is a reviewed change (CODEOWNERS).
SEED_TABLES = {"agent_profiles", "global_plan_state"}

_spec = importlib.util.spec_from_file_location("commit_guard", ROOT / "scripts" / "commit_guard.py")
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)

OWNER_NAME = re.compile("mal" + "len", re.I)  # the word tests/test_no_*_refs.py ratchets
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
RESERVED_MAIL = re.compile(r"(?:^|\.)(?:example(?:\.(?:com|org|net))?|test|invalid|localhost)$", re.I)

FUNC_WRITES = re.compile(r"\bINSERT\s+INTO\b|\bUPDATE\s+(?:ONLY\s+)?[\w.\"]+(?:\s+(?:AS\s+)?\w+)?\s+SET\b|\bDELETE\s+FROM\b|"
                         r"\bTRUNCATE\b|\bCOPY\b", re.I)
DML_START = {"INSERT", "UPDATE", "DELETE", "COPY", "TRUNCATE", "MERGE"}
# First words a migration statement may start with (INSERT, DO, WITH and SELECT are handled by their own rules below).
ALLOWED_FIRST = {"CREATE", "ALTER", "DROP", "COMMENT", "GRANT", "REVOKE", "SET", "BEGIN", "COMMIT", "END", "START"}
# Pure built-ins a top-level SELECT or a DEFAULT may call. Anything else (a user function, setval, pg_read_file, dblink...) is refused.
BUILTIN_FUNCS = {
    "now", "current_timestamp", "current_date", "current_user", "clock_timestamp", "statement_timestamp", "transaction_timestamp",
    "count", "sum", "min", "max", "avg", "bool_and", "bool_or", "array_agg", "string_agg", "json_agg", "jsonb_agg",
    "coalesce", "nullif", "greatest", "least", "lower", "upper", "length", "char_length", "concat", "concat_ws", "substr",
    "substring", "trim", "ltrim", "rtrim", "replace", "left", "right", "md5", "abs", "round", "floor", "ceil", "ceiling", "mod",
    "to_char", "to_date", "to_timestamp", "to_number", "date_trunc", "extract", "age", "make_interval", "make_date",
    "jsonb_build_object", "json_build_object", "jsonb_build_array", "json_build_array", "to_jsonb", "to_json", "jsonb_typeof",
    "row_number", "gen_random_uuid", "version", "current_setting", "pg_typeof", "to_regclass", "generate_series", "unnest",
    "array_length", "cardinality", "interval", "timezone", "date_part", "regexp_replace", "split_part", "position",
}
# Words that sit before a ( without being a function call.
NOT_CALLS = {
    "in", "exists", "values", "any", "all", "some", "and", "or", "not", "array", "row", "select", "from", "where", "on", "as",
    "over", "filter", "case", "when", "then", "else", "using", "join", "lateral", "into", "by", "having", "limit", "union",
    "distinct", "is", "between", "like", "ilike", "similar", "with", "within", "group", "order", "partition", "end", "returning",
    "varchar", "char", "numeric", "decimal", "timestamp", "timestamptz", "time", "interval", "bit", "varbit", "character",
    "text", "int", "integer", "bigint", "smallint", "boolean", "uuid", "jsonb", "json", "inet", "cidr", "date", "float",
    "double", "real", "default", "check", "unique", "primary", "key", "references", "constraint", "only", "set", "if", "cross",
    "left", "right", "inner", "outer", "full", "natural", "except", "intersect", "offset", "fetch", "first", "next", "rows",
    "for", "update", "share", "nowait", "skip", "locked", "collate", "at", "to", "do", "nothing", "conflict", "not", "null",
    "varying", "cast",
}
# Functions a DEFAULT (and only a DEFAULT) may call besides BUILTIN_FUNCS: sequences, random ids, timestamps. Not allowed as a top-level SELECT.
DEFAULT_FUNCS = {"nextval", "gen_random_uuid", "uuid_generate_v4", "random", "encode", "gen_random_bytes", "now", "clock_timestamp"}
_IDENT = r'(?:"[^"\n]*"|[A-Za-z_]\w*)'
_CALL = re.compile(r'(?<![\w$."])(' + _IDENT + r'(?:\s*\.\s*' + _IDENT + r')*)\s*\(')
WRITES = re.compile(r"\bINSERT\s+INTO\b|\bUPDATE\s+(?:ONLY\s+)?[\w.\"]+(?:\s+(?:AS\s+)?\w+)?\s+SET\b|\bDELETE\s+FROM\b|\bCOPY\b|"
                    r"\bTRUNCATE\b|\bEXECUTE\b|\bPERFORM\b|\bMERGE\s+INTO\b", re.I)


def split_statements(sql: str) -> list[dict]:
    """Split SQL into statements. Each is {'code': text with comments dropped, string literals as '' and dollar-quoted bodies as $$$$,
    'raw': text with comments dropped but literals kept, 'bodies': [dollar-quoted bodies], 'strings': [literal contents]}."""
    stmts, code, raw, bodies, strings = [], [], [], [], []
    i, n = 0, len(sql)

    def flush():
        c = "".join(code).strip()
        if c:
            stmts.append({"code": c, "raw": "".join(raw).strip(), "bodies": bodies[:], "strings": strings[:]})
        code.clear(); raw.clear(); bodies.clear(); strings.clear()

    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i): depth += 1; i += 2
                elif sql.startswith("*/", i): depth -= 1; i += 2
                else: i += 1
        elif ch == '"':  # a quoted identifier: may hold ' or $ or --; "" is an escaped quote
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if sql.startswith('""', j): j += 2; continue
                    break
                j += 1
            ident = sql[i + 1:j]
            # keep the name when it is (or leads to) a call, so "setval"(...) and public."wipe"() are seen; else a placeholder
            nxt = sql[j + 1:j + 40].lstrip()
            keep = re.fullmatch(r"[A-Za-z_]\w*", ident) is not None and nxt[:1] in ("(", ".")
            code.append('"' + ident + '"' if keep else '"x"' if nxt[:1] not in ("(", ".") else '"?"')
            raw.append(sql[i:j + 1]); i = j + 1
        elif ch == "'":
            # E'...' (or e'...') is an escape string: backslash escapes the next character as well as '' doing so
            esc = i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] in "_$"))
            j = i + 1
            while j < n:
                if esc and sql[j] == "\\":
                    j += 2; continue
                if sql[j] == "'":
                    if sql.startswith("''", j): j += 2; continue
                    break
                j += 1
            lit = sql[i + 1:j]
            strings.append(lit.replace("''", "'"))
            if esc and code and code[-1] in "eE": code.pop()  # the E prefix belongs to the literal
            code.append("''"); raw.append(sql[i:j + 1]); i = j + 1
        elif ch == "$" and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            m = re.compile(r"\$(?:[A-Za-z_]\w*)?\$").match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, m.end())
                j = n if j < 0 else j
                bodies.append(sql[m.end():j])
                code.append("$$$$"); raw.append(sql[i:j + len(tag)]); i = j + len(tag)
            else:
                code.append(ch); raw.append(ch); i += 1
        elif ch == ";":
            flush(); i += 1
        else:
            code.append(ch); raw.append(ch); i += 1
    flush()
    return stmts


def _instance_hits(text: str, deny: list[str]) -> list[str]:
    hits = []
    if OWNER_NAME.search(text): hits.append("the instance name")
    if _guard.INTERNAL_IP.search(text): hits.append("an internal address")
    if any(not RESERVED_MAIL.search(m.group(1)) for m in EMAIL.finditer(text)): hits.append("an email address")
    low = text.lower()
    if any(d in low for d in deny): hits.append("a private denylist entry")
    return hits


def split_code(text: str) -> str:
    return " ".join(st["code"] for st in split_statements(text))


def _body_writes(body: str) -> list[str]:
    found = []
    for st in split_statements(body):
        found += [m.group(0).split()[0].upper() for m in WRITES.finditer(st["code"])]
    return found


def _unsafe_calls(code: str, extra: frozenset | set = frozenset()) -> list[str]:
    """Names of the functions `code` calls that are not on the pure built-in list. (`code` has literals blanked, so a word in a
    string never counts. A call to a cast type such as varchar(20) is not a call.)"""
    bad = []
    for m in _CALL.finditer(code):
        name = re.sub(r"\s+", "", m.group(1)).replace('"', "").lower()   # "setval"( and public."wipe"( are calls too
        ok = BUILTIN_FUNCS | extra
        if name in NOT_CALLS or name in ok:
            continue
        if name.startswith("pg_catalog.") and name[11:] in ok:
            continue
        bad.append(name)
    return bad


def _skip_literal(raw: str, i: int) -> int | None:
    """If a string literal, quoted identifier or dollar-quoted body starts at raw[i], the index just past it; else None."""
    n, ch = len(raw), raw[i]
    if ch == "'":
        esc = i > 0 and raw[i - 1] in "eE" and (i < 2 or not (raw[i - 2].isalnum() or raw[i - 2] in "_$"))
        j = i + 1
        while j < n:
            if esc and raw[j] == "\\":
                j += 2; continue
            if raw[j] == "'":
                if raw.startswith("''", j): j += 2; continue
                break
            j += 1
        return j + 1
    if ch == '"':
        j = i + 1
        while j < n:
            if raw[j] == '"':
                if raw.startswith('""', j): j += 2; continue
                break
            j += 1
        return j + 1
    if ch == "$" and not (i > 0 and (raw[i - 1].isalnum() or raw[i - 1] == "_")):
        m = re.compile(r"\$(?:[A-Za-z_]\w*)?\$").match(raw, i)
        if m:
            j = raw.find(m.group(0), m.end())
            return n if j < 0 else j + len(m.group(0))
    return None


def _default_exprs(raw: str) -> list[str]:
    """The text of every DEFAULT expression in a CREATE / ALTER statement (literals kept): from after DEFAULT to the next comma or
    closing bracket at the same depth, or the next clause keyword. The statement is scanned token by token, so the word DEFAULT inside
    a string literal, a quoted identifier or a function body ($$ ... $$) is not a DEFAULT."""
    out, i, n = [], 0, len(raw)
    while i < n:
        end = _skip_literal(raw, i)
        if end is not None:
            i = end; continue
        m = re.compile(r"DEFAULT\b", re.I).match(raw, i)
        if not m or (i > 0 and (raw[i - 1].isalnum() or raw[i - 1] in "_$")):
            i += 1; continue
        i, depth = m.end(), 0
        start = i
        while i < n:
            end = _skip_literal(raw, i)
            if end is not None:
                i = end; continue
            ch = raw[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0: break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            elif depth == 0 and re.match(r"\s(?:NOT\s+NULL|NULL|CHECK|REFERENCES|PRIMARY|UNIQUE|CONSTRAINT|GENERATED|COLLATE)\b", raw[i:i + 20], re.I):
                break
            i += 1
        out.append(raw[start:i])
    return out


_ASSIGN = re.compile(r"^[A-Za-z_][\w.]*(?:\[[^\]]*\])?\s*(?::=|=(?!=))")


def _do_body_calls(body: str) -> list[str]:
    """Calls in a DO block that are not built-ins: CALL, `x := f()` / `x = f()`, `SELECT f() INTO v`, and an IF / WHILE condition."""
    bad = []
    for st in split_statements(body):
        code = st["code"]
        for frag in re.split(r"\b(?:THEN|ELSE|BEGIN|LOOP|DECLARE)\b", code, flags=re.I):
            frag = frag.strip()
            if not frag:
                continue
            if re.match(r"CALL\b", frag, re.I):
                bad.append("call")
            elif ":=" in frag or _ASSIGN.match(frag) or re.match(r"(?:IF|ELSIF|ELSEIF|WHILE)\b", frag, re.I) \
                    or (re.match(r"SELECT\b", frag, re.I) and _top_level_has(frag, r"\bINTO\b")):
                bad += _unsafe_calls(frag)
    return bad


def _top_level_has(code: str, pattern: str) -> bool:
    """True when `pattern` matches `code` at bracket depth 0."""
    depth, flat = 0, []
    for ch in code:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        flat.append(ch if depth == 0 else " ")
    return re.search(pattern, "".join(flat), re.I) is not None


def lint_text(sql: str, deny: list[str] = ()) -> list[str]:
    """Findings (rule messages, never the offending values) for one migration's text."""
    out = []
    for st in split_statements(sql):
        code = st["code"]
        words = code.split(None, 1)
        first = re.match(r"[A-Za-z]+", words[0]).group(0).upper() if words and re.match(r"[A-Za-z]", words[0]) else (words[0].upper() if words else "")
        if first == "INSERT":
            m = re.match(r"INSERT\s+INTO\s+(?:ONLY\s+)?\"?(?:public\"?\.\"?)?(\w+)", code, re.I)
            table = (m.group(1) if m else "?").lower()
            if table not in SEED_TABLES:
                out.append(f"INSERT into '{table}', which is not an allowlisted seed table")
                continue
            if not re.search(r"\bVALUES\b", code, re.I) or re.search(r"\bSELECT\b", code, re.I):
                out.append(f"INSERT into seed table '{table}' must be literal VALUES, not a SELECT (that copies instance data)")
            if re.search(r"\bDO\s+UPDATE\b", code, re.I):
                out.append(f"INSERT into seed table '{table}' uses ON CONFLICT DO UPDATE, which overwrites edited rows (only DO NOTHING)")
            elif not re.search(r"\bON\s+CONFLICT\b", code, re.I) or not re.search(r"\bDO\s+NOTHING\b", code, re.I):
                out.append(f"INSERT into seed table '{table}' must end with ON CONFLICT ... DO NOTHING (a migration must be safe to re-run)")
            hits = _instance_hits(st["raw"], deny)
            if hits:
                out.append(f"INSERT into seed table '{table}' carries instance content ({', '.join(hits)})")
        elif first in DML_START:
            out.append(f"{first} is data, not schema")
        elif first == "WITH" or first == "SELECT":
            if first == "WITH" and WRITES.search(code):
                out.append("WITH statement writes data")
            if _top_level_has(code, r"\bINTO\b"):
                out.append("SELECT ... INTO creates a table from data")
            bad = _unsafe_calls(code)
            if bad:
                out.append("SELECT calls a function that is not a pure built-in, which can write data")
        elif first == "CALL":
            out.append("CALL runs a procedure, which can write data")
        elif first == "DO":
            # the body is dollar-quoted ($$ ... $$) or a plain single-quoted string: read both
            w = sorted({x for b in st["bodies"] + st["strings"] for x in _body_writes(b)})
            if w:
                out.append(f"DO block writes data ({', '.join(w)})")
            if any(_do_body_calls(b) for b in st["bodies"] + st["strings"]):
                out.append("DO block calls a function that is not a pure built-in, which can write data")
        elif first in ALLOWED_FIRST:
            if first == "CREATE":
                if re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?RULE\b", code, re.I):
                    out.append("CREATE RULE can rewrite later statements into data writes")
                elif re.match(r"CREATE\s+MATERIALIZED\s+VIEW\b", code, re.I):
                    out.append("CREATE MATERIALIZED VIEW ... AS copies data into a stored result")
                elif re.match(r"CREATE\s+(?:(?:GLOBAL\s+|LOCAL\s+)?(?:TEMP|TEMPORARY)\s+|UNLOGGED\s+)?TABLE\b", code, re.I) \
                        and _top_level_has(code, r"\bAS\b") and not re.search(r"\bPARTITION\s+OF\b", code, re.I):
                    out.append("CREATE TABLE ... AS copies data into a table")
                elif re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\b", code, re.I):
                    texts = st["bodies"] + st["strings"]
                    if any(FUNC_WRITES.search(split_code(t)) for t in texts):
                        out.append("function or procedure body writes data (a migration defines schema; data changes go through the app)")
            if first in ("CREATE", "ALTER"):
                for d in _default_exprs(st["raw"]):
                    hits = _instance_hits(d, deny)
                    if hits:
                        out.append(f"a DEFAULT carries instance content ({', '.join(hits)})")
                    if _unsafe_calls(split_code(d), DEFAULT_FUNCS):
                        out.append("a DEFAULT calls a function that is not a pure built-in")
        else:
            out.append(f"{first or 'this statement'} is not allowed in a migration (allowed: {', '.join(sorted(ALLOWED_FIRST))}, "
                       "a seed INSERT, a DO block, a plain SELECT)")
    return out


def load_grandfather() -> dict:
    return json.loads(GRANDFATHER.read_text(encoding="utf-8"))


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def sha(path: Path) -> str:
    """sha256 of the file with CRLF read as LF, so a Windows checkout (core.autocrlf) hashes the same as the repo."""
    return sha_bytes(path.read_bytes())


def denylist() -> list[str]:
    return [d.lower() for d in _guard.read_lines(_guard.home() / "denylist.txt")]


def _git(*args: str) -> bytes:
    import subprocess
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def git_sources(rev: str | None) -> dict[str, bytes]:
    """The migrations as COMMITTED, not as they sit in the working tree: the index when rev is None (what a commit is about to record),
    else the tree of `rev` (what a push sends). {file name: bytes}."""
    if rev is None:
        names = [l.split("\t", 1)[1] for l in _git("ls-files", "-s", "-z", "--", "db/migrations").decode().split("\0") if "\t" in l]
        read = lambda n: _git("show", ":" + n)  # noqa: E731
    else:
        names = [n for n in _git("ls-tree", "-r", "--name-only", "-z", rev, "--", "db/migrations").decode().split("\0") if n]
        read = lambda n: _git("show", f"{rev}:{n}")  # noqa: E731
    return {Path(n).name: read(n) for n in names if n.endswith((".sql", ".py"))}


def lint_tree(files: list[Path] | None = None, deny: list[str] = (), grandfather: dict | None = None,
              sources: dict[str, bytes] | None = None) -> tuple[list[str], dict]:
    """(problems, {file: findings}) over the migrations. A grandfathered file's findings are not problems while its hash matches.
    `sources` ({name: bytes}) lints those contents (e.g. the staged blobs) instead of reading the working tree."""
    if sources is None and not MIGRATIONS.is_dir():
        return [], {}  # a tree without migrations (e.g. a scratch repo that copies only scripts/) has nothing to lint
    gf = load_grandfather() if grandfather is None else grandfather
    if sources is None:
        files = sorted(list(MIGRATIONS.glob("*.sql")) + list(MIGRATIONS.glob("*.py"))) if files is None else files
        every = len(files) == len(list(MIGRATIONS.glob("*.sql"))) + len(list(MIGRATIONS.glob("*.py")))
        items = [(f.name, f.read_bytes()) for f in files]
    else:
        if not sources:
            return [], {}   # a tree without migrations (e.g. a scratch repo that copies only scripts/) has nothing to lint
        items, every = sorted(sources.items()), True
    problems, raw = [], {}
    for name, data in items:
        entry = gf.get(name)
        if name.endswith(".py"):
            raw[name] = [".py migration"]
            if entry is None:
                problems.append(f"{name}: a .py migration can run any code; a migration changes the schema only (data changes go "
                                "through app code or the instance repo)")
            elif sha_bytes(data) != entry["sha256"]:
                problems.append(f"{name}: grandfathered, but the file changed since it was listed (applied migrations are not edited)")
            continue
        found = lint_text(data.decode("utf-8"), deny)
        raw[name] = found
        if entry is None:
            problems += [f"{name}: {x}" for x in found]
        elif sha_bytes(data) != entry["sha256"]:
            problems.append(f"{name}: grandfathered, but the file changed since it was listed (applied migrations are not edited)")
        elif not found:
            problems.append(f"{name}: grandfathered, but it breaks no rule now; remove it from migration_lint_grandfather.json")
    if every:
        problems += [f"{n}: in the grandfather list but the file does not exist" for n in gf if n not in raw]
    return problems, raw


def _report(problems: list[str]) -> int:
    if problems:
        print("migration lint: a migration changes the schema only (see CONTRIBUTING.md, 'Migration lint').", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        print("Instance data belongs in the app or the private instance repo. Never add a new file to the grandfather list.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    """No argument: the working tree. --staged: the index (pre-commit). --push: read `git push` ref lines on stdin and lint the tree of
    every pushed commit (pre-push). Otherwise: the named files."""
    if argv == ["--staged"]:
        return _report(lint_tree(deny=denylist(), sources=git_sources(None))[0])
    if argv == ["--push"]:
        problems, seen = [], set()
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) < 2 or set(parts[1]) == {"0"} or parts[1] in seen:
                continue   # a deleted ref pushes no tree
            seen.add(parts[1])
            problems += lint_tree(deny=denylist(), sources=git_sources(parts[1]))[0]
        return _report(sorted(set(problems)))
    files = [Path(a).resolve() for a in argv] or None
    return _report(lint_tree(files, denylist())[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
