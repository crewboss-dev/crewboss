# Configuration

*For advanced users. A normal install never needs to edit anything here: `./crewboss install` writes it for you.*

Each Crewboss install keeps its own settings outside the repository, in the **instance folder**: `CREWBOSS_HOME`
(default `~/.crewboss`; mounted read-only at `/crewboss-home` in the container). `./crewboss install` creates it.

## Files in the instance folder

| File | Made by | Holds |
|---|---|---|
| `instance.yaml` | the installer; start from `instance.example.yaml` | this install's name, URLs, hosts, sign-in mode, roles, add-ons |
| `compose.env` | the installer (mode 0600) | what `./crewboss` hands to Docker Compose: project name, port, database password, folders |
| `secrets/` | the installer (mode 0700) | `ssh-ca-enc-key`, `vapid-private.pem`, `runner-identity-key`, `local-auth.key`. Back them up with the database |
| `materials/` | the installer | shared files (logos, templates) |
| `drift.yaml`, `health_feeds.yaml` | you, from `drift.example.yaml`, `health_feeds.example.yaml` | optional: linked admin sites; how a project's health arrives |
| `seed.yaml` | you, from `seed.example.yaml` | optional: the project and tasks created on first start. Without it one generic "example" project is created |
| `denylist.txt` | you | optional: values the commit guard keeps out of the repository |
| `frontdoor/` | the installer (LAN and own-address modes) | the front door's files: `front_door.json` (mode, address, certificate state; written by the sidecar, read by the app), the LAN certificate and its key (`lan.key`, 0600), and the Caddyfile the sidecar renders. Back it up. A hand-edited Caddyfile is left alone and the Front door card is then read-only |
| `compose.override.yml` | you | optional: extra mounts and add-ons, added as a second `-f` |

The repository's own `.env` is never read. A missing or empty `instance.yaml` is a fresh install with neutral defaults and the
app still starts. A file that is not a YAML mapping, or a value of the wrong type, stops the app at start-up with an error naming the key.
An environment variable `CREWBOSS_*` overrides the matching `instance.yaml` key. After editing `instance.yaml`, run `./crewboss restart`.

## instance.yaml keys

These are the keys the code reads (`app/instance.py` and `app/config.py`). `instance.example.yaml` has a commented copy to start from.

