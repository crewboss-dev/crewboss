# Contributing to Crewboss

Thank you for helping. This page covers how to set up, run the tests, send a change and sign it off.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report security problems as described in
[SECURITY.md](SECURITY.md), not in a public issue.

## Set up a dev environment

You need Docker (the tests start a throw-away Postgres container), Python 3.12 (the app image uses 3.12, and the runner needs 3.12
or newer), and git. Node 20 is needed only if you change the front end.

```
git clone <your fork>
cd crewboss
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest ./runner
scripts/install-hooks.sh
```

`scripts/install-hooks.sh` points `core.hooksPath` at `scripts/hooks`. The pre-commit hook (`scripts/commit_guard.py`) blocks a
commit that contains a secret-shaped string or a value from your private denylist (`$CREWBOSS_HOME/denylist.txt`, optional, one
value per line). It also runs `gitleaks` on the staged changes when `gitleaks` is installed. CI runs the same guard over the
whole tree.

### The leak guards

Every change headed for the public repository passes three guards, so that no real company, person, hostname, internal address or
secret ships by accident:

1. **Push guard (automatic, free).** `scripts/install-hooks.sh` also enables `scripts/hooks/pre-push`, which runs
   `scripts/push_guard.py` on the commits being pushed and blocks the push on any hit. It checks, over every commit pushed (not just the
   tip), and a value added then removed again still counts:
   * file names: `.env*`, `*.bak`, `*.dump`, `*.sqlite*`, `*.db`, `*.pem`, `*.key`, `*.p12`, `id_*`, `credentials.json`, `*.tfstate`,
     `.npmrc` and similar secret-bearing files are refused (templates ending `.example`, `.sample` or `.template`, and `*.pub`, are
     fine). A binary file cannot be scanned, so each is listed and blocked unless it matches an asset pattern in
     `scripts/binary-allow.txt`;
   * every added line, merge conflict resolutions included, against your private denylist, secret patterns and RFC1918 or CGNAT
     addresses (10.x, 172.16-31.x, 192.168.x, 100.64-127.x), including three-octet prefixes and regex-escaped forms;
   * metadata, which is public too: commit messages and author names, file paths, branch and tag names, and annotated tag text;
   * the tree at the pushed commit (`commit_guard.py --strict`), and `gitleaks` over the pushed commits (needs `gitleaks` on your PATH;
     set `CREWBOSS_REQUIRE_GITLEAKS=1` to make a missing binary a failure);
   * the address baseline `scripts/internal-ip-baseline.json` may only go down, compared with the commit your push builds on.

   Messages name files and rules, never values. Use documentation addresses (192.0.2.x, 198.51.100.x, 203.0.113.x) and names ending
   `.example`, `.test` or `.invalid` in tests and docs. Do not bypass the hook; fix the commit. After removing addresses from the tree, run
   `python3 scripts/commit_guard.py --write-ip-baseline` to lower the baseline. A first push of a single commit to an empty remote is
   checked by its tree instead of as "added" lines, so the first public release needs no bypass.
2. **CI guard (`.github/workflows/guard.yml`)**, on every push and pull request, with its actions pinned by commit SHA: gitleaks over the
   history of the push or pull request (a new branch is compared with the default branch), the reference ratchet test, and the commit
   guard with its built-in patterns, over the same added lines, metadata, file names and binaries as the hook. The private denylist is not
   available there, which the guard says in its output.
3. **AI diff read (optional).** When a maintainer sets the repository secret `ANTHROPIC_API_KEY`, the `ai-diff-read` job sends only the
   pull request's diff to Claude and fails the check unless the answer is `PASS`. Without the secret (and on fork pull requests, which
   never receive secrets) it logs a warning that the diff was not read.

### Migration lint

