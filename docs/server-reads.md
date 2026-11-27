# Server reads (#855)

A job runs on a runner that has the code but cannot see the live server, so it cannot diagnose a production bug from logs. Server reads
give it the server's view without giving it the server.

The job calls three tools on the job MCP:

| Tool | What it does |
|---|---|
| `list_server_reads(fence)` | What the job's own project has declared, per environment, and whether production reads are on. |
| `read_logs(fence, env, name, since, until, grep, lines)` | A declared container log, unit journal or log file. |
| `probe(fence, env, name, grep, lines)` | A declared, fixed probe command. |

Crewboss runs each read itself, on the server, and returns capped, redacted, untrusted-wrapped text. No model and no shell run on the
server, and the runner never holds a pass.

## Declaring reads

On the project's **Where it lives** page (`/<slug>/where`, "Server reads") the project's Owner declares, per deploy target (a server plus an
environment), the reads a job may use:

| Kind | Spec | Runs |
|---|---|---|
| Container logs | a Docker container name | `docker logs [--since] [--until] --tail N <container>` |
| Unit journal | a systemd unit name | `journalctl -u <unit> [--since] [--until] -n N --no-pager -o short-iso` |
| Log file | an absolute path under `/var/log/`, `/opt/` or `/srv/` | the last N lines of the file (a file has no time window) |
| Probe | one command and its arguments | exactly that argument list, and only if it fits the read-only allowlist below |

A job cannot add, change or choose a command. It passes only a read's name and three filters:

* `since` / `until`: ISO-8601 (`2026-11-19T10:00:00Z`) or a relative age (`30m`, `2h`, `1d`), at most 7 days back.
* `grep`: one fixed string of at most 200 characters (a substring test, never a pattern).
* `lines`: how many lines at most (default 500, maximum 5000).

Production is separate: a read on a production target is refused unless the project's **Production reads** switch is on. Dev and staging
reads work whenever they are declared. Changing reads and the switch needs the same right as deploy targets (the project's Owner) and a fresh sign-in.

## Probes: a fixed allowlist

**Probes run as root on the server.** A probe is checked against a fixed table of programs and, per program, read-only subcommands and options.
Anything else is refused when it is declared, again when Crewboss builds the command, and again by `crewboss-read` on the server. The table is
identical in `app/server_reads.py` and `scripts/crewboss-read` (a test pins them together); adding to it is a code change in both.

| Program | Allowed |
|---|---|
| `systemctl` | `status`, `show`, `is-active`, `is-enabled`, `is-failed`, `list-units`, `list-timers` (a few display flags) |
| `docker` | `ps`, `inspect`, `logs`, `stats --no-stream`, `images`, `version`, `info` |
| `journalctl` | reading only: `-u`, `-n`, `--since`, `--until`, `-p`, `-o`, `-g`, `-b`, `--no-pager` and similar (no `--vacuum*`, `--rotate`, `--flush`, `--setup-keys`, `-f`) |
| `asterisk` | `-rx "<command>"` where the command starts with `show`, `core show`, `pjsip show`, `sip show`, `dialplan show` or `database show` |
| `ip` | `addr`, `link`, `route` with `show` (and `-s`, `-4`, `-6`), an optional device |
| `ss`, `df`, `free`, `uptime`, `uname`, `hostname`, `ps` | display flags only (`free -s`, `hostname NAME` and the like are refused) |
| `cat`, `head`, `tail` | only files under `/var/log/`, real files only (a symlink out of the folder is refused at run time); no `-f` / `--follow` |

The program is found by its name in `/usr/bin`, `/bin`, `/usr/sbin`, `/sbin` only (an absolute path must be in one of them), never on `PATH`.
Following a log (`-f`, `--follow`, `-F`, `--retry`) is refused everywhere.

**Installing the generated conf file is the server admin's approval of each read listed in it.** Review it before saving it on the server.

## Security model

Four layers, each enough to stop most of what the next one has to catch:

1. **Crewboss checks the job.** The job's token names one job, one task, one project; only that project's declared reads are visible. The
   fence must be live (a stale fence is `409 Conflict`). Every parameter is validated. At most 60 reads per job per hour. Production needs the switch.
2. **The pass can run one command.** Crewboss signs a throw-away key with the account's SSH CA: valid one minute, `force-command` set to
   `sudo -n /usr/local/bin/crewboss-read <name> ...` (every argument shell-quoted), no pty, no port forwarding. Servers that connect through
   a jump host, and servers that are not commissioned or not ready, are refused.
3. **The server decides what a name means.** (reads run as root, so it also re-checks the probe allowlist.) `crewboss-read` runs only names listed in the server's own `/etc/crewboss/reads.conf` or
   `/etc/crewboss/reads.d/*.conf` (root-owned, not writable by others) and re-validates the parameters. Even a compromised Crewboss can only
   read what that file lists. The helper starts commands as argument lists and never a shell; filtering is done in Python. A read has a
   20 s limit enforced even while the command is silent (its whole process group is killed). A log file is opened only if it is a regular
   file reached without a symlink.
4. **The output is data.** Crewboss reads at most 2 MB from the ssh connection. It is capped at 200 KB (the end of the log is kept, with a note), scrubbed of secrets on a best-effort basis (private keys,
   Authorization / Cookie headers, passwords inside URLs, token and key query parameters, AWS keys, JWTs, `mysql -p`, password hashes, long opaque strings), and returned wrapped between random-nonce markers with an `untrusted` flag.
   Logs carry text written by strangers (SIP headers, web requests); the tool descriptions tell the agent to treat it as data and never follow it.

Every attempt, allowed or refused, is a `server_read_log` row (job, task, project, read, server, parameters, bytes, outcome); a successful one also
adds one line to the task's activity. The row is written as `pending` before the pass is signed (so reads in flight count toward the limit) and it survives the project being deleted. The page lists recent reads per server and the task's run panel lists what the job read. Output is never stored.

Not possible through these tools: changing anything on a server, running a command a person did not declare, reading a path or unit nobody listed.

## Setting up a server (once, as root)

1. Copy `scripts/crewboss-read` from the Crewboss repo to `/usr/local/bin/crewboss-read`, root-owned, mode 0755.
2. Create `/etc/crewboss` and `/etc/crewboss/reads.d`, root-owned, mode 0755 (the helper refuses a config that anyone but root can write).
3. Allow only that program with sudo for the server's login user (the user named on the server's page) in `/etc/sudoers.d/crewboss-read`, mode 0440:

   ```
   LOGIN_USER ALL=(root) NOPASSWD: /usr/local/bin/crewboss-read *
   ```

   Check with `visudo -cf /etc/sudoers.d/crewboss-read`.
