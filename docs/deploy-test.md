# Deployment test

Before you ship a release, prove that a stranger can install it. `./crewboss deploy-test` builds a throwaway copy from the exact
commit you are about to publish, checks that it works, and deletes it. It never touches your real install.

```
./crewboss deploy-test                 # install, check, delete (about a minute with a warm Docker cache, more on a cold one)
./crewboss deploy-test --ref v1.4.0    # test a tag, branch or commit instead of origin/main
./crewboss deploy-test --keep          # after the checks keep it up for 60 minutes (--keep 15 for 15) so you can look
./crewboss deploy-test down <id>       # delete a kept trial now (Ctrl-C in the waiting terminal does the same)
./crewboss deploy-test clean           # delete the failure folders kept after failed runs
./crewboss deploy-test list            # trials still on this machine
```

## What it does

1. `git archive` of the commit into a new folder under your temp directory: only tracked files, exactly what a clone gets.
2. The normal install of that copy: `./crewboss install --mode localhost --yes` with its own instance folder, its own Compose
   project `crewboss-trial-<timestamp>`, a free port, fresh secrets and an empty database.
3. Checks, each printed as `PASS`, `FAIL` or `SKIP`: the app answers `/healthz`; the sign-in page loads and an owner exists; then, in
   a headless browser, the owner signs in with the one-time password, adds a passkey (a virtual one), saves the recovery codes and
   creates a project. The first-run tour is walked there too, step by step to Done (the project is made with its "Make a project now" button), asserting no off-site navigation and no 4xx.
4. With `--keep`: it publishes the trial on this machine's private network address (the first private address in `hostname -I`;
   `--lan-ip` overrides) and prints it. It leads with the `ssh -L <port>:localhost:<port>` line to run on the other computer, because browsers only sign in from
   the app's own address and passkeys need localhost or https. The LAN address itself shows the sign-in page only.
5. Teardown: the containers, the volumes, the network and the image of that trial's Compose project (found by its project label),
   then the folder. It then checks nothing is left and says so.

The browser checks need Playwright for Python. If your `python3` has none, the script makes a small virtual environment inside
the trial folder and installs it (needs network). Set `DEPLOY_TEST_PYTHON` to a Python that already has it. Without a browser the
checks print `SKIP`, never a false `PASS`. Exit status is 0 only when no check failed.

## Why it can't hurt your real install

* It removes every `COMPOSE_*`, `CREWBOSS_*`, `POSTGRES_*` and `APP_PORT` variable from its own environment before doing anything.
* It refuses any project name that is not `crewboss-trial-<18 digits>`, so never `crewboss`.
* It refuses any folder that is not under the temp directory, or that is, contains or sits inside the default instance folders
  (`~/.crewboss`, `~/.config/crewboss`, `~/.local/share/crewboss`) or a folder your shell's `CREWBOSS_*` variables point at.
* It removes only what carries its own Compose project label. There is no prune, no `down` of any other project.

After a failed run, the screenshot, the page and the installer log are kept in `<temp>/crewboss-trial-<id>-failure/` (mode 0700, files
0600, the one-time password removed from the log). Delete one with `./crewboss deploy-test down <id>-failure`, or all with `clean`.

The trial's files are in `<temp>/crewboss-trial-<id>/` while it runs: `src/` (the copy), `home/` (its instance folder) and
`install.log`.
