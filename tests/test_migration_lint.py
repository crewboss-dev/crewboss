"""Migration lint (task #589, scripts/migration_lint.py). No database needed: run `python -m pytest --noconftest tests/test_migration_lint.py`.
Names and addresses are assembled from pieces so this file carries none."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("migration_lint", ROOT / "scripts" / "migration_lint.py")
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)

INST = "Mal" + "len"
LAN = "192." + "168.1.20"


def test_the_migrations_pass():
    problems, _ = lint.lint_tree(deny=[])
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("sql", [
    "CREATE TABLE t (id int); ALTER TABLE t ADD COLUMN c text DEFAULT 'x';",
    "-- INSERT INTO tasks VALUES (1);\n/* UPDATE tasks SET a=1; */ CREATE INDEX i ON t(a);",
    "CREATE TABLE t (a int REFERENCES p(id) ON DELETE CASCADE ON UPDATE CASCADE);",
    "CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN NEW.a = 1; RETURN NEW; END $$ LANGUAGE plpgsql;",
    "ALTER TABLE t ADD CONSTRAINT c CHECK (a IN ('INSERT INTO x', 'DELETE FROM y'));",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='c') THEN ALTER TABLE t ADD CONSTRAINT c CHECK (a>0); END IF; END $$;",
    "INSERT INTO agent_profiles (name, model) VALUES ('builder', 'sonnet') ON CONFLICT (name) DO NOTHING;",
    "INSERT INTO agent_profiles (name, model) VALUES ('a', 'b'), ('c@example.com', 'd') ON CONFLICT DO NOTHING;",
    "ALTER TABLE t ADD COLUMN c text DEFAULT 'plain', ADD COLUMN d timestamptz DEFAULT now(), ADD COLUMN e varchar(20) DEFAULT 'x@example.com';",
    "CREATE TABLE p PARTITION OF q FOR VALUES IN (1); CREATE TABLE g (a int, b int GENERATED ALWAYS AS (a + 1) STORED);",
    "SELECT 1; SELECT count(*) FROM pg_class;",
    "BEGIN; CREATE INDEX i ON t(lower(a)); COMMIT;",
])
def test_clean_sql_passes(sql):
    assert lint.lint_text(sql) == []


@pytest.mark.parametrize("sql,needle", [
    ("INSERT INTO tasks (title) VALUES ('x');", "not an allowlisted seed table"),
    ("UPDATE tasks SET a = 1;", "UPDATE"),
    ("DELETE FROM tasks;", "DELETE"),
    ("COPY tasks FROM '/tmp/x';", "COPY"),
    ("TRUNCATE tasks;", "TRUNCATE"),
    ("WITH d AS (DELETE FROM tasks RETURNING 1) SELECT 1;", "WITH"),
    ("DO $$ BEGIN INSERT INTO tasks (title) VALUES ('x'); END $$;", "DO block writes"),
    ("DO $q$ BEGIN UPDATE tasks SET a = 1; END $q$;", "DO block writes"),
    ("DO $$ BEGIN PERFORM refresh_things(); END $$;", "DO block writes"),
    ("DO $$ BEGIN EXECUTE 'delete from tasks'; END $$;", "DO block writes"),
    ("INSERT INTO agent_profiles (name) VALUES ('" + INST + " builder');", "instance content"),
    ("INSERT INTO agent_profiles (name, description) VALUES ('a', 'host " + LAN + "');", "instance content"),
    ("INSERT INTO agent_profiles (name, description) VALUES ('a', 'ask bob@acme.io');", "instance content"),
    ("INSERT INTO agent_profiles (name) SELECT name FROM users;", "literal VALUES"),
    ("INSERT INTO agent_profiles (name) VALUES ('secret-host') ON CONFLICT DO NOTHING;", "denylist"),
    # the release-review probes (every one must be refused)
    ("DO 'BEGIN DELETE FROM tasks; END';", "DO block writes"),
    ("DO E'BEGIN DELETE FROM tasks; END' LANGUAGE plpgsql;", "DO block writes"),
    ("PREPARE p AS UPDATE tasks SET a = 1; EXECUTE p;", "PREPARE is not allowed"),
    ("PREPARE p AS UPDATE tasks SET a = 1; EXECUTE p;", "EXECUTE is not allowed"),
    ("EXPLAIN ANALYZE UPDATE tasks SET a = 1;", "EXPLAIN is not allowed"),
    ("VACUUM tasks;", "not allowed in a migration"),
    ("CREATE TABLE x AS SELECT * FROM tasks;", "CREATE TABLE ... AS"),
    ("CREATE TABLE x (a, b) AS SELECT 1, 2;", "CREATE TABLE ... AS"),
    ("SELECT id INTO x FROM tasks;", "SELECT ... INTO"),
    ("SELECT projects_refresh_partition(id) FROM projects;", "not a pure built-in"),
    ("SELECT pg_read_file('/etc/passwd');", "not a pure built-in"),
    ("SELECT setval('s', 1);", "not a pure built-in"),
    ("CREATE RULE r AS ON INSERT TO a DO ALSO DELETE FROM tasks;", "CREATE RULE"),
    ("INSERT INTO agent_profiles (name) VALUES ('a');", "ON CONFLICT"),
    ("INSERT INTO agent_profiles (name) VALUES ('a') ON CONFLICT (name) DO UPDATE SET name = 'b';", "DO UPDATE"),
    ("ALTER TABLE t ADD COLUMN c text DEFAULT '" + INST + " builder';", "DEFAULT carries instance content"),
    ("CREATE TABLE t (c text DEFAULT 'host " + LAN + "', d int);", "DEFAULT carries instance content"),
    ("ALTER TABLE t ADD COLUMN c text DEFAULT 'bob@acme.io';", "DEFAULT carries instance content"),
    ("ALTER TABLE t ADD COLUMN c text DEFAULT E'secret-host';", "DEFAULT carries instance content"),
    ("ALTER TABLE t ADD COLUMN c int DEFAULT wipe_things();", "DEFAULT calls a function"),
])
def test_bad_sql_is_flagged(sql, needle):
    found = lint.lint_text(sql, deny=["secret-host"])
    assert found and any(needle in f for f in found), found


def test_findings_never_echo_the_value():
    assert INST not in " ".join(lint.lint_text("INSERT INTO agent_profiles (name) VALUES ('" + INST + "');"))


def test_grandfather_entries_have_reasons_and_hashes():
    for name, e in lint.load_grandfather().items():
        assert e["reason"].strip() and len(e["sha256"]) == 64, name


def test_a_new_violating_migration_fails_and_an_edited_grandfathered_one_fails(tmp_path, monkeypatch):
    bad = tmp_path / "0999_bad.sql"
    bad.write_text("UPDATE tasks SET a = 1;")
    problems, _ = lint.lint_tree([bad], grandfather={})
    assert problems
    gf = {"0999_bad.sql": {"reason": "r", "sha256": lint.sha(bad)}}
    assert lint.lint_tree([bad], grandfather=gf)[0] == []
    bad.write_text("UPDATE tasks SET a = 2;")
    assert "changed" in lint.lint_tree([bad], grandfather=gf)[0][0]
    bad.write_text("CREATE TABLE t (a int);")
    gf["0999_bad.sql"]["sha256"] = lint.sha(bad)
    assert "remove it" in lint.lint_tree([bad], grandfather=gf)[0][0]


@pytest.mark.parametrize("sql,needle", [
    ("CREATE FUNCTION bf() RETURNS void AS $$ BEGIN UPDATE tasks SET x = 1; END $$ LANGUAGE plpgsql; SELECT bf();", "function or procedure"),
    ("CREATE FUNCTION bf() RETURNS void AS $$ BEGIN NULL; END $$ LANGUAGE plpgsql; SELECT bf();", "SELECT calls a function"),
    ("CALL do_things();", "CALL"),
    ("INSERT INTO agent_profiles (name, model) VALUES ('a', 'b') ON CONFLICT (name) DO UPDATE SET model = 'c';", "DO UPDATE"),
    ("UPDATE tasks SET x = 1; -- '", "UPDATE"),
    ("SELECT E'it\\'s'; UPDATE tasks SET x = 1;", "UPDATE"),
    ("SELECT 1 AS \"it's\"; UPDATE tasks SET x = 1;", "UPDATE"),
    ("SELECT 1 AS \"a$$b\"; UPDATE tasks SET x = 1;", "UPDATE"),
    ("INSERT INTO agent_profiles (name) VALUES (E'a\\' " + INST + "');", "instance content"),
])
def test_evasions_are_caught(sql, needle):
    found = lint.lint_text(sql)
    assert found and any(needle in f for f in found), found


def test_lexer_does_not_swallow_following_statements_after_escapes():
    assert lint.lint_text("SELECT E'it\\'s fine'; CREATE TABLE t (a int);") == []
    assert lint.lint_text("CREATE TABLE \"it's\" (a int);") == []


def test_a_new_py_migration_is_refused_and_crlf_hashes_the_same(tmp_path, monkeypatch):
    monkeypatch.setattr(lint, "MIGRATIONS", tmp_path)
    py = tmp_path / "0999_data.py"
    py.write_text("def migrate(conn): pass\n")
    problems, _ = lint.lint_tree(grandfather={})
    assert any(".py migration" in p for p in problems)
    gf = {"0999_data.py": {"reason": "r", "sha256": lint.sha(py)}}
    assert lint.lint_tree(grandfather=gf)[0] == []
    py.write_bytes(b"def migrate(conn): pass\r\n")
    assert lint.lint_tree(grandfather=gf)[0] == []


def test_the_real_py_migrations_are_all_grandfathered():
    gf = lint.load_grandfather()
    assert {p.name for p in lint.MIGRATIONS.glob("*.py")} <= set(gf)


def test_the_probe_findings_never_echo_the_value():
    out = " ".join(lint.lint_text("ALTER TABLE t ADD COLUMN c text DEFAULT '" + INST + " x';"))
    assert INST not in out and LAN not in out


def _git_repo(tmp_path):
    import subprocess
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "PATH": __import__("os").environ["PATH"], "HOME": str(tmp_path)}
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, env=env)  # noqa: E731
    run("init", "-q")
    (tmp_path / "db" / "migrations").mkdir(parents=True)
    return run


def test_the_hooks_lint_the_staged_blob_and_the_pushed_tree_not_the_working_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(lint, "ROOT", tmp_path)
    run = _git_repo(tmp_path)
    f = tmp_path / "db" / "migrations" / "0999_x.sql"
    f.write_text("DELETE FROM tasks;")
    run("add", "db/migrations/0999_x.sql")
    f.write_text("CREATE TABLE t (a int);")          # the working tree is clean; what is staged is not
    assert lint.lint_tree(grandfather={}, sources=lint.git_sources(None))[0]
    assert not lint.lint_tree(grandfather={}, files=[f])[0]
    run("commit", "-q", "-m", "bad")
    pushed = run("rev-parse", "HEAD").stdout.decode().strip()
    assert lint.lint_tree(grandfather={}, sources=lint.git_sources(pushed))[0]
    f.write_text("DELETE FROM tasks;")               # the opposite: a bad working tree, a clean staged blob
    run("add", "db/migrations/0999_x.sql")
    f.write_text("CREATE TABLE t (a int);")
    run("add", "db/migrations/0999_x.sql")
    assert not lint.lint_tree(grandfather={}, sources=lint.git_sources(None))[0]


def test_the_hook_scripts_pass_the_staged_and_pushed_modes():
    for name, flag in (("pre-commit", "--staged"), ("pre-push", "--push")):
        assert flag in (ROOT / "scripts" / "hooks" / name).read_text()


def test_the_guard_files_are_code_owned():
    owners = (ROOT / ".github" / "CODEOWNERS").read_text()
    for path in ("/scripts/ci_guard.sh", "/scripts/migration_lint.py", "/scripts/migration_lint_grandfather.json", "/tests/test_migration_lint.py"):
        assert path + " @OWNER" in owners


@pytest.mark.parametrize("sql", [
    "CREATE SEQUENCE s; CREATE TABLE t (id bigint DEFAULT nextval('s'), u uuid DEFAULT gen_random_uuid(), v uuid DEFAULT uuid_generate_v4());",
    "CREATE TABLE t (r double precision DEFAULT random(), k text DEFAULT encode(gen_random_bytes(16), 'hex'), c timestamptz DEFAULT clock_timestamp());",
    "ALTER TABLE t ADD COLUMN k text NOT NULL DEFAULT encode(gen_random_bytes(8),'hex');",
    "CREATE TABLE t (a character varying(20), b CAST(1 AS int));".replace("b CAST(1 AS int)", "b int"),
    "ALTER TABLE t ALTER COLUMN a TYPE character varying(40) USING a::text;",
    # DEFAULT inside a function body or a string literal is not a DEFAULT expression
    "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; /* DEFAULT wipe() */ END $$ LANGUAGE plpgsql;",
    "CREATE FUNCTION f() RETURNS int AS $$ DECLARE x int DEFAULT wipe_things(); BEGIN RETURN x; END $$ LANGUAGE plpgsql;",
    "COMMENT ON TABLE t IS 'the DEFAULT wipe() is documented here'; ALTER TABLE t ADD COLUMN c text DEFAULT 'DEFAULT x()';",
    "CREATE TABLE \"DEFAULT\" (\"a\" int);",
    # builtin calls in a DO body, and a DO that only guards DDL
    "DO $$ DECLARE n int := 1; BEGIN IF to_regclass('t') IS NULL THEN n := coalesce(n, 2); END IF; END $$;",
    'CREATE TABLE "t" ("a" int); ALTER TABLE "t" ADD COLUMN b text DEFAULT now();',
    "INSERT INTO \"agent_profiles\" (name) VALUES ('a') ON CONFLICT DO NOTHING;",
])
def test_default_and_body_false_refusals_are_gone(sql):
    assert lint.lint_text(sql) == [], lint.lint_text(sql)


@pytest.mark.parametrize("sql,needle", [
    # DEFAULT-only functions are still refused as a plain SELECT
    ("SELECT nextval('s');", "not a pure built-in"),
    ("SELECT uuid_generate_v4();", "not a pure built-in"),
    ("SELECT random();", "not a pure built-in"),
    ("SELECT encode(gen_random_bytes(8), 'hex');", "not a pure built-in"),
    ("ALTER TABLE t ADD COLUMN c int DEFAULT wipe_things();", "DEFAULT calls a function"),
    ("ALTER TABLE t ADD COLUMN c int DEFAULT setval('s', 1);", "DEFAULT calls a function"),
    # quoted function names
    ('SELECT "setval"(\'s\', 1);', "not a pure built-in"),
    ('SELECT public."wipe"();', "not a pure built-in"),
    ('SELECT "public"."wipe" ();', "not a pure built-in"),
    ('SELECT "my func"();', "not a pure built-in"),
    ('ALTER TABLE t ADD COLUMN c int DEFAULT "wipe_things"();', "DEFAULT calls a function"),
    # WITH ... SELECT ... INTO, materialized views
    ("WITH x AS (SELECT 1 AS a) SELECT a INTO copy_t FROM x;", "SELECT ... INTO"),
    ("CREATE MATERIALIZED VIEW mv AS SELECT * FROM tasks;", "MATERIALIZED VIEW"),
    ("CREATE MATERIALIZED VIEW IF NOT EXISTS mv AS SELECT 1 WITH NO DATA;", "MATERIALIZED VIEW"),
    # calls in a DO body
    ("DO $$ BEGIN CALL wipe_things(); END $$;", "DO block calls"),
    ("DO $$ DECLARE v int; BEGIN v := wipe_things(); END $$;", "DO block calls"),
    ("DO $$ DECLARE v int := wipe_things(); BEGIN NULL; END $$;", "DO block calls"),
    ("DO $$ DECLARE v int; BEGIN v = wipe_things(); END $$;", "DO block calls"),
    ("DO $$ DECLARE v int; BEGIN SELECT wipe_things() INTO v; END $$;", "DO block calls"),
    ("DO $$ DECLARE v int; BEGIN IF true THEN SELECT public.\"wipe\"() INTO v; END IF; END $$;", "DO block calls"),
    ("DO $$ BEGIN IF wipe_things() THEN NULL; END IF; END $$;", "DO block calls"),
    ("DO 'DECLARE v int; BEGIN v := wipe_things(); END';", "DO block calls"),
])
def test_cheap_lint_bypasses_are_refused(sql, needle):
    found = lint.lint_text(sql)
    assert found and any(needle in f for f in found), found