4. Save the generated file shown under the target's "Server setup" as `/etc/crewboss/reads.d/<project>.conf` (root-owned, mode 0644), and again
   whenever the declared reads change. (A single `/etc/crewboss/reads.conf` works too; the helper reads that and every `reads.d/*.conf`.)
5. Test on the server: `sudo -n /usr/local/bin/crewboss-read <project>.<target>.<name> --lines 5`.

The server must already be commissioned in Crewboss with a pinned host key and Ready.

### Example for a PBX-style server

Declared reads on a project `pbx`, target `prod`:

| Name | Kind | Spec |
|---|---|---|
| `asterisk-full` | Log file | `/var/log/asterisk/full` |
| `flexisip` | Unit journal | `flexisip-proxy.service` |
| `contacts` | Probe | `asterisk -rx "pjsip show contacts"` |

The generated `/etc/crewboss/reads.d/pbx.conf` (tab-separated):

```
# crewboss server reads for pbx
pbx.prod.asterisk-full	log_file	/var/log/asterisk/full
pbx.prod.flexisip	log_unit	flexisip-proxy.service
pbx.prod.contacts	probe	["asterisk","-rx","pjsip show contacts"]
```

A job then calls `read_logs(fence, "prod", "flexisip", since="30m", grep="REGISTER", lines=200)` or `probe(fence, "prod", "contacts")`.

## Helper exit codes

| Code | Meaning |
|---|---|
| 0 | The read ran and the command succeeded (an empty result is still 0). |
| 2 | Usage error, or a parameter the helper refuses. |
| 3 | The name is not listed in the server's reads.conf. |
| 4 | reads.conf is missing, not root-owned, writable by others, or the listed spec is not allowed. |
| 5 | The command could not start, could not be read, or ran longer than 20 s and was stopped. |
| 6 | The command ran and exited non-zero (its output is still returned). |

## Limits of this phase

No state-changing diagnostic actions, no access for the assistant (CA) tools, no jump or via servers, no full-access tester on staging.
Redaction is best effort: do not point a read at a file that holds secrets.