A migration changes the schema only. `scripts/migration_lint.py` (run by the pre-commit and pre-push hooks, by the CI guard, and by
`python -m pytest --noconftest tests/test_migration_lint.py`) reads every file in `db/migrations/`. The hooks read what is being
committed or pushed (the staged blobs, the pushed commits' trees), not the working tree. It works from an allowlist: a statement must
start with `CREATE`, `ALTER`, `DROP`, `COMMENT`, `GRANT`, `REVOKE`, `SET` (or `BEGIN`/`COMMIT`), or be a seed `INSERT`, a `DO` block or a
plain `SELECT`; anything else (`PREPARE`, `EXECUTE`, `EXPLAIN`, `VACUUM`...) is refused. It also refuses:

* `CREATE TABLE ... AS`, `SELECT ... INTO`, `CREATE RULE`, and a top-level `SELECT` that calls any function that is not a pure built-in
  (`BUILTIN_FUNCS` in the script);
* a seed `INSERT` without `ON CONFLICT ... DO NOTHING`, and instance content (the checks below) in a `DEFAULT` of a `CREATE` or `ALTER`;
* a `DO` block that writes, whether it is quoted with `$$` or with single quotes;

* `INSERT`, `UPDATE`, `DELETE`, `COPY`, `TRUNCATE` or `MERGE`, except an `INSERT ... VALUES` into a generic seed table (the `SEED_TABLES`
  list in the script, currently `agent_profiles` and `global_plan_state`);
* an `INSERT` into a seed table that carries instance content: the owner's name (the word the reference ratchet counts), an internal
  address, an email address outside the reserved example domains, or a value from your private denylist; or that copies rows with
  `SELECT`;
* a `DO` block (or `WITH` statement) that writes data: `INSERT INTO`, `UPDATE ... SET`, `DELETE FROM`, `COPY`, `TRUNCATE`, `EXECUTE`
  or `PERFORM`. A `DO` block that only adds a guarded constraint is fine.

* any new `.py` migration (`app/db.py` still applies the five old ones, but a `.py` file can run anything, so no new one is accepted: do
  a data change in app code or your private instance repo);
* `CALL`, a function or procedure body that writes data, a `SELECT` of a function created in the same file, and `ON CONFLICT ... DO
  UPDATE` on a seed table (only `DO NOTHING`).

A `DEFAULT` may call the built-ins plus `nextval`, `gen_random_uuid`, `uuid_generate_v4`, `random`, `encode(gen_random_bytes(n), 'hex')`,
`now` and `clock_timestamp` (not as a top-level `SELECT`). Quoted function names (`"setval"(...)`, `public."wipe"()`) are read as the
names they are. `SELECT ... INTO` (also after `WITH`), `CREATE MATERIALIZED VIEW` and, in a `DO` block, a `CALL`, an assignment
(`x := f()`), `SELECT f() INTO v` or an `IF` condition that calls a function that is not a built-in are refused.

**Refused by design** (a migration has no use for them; if you think yours does, say so in the pull request): `RESET`, `LOCK`, `ANALYZE`,
`REINDEX`, `REFRESH MATERIALIZED VIEW`, and advisory locks (`pg_advisory_lock` and friends).

**Known limits** (the lint is a tripwire for honest mistakes, not a sandbox): it does not look inside index, `CHECK` or `GENERATED`
expressions for calls to user functions, nor at dynamic `EXECUTE` inside a function body. `.github/CODEOWNERS` review of every
migration is the real control.

Instance data goes through the app or your private instance repo, never a migration. Applied migrations are not rewritten: the ones that
predate the rule are in `scripts/migration_lint_grandfather.json`, each with a reason and the file's sha256 (line endings read as LF; `.gitattributes` keeps migrations unconverted), so editing one (or leaving
a stale entry) fails the lint. Never add a new migration to that list; fix the migration. Adding a seed table is a reviewed change.

**Images are not content-scanned by any automatic check: a human looks at every added image before a release.**

**To publish to a NEW public remote**, create a fresh one-commit history in a separate clone and push that; never re-point an existing
clone's `origin` at a new remote. The hook asks the remote itself what it already has (never the remote-tracking refs, which describe
the old remote), but a clean one-commit history is the only thing that is safe to make public.

`.github/CODEOWNERS` assigns the guard files (workflows, `scripts/migration_lint.py`, its grandfather list and test, `scripts/ci_guard.sh`, `scripts/commit_guard.py`, `scripts/push_guard.py`, `scripts/ai_diff_read.py`,
`.gitleaks.toml`, the baseline and the binary allowlist) to `@OWNER`. Replace that placeholder with your GitHub user or team, and turn on
"Require review from Code Owners" in branch protection so a pull request cannot loosen the guards by itself.