| Key | Type | Meaning |
|---|---|---|
| `name` | string | The install's display name. |
| `public_url` | string | The app's base URL. Default `http://localhost:8931`. Passkeys need a signed certificate here (or localhost). Keep it stable: passkeys are tied to the name. |
| `erp_url` | string | An optional link to another system, shown in the menu drawer. |
| `auth` | `local` or `oidc` | Sign-in mode. `local` (default) is password plus authenticator app with recovery codes; passkeys are optional (`auth_passkeys`). `oidc` is Microsoft (Entra) sign-in and needs `CREWBOSS_OIDC_TENANT_ID`, `CREWBOSS_OIDC_CLIENT_ID`, a client secret file and `CREWBOSS_SUPERADMIN_OIDS`. An install that already has OIDC configured and no `auth:` key stays on `oidc`. |
| `auth_passkeys` | bool | Offer passkeys (passwordless sign-in) as an upgrade. Unset: on when the front door has a publicly trusted certificate, off otherwise. `false` forces password plus authenticator app only. A passkey is tied to the host name (`rp_id`), so it never works on a LAN address or a self-signed certificate. |
| `trusted_proxies` | list of CIDRs | Reverse proxies whose `X-Forwarded-For` is believed (sign-in rate limits use the client address). Default: loopback only. `./crewboss install` writes your proxy's actual address. List the address your proxy connects from if it is not loopback. Set `[]` if you expose the app directly with no proxy. |
| `origin_hosts` | list | The app's own origins (`host[:port]`, as a browser sends them in `Origin`), for the same-origin guard. |
| `mcp_allowed_hosts` | list | Hosts the MCP transport accepts (a DNS-rebinding guard). `127.0.0.1` and `localhost` are always allowed. |
| `servers` | list | Machine names where a project's dev, staging or production can live (offered when you set up a project). |
| `tls_check` | `warn`, `enforce` or `off` | Certificate check for an https `public_url`. `warn` (default if absent) logs and shows a banner. `enforce` also refuses to start without a good certificate, after a 150 s grace. The installer writes `enforce` for new network installs. |
| `tls_check_connect` | `host:port` | Make the certificate check dial this address (for example the bundled proxy) while still verifying the public name. |
| `ca_model` | string | The Claude model CA chat turns use (passed to `claude --model` on your runner). Default `sonnet`. |
| `timezone` | string | Your local time zone (IANA name, e.g. `Europe/Berlin`): the CA's reminders, the Board's "today" and "this week", the morning re-plan and the times shown for servers. Default UTC. |
| `roles` | mapping | Which project (by slug) plays each part: `overseer`, `health`, `default`, `legacy_token`, `export`, `dashboard`. A role with no slug is unfilled and grants nothing. With no `overseer`, the tree's top-level partitions merge. |
| `catalog_project` | string | The project a catalog import uses when none is named. |
| `project_repos` | mapping | Slug to repository path, for building each project's catalog. |
| `repos_dir` | string | Folder searched for repositories to suggest when you set up a project. Default `~/repos`. |
| `platform_key_stores` | mapping | One entry per project that has a platform key store (its `url` and a `key_file`, a 0600 file holding the single writer credential). Default `{}`. |
| `addons` | list | Add-on names to load (entry-point group `dash.addons`). A merely installed package is never loaded. |
| `menu_links` | list | Extra links at the bottom of the menu drawer: each needs a `label` (1 to 40 characters) and an http(s) `url`. A bad entry is skipped with a warning. |
| `speech` | mapping | CA speech-to-text: `provider: aws_transcribe`, `region`, `language`. With no provider the microphone is hidden. Credentials come from a 0600 file at `/run/secrets/speech-credentials` (override with `CREWBOSS_SPEECH_CREDENTIALS_FILE`), never from this file. |
| `ca_uploads_dir` | string | Where files attached to CA chats are kept. Default `/data/ca-uploads` (inside the container). Back it up. |
| `erp_health_code_faults_only` | bool | Track only events tagged `code_fault` in a health feed. Default `false`. Env `CREWBOSS_ERP_HEALTH_CODE_FAULTS_ONLY` wins. |
| `zabbix_url` | string | The Zabbix API address (`https://zabbix.example.com/api_jsonrpc.php`). Default empty: the Zabbix feed is off. |
| `zabbix_groupids` | list | Zabbix host groups watched. Default none. |
| `vapid_sub` | string | The `mailto:` claim Web Push services may use to contact the sender. |
| `server_health_keys` | map | Zabbix item keys for the Servers page readings that come from your own agent template: `reboot`, `sec_updates`, `updates`. Unset = those readings are not shown. |
| `fallback_session_slug` | string | The slug of the account the session launcher's boot fallback names (first word of `CREWBOSS_FALLBACK_SESSION`); a rename of that project is refused. Default none. |
| `slug_prefix` | string | A prefix dropped from a project slug when its session short name is derived (`acme-` makes `acme-erp` get `erp`). Default none. |
| `job_mcp_servers` | list | Extra MCP servers every runner job gets beside the built-in one. Each has `name`, `url`, optional `projects` (slugs). It is reached from the runner PC's own network and no credential is sent. |
| `merge_executor` | mapping | `bot_login` (the GitHub account Crewboss merges as) and `deny_logins` (human accounts it must refuse). Turning the merge executor on is refused without a bot login. |
| `runner_repo` | string | The repository URL shown in the runner install how-to. Default: the public Crewboss repository (`https://github.com/crewboss-dev/crewboss.git`). |
| `runner_repo_private` | bool | Add a "sign git in to GitHub" step to that how-to. Default `false`. |
| `connections` | mapping | Named external connections shown on the Connections page (see `app/config.py`). |
| `chrome_upstream_path` | string | Developer use: a path the UI-drift test compares against. Unset skips it. |

## Front door (LAN and own web address)

`./crewboss install --mode lan` serves the app over https with a self-signed certificate for the machine's LAN address (add more
names with `--lan-host`). Browsers show a one-time warning; nothing is installed on client machines. If the machine's address
changes, run `./crewboss lan-address` to reissue the certificate. Administration, then Front door, upgrades the same install to a
Let's Encrypt certificate for your own domain (HTTP-01: the name must point here and port 80 must reach the machine). The app talks to
a small sidecar (`frontdoor_agent`) that only accepts typed actions from the holder of `frontdoor-token`; it does not hand out
Caddy config, files or Docker. The proxy runs as the `frontdoor` service; `./crewboss up` also upgrades an install from before the
sidecar existed (its bundled proxy settings move under it, via `ensure_frontdoor`) and nothing else changes.

### Opening the app from another computer (details)

The app listens on `127.0.0.1` only, and a browser may sign in only from the app's own address (`origin_hosts`): it refuses writes from
any other address (403 "Origin not a dashboard host"). The ways to reach it:

* **SSH tunnel** (quick, nothing exposed): on the other computer run `ssh -L <port>:localhost:<port> <server>`, leave it open, browse to
  `http://localhost:<port>`.
* **Network install with a real host name**: `./crewboss install --mode network --domain crew.example.com --proxy bundled --yes`. The name
  must already point at the server and ports 80 and 443 must reach it. The installer writes `origin_hosts` and `public_url` for you.
* **Your own reverse proxy**: `--proxy existing` prints ready-made Caddy and nginx snippets (also saved in `<instance folder>/proxy/`);
  point them at `127.0.0.1:<port>`.
* **Non-standard ports**: `--https-port 8443` only changes the port the container binds on this machine; browsers still use
  `https://crew.example.com` (a router forwards 443 to it), which is what the installer assumes (`--public-port 443`). If browsers really
  use `https://crew.example.com:8443`, say so with `--public-port 8443`: it goes into `public_url` and `origin_hosts` as
  `crew.example.com:8443`. The same flag works with your own proxy: `./crewboss install --mode network --domain crew.example.com --proxy existing --public-port 8443`.
* Passkeys are tied to the name: keep it for good.

`tls_check` for LAN: the installer writes `tls_check: warn`. A LAN host is not checked against the system trust store (a self-signed
certificate is expected); instead the app does a handshake to the front door and compares the certificate's fingerprint with
`front_door.json`, warning when it has 60 days or less left. A public name keeps `enforce`. The app also shows a banner if it is
reached from a public address, and HSTS is never sent for LAN hosts.

Future options, not built in v1: DNS-01 validation (Caddy can prove ownership through your DNS provider, so no port needs opening;
needs a Caddy build with your provider's module and an API token), and a Crewboss-run certificate authority for LAN passkeys
(dropped for now: clients would have to install a root certificate).

## Environment variables

Settings that are not in `instance.yaml` are `CREWBOSS_*` environment variables; each is defined next to its default in `app/config.py`.
Some you may meet:

| Variable | Default | Meaning |
|---|---|---|
| `CREWBOSS_HOME` | `~/.crewboss` | The instance folder. |
| `CREWBOSS_WEB_AUTH` | `required` | Web sign-in. `off` is for tests only. |
| `CREWBOSS_WEB_STEPUP_HOURS` | `12` | How recent a fresh authenticator code (or passkey tap) must be for sensitive actions. A step-up needs a NEW code: the code just used to sign in is refused, so wait for the next one. |
| `CREWBOSS_RUNNER_BASE_URL` | the public URL | The address runners dial, if it differs. |
| `CREWBOSS_RUNNER_ALLOWED_HOSTS` | none | Extra hosts the runner socket accepts. |
| `CREWBOSS_JOB_LEASE_SECONDS` | `90` | How long a job's lease lasts without a heartbeat. |
| `CREWBOSS_JOB_MAX_SECONDS` | 4 hours | The longest a job may run. |
| `CREWBOSS_SSH_CERT_MINUTES` | `60` | How long a session SSH pass is valid. |
| `CREWBOSS_DEPLOY_POLL_SECONDS` | `5` | How often the deploy executor looks for approved deploys. `0` turns it off. |
| `CREWBOSS_GITHUB_TOKEN_FILE` | `/run/secrets/github-token` | The merge executor's token file. |

Mounts for other systems (a Zabbix token, a platform-key writer credential, a linked repository's `.git`) are in
`docker-compose.addons.example.yml`. Copy it and set `COMPOSE_FILE=docker-compose.yml:<your-file>` in `compose.env`, or put the extra
mounts in `compose.override.yml`.

## Upgrading

`trusted_proxies` now defaults to loopback only (it used to trust every private range). An install made before that change, whose
reverse proxy reaches the app from another address (the docker gateway, the bundled proxy's container address, a mesh peer), would
see every client as that one address, so everyone shares one sign-in rate-limit bucket. Crewboss tells you: it logs
`PROXY NOT TRUSTED` once per process and shows a "Proxy not trusted" banner naming the address. Add that address only if it is YOUR reverse proxy. If it is not,
investigate: a client may be reaching the app directly, and trusting it would let that client choose the address it appears as.

To fix it, run `./crewboss up`. For an install the installer made (its `instance.yaml` carries the installer marker), `up` works out
the address your proxy connects from, adds it to `trusted_proxies`, and restarts the app once, only if something was added. It only
ever adds: an address you put in that block yourself (one per line, indented two spaces like the others) is kept. The block has to be exactly what the
installer writes: its two comment lines, `trusted_proxies:` on the next line, then only entry lines. If it is anything else (a blank
line or a comment inside it, another key between the comment and the key, unindented `- x` lines, a `# comment` after an entry, a
list written as `[a, b]`), or you wrote `trusted_proxies` yourself without the installer's comment, or the file has no installer
marker, `up` does not edit it. Before it replaces `instance.yaml` it checks that the new file reads back as the same settings plus
the new list; if not it changes nothing and prints a warning. In every one of those cases add the proxy's address to
`trusted_proxies` by hand and run `./crewboss restart`.

Other changes in the same upgrade: the runner enrolment rate limit now counts clients by their real address (it believes
`X-Forwarded-For` only from `trusted_proxies`); acknowledging a decision made in chat needs an Admin or Owner of the project and is
recorded under that person; and turning on "AI sessions may open a shell" needs a fresh authenticator code within the last 5 minutes.