Run the guards yourself: `python3 scripts/commit_guard.py --strict` (add `--added-in origin/main..HEAD` to check what you added), and
`python -m pytest --noconftest tests/test_push_guard.py` (no database needed).

To run the app itself from a checkout, see [docs/getting-started.md](docs/getting-started.md). The stylesheet and vendor scripts
are built in the Docker image; to build them locally, run `npm install` then `npm run build:css` and `npm run build:vendor`
in `frontend/`.

## Run the tests

```
.venv/bin/python -m pytest -q                       # the whole suite
.venv/bin/python -m pytest tests/test_jobs.py -q    # one file
```

The suite needs a running Docker daemon. `tests/conftest.py` starts a throw-away `postgres:16-alpine` container (database
`dashboard_test`) bound to `127.0.0.1`, runs the migrations against it, and removes it when the run ends. It never touches a
real database: `DATABASE_URL` is overwritten, and the suite reads `tests/home` instead of your real `CREWBOSS_HOME`.

By default each run picks a container name unique to it (`crewboss-test-db-<pid>`) and a free port, so parallel runs never collide. Two variables pin them:

| Variable | Default | Meaning |
|---|---|---|
| `CREWBOSS_TEST_CONTAINER` | `crewboss-test-db-<pid>` | name of the throw-away Postgres container |
| `CREWBOSS_TEST_PORT` | a free port | host port it listens on (on 127.0.0.1) |

```
CREWBOSS_TEST_CONTAINER=crewboss-test-b CREWBOSS_TEST_PORT=55433 .venv/bin/python -m pytest -q
```

The browser tests in `tests/e2e` are skipped unless you pass `-m e2e` or set `CREWBOSS_E2E=1`; see `tests/e2e/README.md`.
The JavaScript tests (`tests/*.test.mjs`) run with Node.

## House rules

* **No instance-specific strings in the code.** `tests/test_no_mallen_refs.py` counts the word "mallen" (any case) in every tracked
  text file and fails if a file has more than its count in `tests/mallen_refs_baseline.json`. The counts may only go down. New code
  puts hostnames, company names, URLs, project slugs and people into the instance config (`instance.yaml`, read by
  `app/instance.py`), never into the code. Examples and docs use `example.com`.
* **No secrets, hosts or personal data in the repo**, including tests and docs. The commit guard and CI check for this.
* **Install-specific values are config.** A new setting goes in `app/config.py` with a neutral default, an `instance.yaml` key
  where it makes sense, and a line in `instance.example.yaml`.
* **Migrations** are numbered SQL files in `db/migrations`. Add a new one; do not edit one that has shipped.
* **Docker Compose is always pinned.** Never run a bare `docker compose` where another project may be running: the project name
  can come from your shell. `./crewboss` passes `-p`, `--env-file` and `-f` and strips `COMPOSE_*` variables from the
  environment. Use it, or do the same.
* **Docs change with the code.** If a change alters behaviour that a file in `docs/` describes, update that file in the same pull request.
* Keep the writing plain and direct.

## Branch and pull request flow

1. Fork the repository and branch from `main`: `git switch -c fix/short-name origin/main`.
2. Make a focused change with tests. Run the whole suite before you push.
3. Commit with a sign-off (below). Use a short subject line, then say why the change is needed.
4. Push to your fork and open a pull request against `main`. Say what changed, how you tested it, and what a reviewer should look at closely.
5. A maintainer reviews it. Changes to sign-in, step-up, the runner protocol, agent pinning, SSH passes or deploys get extra care and may take longer.
6. A maintainer merges when the checks pass. Push new commits rather than force-pushing over review comments.

## Sign your work (DCO)

Crewboss uses the [Developer Certificate of Origin](https://developercertificate.org/), version 1.1. There is no contributor
licence agreement. Add a `Signed-off-by` line to every commit with `git commit -s`:

```
Signed-off-by: Your Name <you@example.com>
```

The name and email come from your git config and must be the ones you really commit under. If you forgot, `git commit --amend -s`
fixes the last commit, and `git rebase --signoff origin/main` fixes a whole branch. Your contribution is licensed under the
Apache License 2.0 (see [LICENSE](LICENSE)).

The full text of the DCO 1.1:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same license (unless I am permitted to submit
    under a different license), as indicated in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

Questions about the DCO or the licence: open a GitHub issue or discussion.
