# Runner WebSocket protocol (task #627, P3, P4)

`WS /runner/ws` is the one outbound socket a native runner holds to the dashboard. Implemented in `app/runner_ws.py`; this is
the contract the runner (P4) is written from. Version 1. All frames are UTF-8 JSON **text** frames of at most 4096 bytes (except an `agent_file` chunk, below: up to 7168) with a
string `type`. Binary frames close the socket. `b64` = standard base64 with padding.

## Connecting

    GET wss://<host>/runner/ws
    Authorization: Bearer mdr_...        (the DEVICE token from enrolment)
    Host: <host as the runner dialled it, e.g. dash.example.com:8443>

The server refuses the upgrade (HTTP 403, or a close before accept) when: the `Host` is not on the allowlist (the instance's MCP
allowlist, this dashboard's own host, `CREWBOSS_RUNNER_ALLOWED_HOSTS`); the request carries **any** `Origin` header (a runner must
never send one); the token is missing or is not a live device token (job and session tokens are refused; never put the token in
the URL). A refusal never says why. Do not retry a refusal in a tight loop: back off.

## Handshake: a mutual signed challenge

Keys: the server's runner identity key (ed25519; the runner pinned its public key / `fp8` at enrolment) and the device's own
ed25519 key (public half stored at enrolment, private half never leaves the PC).

    transcript(label) = label || client_nonce(32) || server_nonce(32) || device_id (8 bytes, big-endian)
                        || len(host) (2 bytes, big-endian) || host
    SERVER_LABEL = b"crewboss-runner-ws/v1/server\0"
    DEVICE_LABEL = b"crewboss-runner-ws/v1/device\0"
    # before the rename the labels began "mallen-runner-ws/v1/"; both sides still speak them to a peer that has not been updated

A runner that knows the crewboss labels adds `"labels":"crewboss"` to its hello. A server that sees it signs its proof under
`SERVER_LABEL`; a hello without it gets the old label. The runner accepts either server label and signs its own answer under the
matching device label; the server accepts a device proof under either.

`host` is the Host header value the runner sent, lower-cased, exactly as the WebSocket library writes it (default ports 80/443 are usually omitted, so sign what is actually on the wire, not what the URL says). The runner signs the name it dialled, so a proof made for a relay
at another name is useless against the real server. The labels stop one direction being reflected into the other.

1. runner to server, within 10 s of connecting: `{"type":"hello","client_nonce":"<b64 of 32 random bytes>"}`
2. server to runner: `{"type":"challenge","server_nonce":"<b64 32 bytes, fresh>","server_proof":"<b64 signature>","server_public_key":"<b64 raw 32>","fp8":"...","device_id":N}`
   The runner MUST verify `server_proof` = Ed25519(identity key, transcript(SERVER_LABEL)) using the key it **pinned**
   (not `server_public_key` from the frame, which is informational) and check `device_id` is its own. On failure it closes and does not answer.
   The runner also refuses every other frame (`ready`, `gemini_key`, any `*_result`, a nudge) until a challenge has verified on this connection: it closes the socket (1008) and logs a fixed line, never frame contents. A proof is required before it acts on anything the peer says, not only before it answers.
3. runner to server, within 10 s (`CREWBOSS_RUNNER_WS_PROOF_TIMEOUT_SECONDS`):
   `{"type":"proof","signature":"<b64 Ed25519(device key, transcript(DEVICE_LABEL))>"}`
   using the `client_nonce` from step 1 and the `server_nonce` from step 2.
4. server to runner: `{"type":"ready","v":1,"challenge_seconds":20}`, then zero or more `job_available` frames for jobs already
   queued for this device.

### Repeated challenge

Every `challenge_seconds` (`CREWBOSS_RUNNER_WS_CHALLENGE_SECONDS`, default 20) after the last good proof the server sends another
`challenge` (step 2, a fresh `server_nonce`, same `client_nonce`). The runner verifies the server proof and answers with a
`proof` as in step 3. A wrong signature, or no answer within the timeout, closes the socket. A device that is revoked, expired or
whose user is disabled also fails (and the server re-checks that every `CREWBOSS_RUNNER_WS_RECHECK_SECONDS`, default 5).

## Messages after `ready`

| Direction | Frame | Meaning |
|---|---|---|
| server to runner | `{"type":"job_available","job_id":N,"task_id":M}` | A job became claimable for this device (queued, approved, eligible; also a job requeued after another runner was lost). Ids only, never task text. The runner sends `claim` (below); the claim is the truth and may return no job (someone else took it, or it was withdrawn). A nudge is a hint: a runner should still send `claim` occasionally. |
| server to runner | `{"type":"challenge",...}` | as above |
| runner to server | `{"type":"proof",...}` | as above |
| server to runner | `{"type":"gemini_key","id":"<hex>","action":"set","key":"<the Google API key>"}` or `{"type":"gemini_key","id":"<hex>","action":"clear"}` | #861. A person pasted (or removed) a Google API key on the runner's Machines card. One way: the server sends it once to the VERIFIED socket of that device and does not store, log or queue it (an offline runner is an error on the page). The runner validates it (`[A-Za-z0-9_-]{20,200}`), writes `~/.config/crewboss-runner/gemini_api_key` atomically with mode 0600 (or deletes the file for `clear`), and never logs or echoes it. The key frame is the only frame that carries a key. |
| runner to server | `{"type":"gemini_key_result","id":"<same id>","ok":true,"fingerprint":"<first 8 hex of sha256(key)>"}` (or `"ok":false,"error":"<fixed text>"`) | The reply. The server checks the fingerprint against its own, keeps only the fingerprint, the key's last four characters and a time, and clears the file again on a mismatch. Never contains the key. An unknown or late `id` is ignored. |
| runner to server | `{"type":"claim","id":X}` | Ask for the next job this device may run (the one-SQL dispatch, unchanged). Reply `{"type":"claim_result","id":X,"ok":true,"job":null}` (nothing) or `"job":{job_id,task_id,project_id,account_id,fence,branch,job_token,lease_seconds,git_remote,gemini_allowed,max_seconds,profile{name,model,skills,allowed_tools,agent},mcp_servers[{name,url}],pins[{path,sha256}]}`. `profile.agent` ("" = none) is passed as `claude --agent <name>`; `pins` are the project Owner's reviewed agent / command / skill files (see "Agent, command and skill files" below); `mcp_servers` are the instance's extra MCP servers (instance.yaml `job_mcp_servers`, per project, empty unless set), written into the job's `--strict-mcp-config` next to `crewboss-job` and pre-approved as `mcp__<name>` (the job token is never sent to them). `gemini_allowed` (boolean, #861) is the project's "Gemini for jobs" switch: the runner adds its local Gemini tools only when it is true AND the runner has a usable key file. `max_seconds` (server `CREWBOSS_JOB_MAX_SECONDS`, default 14400) is the longest the runner may let the agent run: at that point it kills the agent, then makes one final heartbeat (the fence check). If that is refused (stale fence) it pushes nothing and drops the job; otherwise it pushes the commits the agent already made, flags agent files for review as at a normal end, and sends `finish` with `ok:false`. The job token is in this one reply only. Ids only, never task text. |
| runner to server | `{"type":"heartbeat","id":X,"job_id":N,"fence":F}` | Renew the lease. Reply `{"type":"heartbeat_result","id":X,"ok":true,"lease_seconds":90,"finish":null,"keep_open":false}`, or `{"ok":false,"status":409,"detail":...}` when the fence is stale / the job is not running on this device. On 409 the runner kills the agent, pushes nothing and drops the job. `finish` is `null` until the agent calls the job MCP's `job_finish` (then `"done"` or `"blocked"`; the job is "finishing": still `running`, with the finish mark set). Unless `keep_open` is true, the runner then stops the session by signal only (it never types into the pty: an Enter could approve a permission dialog): SIGTERM to the process group, SIGKILL after `CREWBOSS_RUNNER_FINISH_GRACE` seconds (default 20), then it runs the final heartbeat and the push, and sends `finish` with `ok:true` (and `outcome:"blocked"` when blocked): a stop the RUNNER caused after `job_finish` (exit -15/143/-9/137) is never a failure; a non-zero exit the agent produced itself keeps its code. The mark is tied to the attempt's fence and cleared on requeue. |
| runner to server | `{"type":"finish","id":X,"job_id":N,"fence":F,"ok":true,"summary":"..."}` | End the job after the branch is pushed (`ok` false = failed). Same fence check; reply `finish_result` with `state` or the 409. Locks go with the job. |
| runner to server | `{"type":"release","id":X,"job_id":N,"fence":F,"reason":"...","code":"agent_files_unreviewed"}` | Give the job back because the runner could not START it (no `claude`, remote unreachable, no git credentials). The optional `code` (#987) is the **hold code**: `agent_files_unreviewed` says the only block was agent, command or skill files the Owner has not reviewed (a 0.1.0 runner sends no code; the server recognises its `config.unreviewed` reason). If the Owner has something pending to review, the job is **held** (`hold_reason` `agent_files`): requeued with a new fence but not claimable, no attempt counted, not a failure, shown as "waiting on you"; pinning the files releases it, and a hold older than 7 days fails. With nothing to review it is an ordinary counted release, and the reply carries `hold` only when held. The server treats it like a lapsed lease (`end_lease`): requeued with a NEW fence and no token, `attempts` counted, failed for a human after `CREWBOSS_JOB_MAX_ATTEMPTS`. Reply `release_result` with `state` (`queued` or `failed`) or the 409. The runner then stops claiming for a growing delay (5 s doubling to 5 min). |
| runner to server | `{"type":"skills","id":X,"job_id":N,"fence":F,"written":["build-an-app","rules"],"skipped":[]}` | Task #718. Sent once after the runner wrote the job's skills into its worktree, and ONLY when the claim carried a `skills` bundle (an older server sends none, so it never sees this frame). `claim_result.job.skills` = `{skills:[{key,description,layer,n,version_id,content_hash,skill_md,files:[{path,b64}],warnings}],rules:{text,count,locked}\|null,index:[{key,description}],held_back:[keys],set_hash}`: the project's combined skills (base skills already composed into one text) and ONE rules file, locked rules first. The runner writes `.claude/skills/<key>/` and `.claude/rules/crewboss-rules.md`, never over something the repo already has, and keeps them out of a commit with a per-worktree exclude file. `written` / `skipped` move the job's `job_asset_uses` rows from `resolved` to `delivered` / `missing`. Reply `skills_result` or the 409. |
| runner to server | `{"type":"agent_file","id":X,"job_id":N,"fence":F,"path":"...","sha256":"<hex>","source":"preflight"\|"job","part":0,"parts":3,"b64":"<chunk>"}` | #829. One agent, command, skill or settings file the project Owner has not reviewed (`preflight`: it blocked the job) or that the agent created or changed (`job`). The file (UTF-8 text up to 96 KB) goes in **chunks**: the runner slices it into pieces of at most 4096 bytes, one frame each, each piece SIZED FROM THE FRAME BUDGET (`worker.chunk_size`: 7168 minus the serialised length of the other fields, so a long CJK or emoji path, which JSON escapes to 6 or 12 bytes a character, shrinks the piece; a path that leaves no room is not sent; no frame is ever over 7168 bytes: uvicorn runs with `--ws-max-size 8192` and closes the socket on a larger frame, so the old single 160 KB frame could never arrive), `part` counts from 0, `parts` is the total (at most 100), `b64` is the piece; `path`, `sha256`, `source`, `job_id` and `fence` are repeated unchanged on every chunk. Each chunk is answered before the next is sent: `{"type":"agent_file_result","id":X,"ok":true,"part":i}` for a chunk that is not the last. The server keeps ONE reassembly per verified connection (never more than 96 KB, in memory only, dropped on any error or on a new `part` 0) and checks the whole file's `sha256` when the last chunk arrives; only then is the pending row written and the reply is the usual `{"ok":true}`. A wrong chunk (out of order, another file, too large, bad base64, a hash that does not match, bytes that are not reviewable text) is answered `{"ok":false,"status":400,"detail":"<plain reason>"}` and the socket stays open: the runner stops sending and puts the reason on the job (a preflight job is RELEASED with `could not send N of M file(s): the dashboard said ...`; the end-of-job report is non-fatal, see below). A file that cannot be reviewed (too big, not text) or a link is named only: one frame with no `b64` or `part`. Only a verified socket may send a frame over 4096 bytes, and only of this type. Sent only when the claim carried `pins`. |
| runner to server | `{"type":"job_activity","id":X,"job_id":N,"fence":F,"seq":S,"frames":[{"seq":S,"speaker":"claude","kind":"assistant_text","text":"...","meta":{}}]}` | #89, the live runner conversation (watch only). A batch (about every 0.5 s) of what the agent said and did, read by the runner from Claude Code's own session transcript (the runner names the session with `--session-id`, so the file `<claude config>/projects/<cwd slug>/<id>.jsonl` is known) and from its local Gemini tool's log. `seq` (top level) is the first frame's number; each frame has the runner's own `seq` (from 0, per claim). `speaker`: `claude` \| `gemini` \| `runner`. `kind`: `assistant_text`, `tool_call` (text `Read app/x.py` / `Edit app/x.py (+12 \u22123)` / `$ <command, 200 chars>`; meta `tool`, `path`), `tool_result` (the first 500 characters; meta `tool`, `status` ok\|error), `permission_request`, `status` (meta `state`: working\|idle\|unavailable, and `session_url` = the claude.ai/code link), `note`. For `speaker:"gemini"`, `tool_call` is what Claude asked Gemini and `assistant_text` is its reply. The runner scrubs every text with `redact`, cuts it (2000 characters), caps a job at 2 MB of text (then one "truncated" note) and sizes each message under 7168 bytes as serialised (JSON escapes a non-ASCII character to 6 bytes): like `agent_file`, this is the only other type allowed over 4096 bytes. The server checks the fence like a heartbeat (`jobs._running`: the device must hold the job, running, at exactly this fence), scrubs and caps each frame again (known speakers/kinds, 2048 characters, a few short meta keys, a `session_url` only on https://claude.ai/code/), keeps at most 4000 rows / 2 MB of text per job and 30 days, drops frames whose `seq` it already has (a re-send), and replies `job_activity_result` `{ok:true,accepted,dropped}` or `{ok:false,status:409}` (stale fence: the runner stops sending for this job) or 400. It never renews the lease. If the transcript cannot be found or read, the runner sends ONE status frame `live view unavailable for this run` and the job carries on. |
| runner to server | `{"type":"attention","kinds":["not_signed_in"]}` | No reply. The runner's preflight problems as FIXED kinds (`not_signed_in`, `claude_missing`, `git_missing`, `elevated` (Windows: the runner holds an administrator token and claims nothing)), sent once per socket session when there is a problem and again whenever the set changes (an empty list clears it; never sent before a problem). The server keeps only known kinds, in memory, and the Runners page shows its own fixed wording as "needs attention" next to the online chip, never text from the PC. An old server closes the socket on this frame (4400): deploy the dashboard first. |
| server to runner | `{"type":"ca_available"}` | A CA chat turn is waiting for this device's user (task #702). No ids. The runner sends `ca_claim`; the claim is the truth. Also sent right after `ready` if a turn was queued before the socket connected. |
| runner to server | `{"type":"ca_claim","id":X}` | Ask for the next chat turn of this device's user and account (one SQL, like `claim`). Reply `ca_claim_result` with `"turn":null` or `{turn_id,conversation_id,fence,turn_token,model,system,user_text,replay,session_id,lease_seconds,max_seconds}`. `turn_token` (`cbt_...`) is the MCP credential for that turn alone (only its hash is stored; it dies when the turn ends) and goes to Claude only through a 0600 `--mcp-config` file; the MCP URL is `<server>/runner/ca-mcp`. `session_id` is the Claude session of the last turn ON THIS DEVICE (else null: use `replay`). A runner runs this loop beside its job loop, so a long job never blocks the assistant. |
| runner to server | `{"type":"ca_event","turn_id":N,"fence":F,"kind":"delta","text":"..."}` | Stream the answer (`kind` delta, at most about 600 characters a frame), name a tool that began (`kind` tool, `name`), or keep the lease alive (`kind` hb, every 20 s). No reply, EXCEPT that a turn the runner no longer holds (stopped by the person, failed, finished) is answered with `{"type":"ca_stop","turn_id":N}`: the runner kills Claude and sends nothing more. |
| runner to server | `{"type":"ca_finish","id":X,"turn_id":N,"fence":F,"ok":true,"session_id":"...","tokens_in":n,"tokens_out":n,"tools":["review_tasks"],"error":""}` | End the turn. The answer text is the server's own copy of the deltas (it never rides one frame). Reply `ca_finish_result` or the 409. |
| server to runner | `{"type":"env_available"}` | An environment check is waiting for this device (#810: the person switched a project's "Runs on" to it, or pressed Check again). No ids; the runner sends `env_claim`. Also sent right after `ready` if a check was queued while it was offline. |
| runner to server | `{"type":"env_claim","id":X}` | Ask for the next environment check of this device. Reply `env_claim_result` with `"check":null` or `{check_id, project_id, rows:[{i, kind, name, os, args}]}` (`i` is the requirement's own id; the runner echoes it in its results). A row is ONLY (kind, name, args): the runner maps `kind` (`tool`, `env`, `android_sdk_pkg`, `file`, `keystore`) to its own built-in probe, never executes a string from the server, validates every name against a safe pattern before looking it up, and answers an unknown kind, or a `tool` outside its fixed list of known tools, `unsupported`; it never looks at a network or device path (two leading slashes of either kind), and on Windows only at `~` or `X:\` paths. `args` holds at most `min_version` and `fingerprint` (a keystore's public SHA-256 certificate fingerprint; the key itself is never read for this or sent). No Claude agent is involved. |
| runner to server | `{"type":"env_finish","id":X,"check_id":N,"results":[{"i":0,"s":"ok","v":"17.0.9"}],"os":"linux"}` | One short status per row (`s`: ok, missing, old, mismatch, error, unsupported, na; `v` up to 40 characters, a found version), and the runner's OS (linux, windows, macos). The server alone decides compliance and picks the install hints; an invented status or a missing row is an `error`. Reply `env_finish_result` `{result, missing}` or a 404 for a check that is not this device's or already finished. A check claimed and never finished is offered again after 2 minutes. |
| runner to server | `{"type":"ping"}` | optional app-level ping; the server answers `{"type":"pong"}` |

`id` is any short string or integer the runner picks; it is echoed. These (and `attention`) are accepted ONLY on a socket that has passed its first proof (before that they close it with 4400).

Any other frame, an answer when no challenge is pending, or a malformed frame closes the socket with 4400.

## Close codes

| Code | Meaning | Runner action |
|---|---|---|
| 4401 | Not authorised: failed challenge, or the device was revoked / expired / its user disabled (a revoke closes within 1 s) | Stop. Revoked means re-enrol; do not reconnect with the same token. |
| 4408 | Timeout: no `hello`, or a challenge went unanswered | Reconnect with backoff |
| 4400 | Protocol error | Reconnect with backoff; fix the client |
| 4409 | Replaced: a newer socket of the same device proved the device key (or, for an unproven socket, a newer unproven one arrived) | Reconnect only after a LONG backoff (minutes), never in a loop: two runners fighting over one device would flap forever. One verified socket per device; a connection that has not completed the first proof never displaces it. |
| 1011 | Server error | Reconnect with backoff |

## Why claim and heartbeat are socket-only (review comment 2280)

`POST /runner/jobs/claim` and `/runner/jobs/<id>/heartbeat` used to take the `mdr_` device bearer token alone, so a stolen token
could take and hold jobs without the device key. They now answer **403 to every caller**. The device key proves itself on the
socket (the mutual challenge, repeated every 20 s), so only a process holding that key can claim. REST with a per-request
device signature over a server nonce was considered and rejected: it would add a second proof path to keep correct for no
gain, since the runner holds the socket anyway. The jobs.* rules (one-SQL dispatch, fence on every write) are unchanged.

## TLS pinning and rotation (a LAN install, #976)

A LAN install is served over HTTPS with a **self-signed** certificate, so there is no certificate authority to vouch for it. The enrolment code carries a pin instead.

**Code format.** `host:port#<fp8>.<pin26>-<secret>`, made only when the runner's address is a LAN one. `pin26` is the first 26 characters of lower-case unpadded base32 of sha256 of the certificate's SubjectPublicKeyInfo (130 bits). Base32 has no `.`, `-` or `#`. An address with a Let's Encrypt certificate keeps the old `host:port#<fp8>-<secret>`, which still works both ways; an old runner given a pinned code fails closed (its fingerprint is not 8 characters) and the new runner's error tells the person to update.

**At enrolment** the runner handshakes with certificate checking off, reads the peer certificate and compares its pin with the code's in constant time. **Nothing is sent** (no `GET /runner/identity`, no secret) until they match. It then stores the PEM in the profile's `config.json` (`tls_certs`, `tls_pin`) and in `<state dir>/server-cert.pem`.

**Afterwards** every connection (enrolment requests and the WebSocket) uses an SSL context whose only trust anchors are the stored certificates, with the host name checked (the code's host is a name in the certificate). The device token travels only on a handshake that has already verified. The system store is not consulted for a pinned profile. A profile with no pin (enrolled against a public address, or before LAN mode) uses the system store, unchanged.

**A same-key renewal needs no announcement.** Before each connection a pinned runner handshakes with checking off, sends nothing, and requires (i) sha256(SPKI) equal to the stored pin (constant time) and (ii) the certificate's subject alternative names to contain the host it dials; a certificate that passes is added to the stored ones (so the certificate file agents trust is refreshed too) and the connection then verifies as usual. The front door service reuses the key, so a renewal normally just works; `tls_rotate` is for a real key change. The sidecar only PREPARES the new certificate (60 days out); it never switches by itself.

**Agents.** Claude Code gets `NODE_EXTRA_CA_CERTS=<state dir>/server-cert.pem` (it adds to Node's store, so the Anthropic API still verifies), set by `spawn.agent_env`, so the chat assistant gets it too (if the user already set `NODE_EXTRA_CA_CERTS`, the variable points at a combined file: their file plus the pinned certificate). The Gemini MCP gets `CB_SERVER_CA_FILE`, used only for its connection to the dashboard; `SSL_CERT_FILE` is never set, because it would break the Google call. Both only when the profile is pinned.

**Rotation and renewal** use one frame, sent only on a verified socket (after the mutual ed25519 challenge, over TLS pinned to the old certificate). A frame received before the server's proof verified closes the socket, like every other frame.

```
server -> {"type":"tls_rotate","id":"<hex>","cert_pem":"<new>","sig":"<b64>"}
runner -> {"type":"tls_rotate_result","id":"<hex>","ok":true,"fp":"<pin26 of the new certificate>"}
```

`sig` is the Ed25519 signature, by the server's **identity key** (not the TLS key, so a stolen TLS key cannot push a pin), over

```
b"crewboss-runner-tls-rotate/v1\0" || sha256(new SPKI) || sha256(new cert DER) || sha256(old SPKI) || len(host) as 2 bytes big-endian || host
```

where `host` is the lower-case Host the runner dialled and "old SPKI" is the certificate the socket is on. The label is private to this purpose, so a signature made for the challenge cannot be replayed as a rotation. The runner verifies it against the key **pinned at enrolment**, checks that the new certificate names the host it dialled, keeps **both** certificates as anchors and answers. A bad signature, a wrong label or a certificate for another host changes nothing (`ok: false`). The server keeps the acknowledged pin in `device_tokens.tls_fp` (a runner acknowledges only the pin the server sent in that frame). When a session becomes ready on the newest certificate the runner drops the older ones. A runner that was offline for the whole rotation must be enrolled again.

**Driven from the Front door card.** *Get computers ready* asks the front door service to prepare the new certificate (`POST /lan-cert/prepare`), announces it with the frame above on every verified socket (`runner_ws.rotate_tls_from_prepare`, which first checks the service's hashes against the certificate) and shows "N of M computers that run agents are ready" (`runner_ws.tls_ready(pin)`: devices whose `tls_fp` is the new pin, of all live devices). Only after that does the card offer *Switch now* (`POST /lan-cert/switch`). The service reuses the key, so the pin normally does not change.

## Liveness and leases

A verified socket tells the server the runner process is alive (it updates the device's `last_seen`). It does **not** renew
job leases: each running job must still be renewed with a `heartbeat` message (`job_id`, `fence`), which the broker checks
against the job's fence. A socket that is up while the agent has stopped heartbeating loses the job exactly as before. When the
socket drops, there is no REST fallback, so heartbeat as soon as the socket is back; the lease (default 90 s) is the only authority on whether the
job is still yours, and a 409 means it is not.

If a runner disappears mid-job (the PC sleeps, the network drops), nothing happens until the lease lapses; then the server's sweeper
(`LeaseSweeper.run`, every `CREWBOSS_JOB_SWEEP_SECONDS`, 15 s) calls `end_lease`: the job is **requeued with a new fence** (offered
to a runner again), or **failed** if it has already been tried `CREWBOSS_JOB_MAX_ATTEMPTS` (3) times or held a deploy/migrate lock. A
runner that wakes later gets 409 on its next heartbeat, kills the agent and pushes nothing. If the SERVER sees a clock jump (a
sleeping server host) it grants running jobs one fresh lease instead. The runner holds a sleep inhibitor while a job runs
(docs/runners.md, "Sleep").

## Limits

One app process: the server keeps the registry of live sockets in memory, so a nudge reaches only sockets of the process that
queued the job. (A second app process would need a shared bus; not built.) The signed challenge authenticates the endpoints; it
does not replace TLS, which protects the socket's contents on the wire.

## Spawning Claude Code (P4 spike, `claude` 2.1.314)

Read from `claude --help` and `claude remote-control --help` on the dashboard host; no session was started and no tokens spent.

| | API runner (`device.mode = api`) | Subscription runner |
|---|---|---|
| Launch | `claude -p <prompt> --model M --mcp-config F --strict-mcp-config --permission-mode dontAsk --allowedTools=mcp__crewboss-job,<profile tools> --disallowedTools=<dangerous tools the ceiling does not name> --no-session-persistence --output-format text` | `claude --remote-control dash-j<id> --model M --mcp-config F --strict-mcp-config --permission-mode default --allowedTools=mcp__crewboss-job,... --disallowedTools=<dangerous tools the ceiling does not name> <prompt>` |
| Process | headless, no TTY, exits with its answer | interactive, so it runs under a pseudo-terminal the runner drains to `agent.log`; ends when the person ends it |
| Who answers permission prompts | nobody: `dontAsk` allows only the listed tools | the person (the session starts in the explicit `default` mode: without the flag Claude Code 2.1.330 started in AUTO mode and ran a `Write` outside `--allowedTools` with no prompt, verified against a mock API; a project `defaultMode` does not outrank the flag), from claude.ai/code or the phone (Remote Control), within a grace; else the runner gives the job back (below) |
| Auth | the PC's own `ANTHROPIC_API_KEY`, kept in the child's environment | the PC's `claude` login. `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed from the child's environment |

**Permission prompts (#829).** A tool outside `--allowedTools` raises Claude Code's permission dialog ("Do you want to proceed?" with the numbered choices "1. Yes ... N. No"); in a subscription
session nobody answers it and the job would hang to its time limit. `guard.watch_permission` reads the end of `agent.log`: when a
permission dialog is the last thing on screen (the question followed to the end of the screen by its numbered choices; the tool name is read only from inside the dialog, so an agent's prose containing the same question is not a prompt; a `Bash command` / `Edit file` / `Create file` / `Read file` / `Web fetch` dialog has no `Tool(` call in it, so its header names Bash / Edit / Write / Read / WebFetch, and only a dialog with no readable header says "an unlisted tool") and the log has not changed for `CREWBOSS_RUNNER_PERMISSION_GRACE` seconds (default 90,
long enough for a person on the phone to answer), the runner stops the agent and **releases** the job (requeued, attempts counted) with the
reason `blocked on the permission prompt: Claude Code on <PC> asked to use <tool>, which this job's profile does not allow ...`, shown on
the job. It never types into the session and never approves anything (the only pty write remains the folder-trust answer). Fix: add the tool to the
profile's allowed tools (`Skill` is in builder, designer, db-migrator and code-auditor). Unpushed work in that worktree is discarded with it.
A `keep_open` job that has already called `job_finish` is never released by this watcher: the finished work would be lost with the worktree.
The runner stops the session and ends through the normal push and finish path, as it does when such a session hits the maximum duration.

**Agent, command and skill files (#829): deny by default, with owner-reviewed pins.** `--agent <name>` makes Claude Code load that agent's
file, a pre-approved `Skill` lets a skill's `allowed-tools` pre-approve tools, and legacy `.claude/commands/**` files are loaded and invoked
the same way. All of it comes from the repository and nothing about reading it is safe to decide by parsing (Claude follows symlinks, loads
subfolders and nested `<dir>/.claude/` folders, decodes YAML escapes, duplicate keys and other line breaks differently from any hand-rolled reader).
So **no security decision rests on parsing**. Before a job starts the runner (`agentscan.scan_worktree`) does this:

1. **Find.** Walk the WHOLE worktree for any `.claude` directory at any depth (root and nested, any letter case); inside each look at
   `agents/`, `commands/` and `skills/` (all recursive), plus `.crewboss/skills/**` at the root. `settings.json` / `settings.local.json` of every
   `.claude` (any letter case) are PINNED like the agent files and also get the hard checks below. Folder and file names `.claude` and `.crewboss` are compared
   case-insensitively everywhere (on NTFS and APFS `.CREWBOSS/Skills` is `.crewboss/skills`).
2. **Symlinks.** Any symlink or junction under those paths, a symlinked `.claude` / `.crewboss`, or a folder link anywhere that leaves the repository,
   BLOCKS the job (`config.symlink`). A link is never pinnable.
3. **Pins.** Every regular file found gets its sha256. The job may start only if every (repo path, sha256) is in the project's **pins** (the claim's
   `pins`: `[{path, sha256}]`, kept server-side). An unknown or changed file BLOCKS the job (`config.unreviewed`; the release reason names the files and
   says the Owner pins them on the Machines page). More than 200 such files, a file over 96 KB, a file that is not UTF-8 text, or a file name with
   unusual characters also block (`config.unreviewed` / `config.unreadable`): fail closed, no silent cap. A claim without `pins` (an older dashboard) pins nothing.
4. **Warning layer.** The old frontmatter reader (`hooks`, `mcpServers`, `permissionMode`, `tools` / `allowed-tools` outside the job's `--allowedTools`, odd line
   breaks) still runs, but only produces `warn` findings (`frontmatter.*`): extra reasons shown with a block and in `doctor`. It never approves or blocks
   anything on its own; a pinned file that declares hooks starts.

**The repository's settings files (`.claude/settings.json`, `settings.local.json`).** Claude Code loads these whatever the job's argv says. Verified on Claude Code
2.1.330 in a scratch repo: `--setting-sources user` keeps them out (no hook runs) but ALSO drops the repository's agents and skills (the `project` source
carries all three), so pinned `--agent` files would stop working; **it is therefore NOT used**. What a job does instead, in both modes:
(0) `--disallowedTools=` names every one of Bash, Write, Edit, NotebookEdit, WebFetch, WebSearch, SendMessage, ListAgents, Workflow, EnterPlanMode, ExitPlanMode and AskUserQuestion that the job's ceiling does not name (`MultiEdit` is not a tool of 2.1.330 and is not listed; no seeded profile names any of the last six). SendMessage and ListAgents reach the OTHER Claude sessions on the PC (in subscription mode the account's remote ones too) and would inject text into them; the Workflow, plan-mode and AskUserQuestion dialogs are not tool permission prompts, so a subscription job at one would wait up to the time limit (a job asks a person through `job_finish outcome='blocked'`, never AskUserQuestion; `guard.permission_prompt_tool` also recognises those three dialogs as a second layer). A ceiling holds BARE tool names only: the server refuses a profile whose `allowed_tools` has a pattern such as `Bash(git *)` (it does not deny Bash, so a user-level allow widens it, and a git alias escapes it): `create_job` raises, and a claim of such a job fails it with that reason (`jobs.tool_pattern_problem`); the runner drops any pattern too. Migrations 0038, 0040 and 0109 seed bare names only. a deny beats the runner user's OWN `permissions.allow` in `~/.claude/settings.json`, which otherwise let such a tool run silently even under `dontAsk` (verified on 2.1.330). Such a tool is absent from the session, so a person on the phone cannot approve it either. (a) `--settings '{"disableAllHooks":true,"crossSessionInbound":"refuse"}'` is on every job's argv (`crossSessionInbound` is a key of 2.1.330's settings schema, values accept | hold | refuse: `refuse` opts the session out of messages other sessions send it) (it outranks the project file; verified: a project SessionStart hook does not run, agents and
skills still load). A job therefore loses ALL hooks, the repository's and the user's; that is intended. (b) The job is **blocked, with no pin that can lift it**
(`agentscan._scan_project_settings`), when a project settings file has any of: `hooks` (`settings.hooks`); any key at any nesting level whose lower-cased name contains `plugin` or `marketplace` (`enabledPlugins`, `extraKnownMarketplaces`, its alias `additionalMarketplaces`, `strictKnownMarketplaces`, `allowedMarketplaces`, `pluginDirs`; an empty value sets nothing)
(`settings.plugins`: a plugin is repository code with its own hooks, agents, commands and skills outside `.claude`); `permissions.allow` with any entry outside
the job's `--allowedTools` ceiling (`settings.allow`: a trusted folder would otherwise pre-approve more); any `env` key (`settings.env`); or a command-running or
credential key (`settings.exec`: `apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`, `gcpAuthRefresh`, `otelHeadersHelper`, `statusLine`,
`subagentStatusLine`, `fileSuggestion`, `processWrapper`, `policyHelpers`, `proxyAuthHelper`, `sandbox`, `lspServers`, `mcpServers`, `defaultShell`; the list comes
from the binary's own list of command-carrying settings). (c) The file is also in the inventory, so it needs the Owner's pin like an agent file
(`config.unreviewed`) and a change to it later is flagged. What a job loses from a repository, on purpose: its own hooks, its permission pre-approvals and its plugins.
Its agents, commands and skills (pinned) still work. The plugin / marketplace key search (`agentscan._plugin_keys`) always checks the top-level keys; the nested search is capped at 20000 nodes (and 20 keys found), so a settings file nested deeper or wider than that is not searched past the cap. (`permissions.allow` is judged against the ceiling only when the runner knows the job; `doctor` has none.)

**CA chat turns and looks (#930)** (`ca.argv_for`, `ca.look_argv`) use the same two closures: `--settings` is `spawn.JOB_SETTINGS` (hooks off, `crossSessionInbound=refuse`) and `--disallowedTools=` is `spawn.deny_unnamed([mcp__crewboss-ca])`, i.e. every `DANGEROUS_TOOLS` entry (the CA ceiling names none), so a `permissions.allow` in the runner user's own `~/.claude/settings.json` cannot make a built-in run silently. The CA keeps `--tools ""`, `--allowedTools=mcp__crewboss-ca`, `--permission-mode dontAsk` and `--restricted`; it needs no hook. `--restricted` already ignores the user, project and local settings files (2.1.331 `--help`), so a user-level `permissions.allow` cannot reach a CA or look session; the deny list is the second layer and is what stops an allow coming from managed settings (`managed-settings.json`), which `--restricted` does not ignore. Both are needed; neither is redundant. A look also gets `Task,Agent` denied on top. Checked on Claude Code 2.1.331: both argv shapes start and answer together.

**Owner review (how a file gets pinned).** When a job is blocked on unpinned files the runner sends each one in an `agent_file` frame (below) before it
releases the job. The dashboard stores the bytes as `pending` (table `project_agent_files`, migration `0109a`, whose CHECK ties `sha256` to the stored bytes).
The project Owner sees ONE review card per project on the **Machines** page (Runners tab, "Agent files to review"): each file's text, read-only, and what it
declares (tools, hooks, MCP servers, permission mode; shown by a real YAML reader for convenience, never used to approve), then **Pin all N after reading**,
which needs a fresh factor (`require_fresh`, the same re-confirmation as an approval) and pins exactly the files listed. The next claim carries the pins and
the job runs. Change a file and its hash is new, so it needs pinning again; a pin can be removed (Unpin) from the same page. **ERP example:** acme-erp
commits `.claude/agents/{erp-builder,db-migrator,code-auditor}.md`; the first job on it is released with `config.unreviewed`, the Owner reads and pins the three
files once, and every later job on that repository runs them until one of them changes.

**What the runner trusts without a pin:** the skills the dashboard itself delivers (`claim.skills`, already reviewed as build skills; `skills.install` returns
the sha256 of every file it wrote and the second look trusts exactly those bytes). A copy of the repo's own `.crewboss/skills/**` is trusted only through the pin of its
SOURCE path (`skills.install` returns `sources` {copied file: source file}; `skills.trusted_files` accepts a copy only when `(source, sha256 of the copied bytes)` is
pinned, never because the copy hashes to itself), so a skill the first scan never saw stays unreviewed. The scan therefore runs twice: on the fresh worktree (the pins) and again after `skills.install` (pins plus what install wrote; anything else
under `.claude/{agents,commands,skills}` blocks).

**Files the agent writes during a job.** After `skills.install` the runner records a baseline of every file under those paths. When the job ends (before the
push) it looks again: anything created or changed (or any link made) is sent as an `agent_file` frame with `source: "job"`, and the job record is flagged
(`jobs.review_paths`, shown on the Machines page and as a task comment). The branch IS pushed (the work is kept) and the job summary starts with
`NEEDS OWNER REVIEW: ...`; the files wait on the review card, and no later job runs them until they are pinned. **This report is non-fatal:** it is sent after the
final heartbeat and before the push and `finish`, any failure (a dead socket, a 400, an exception) is caught and only noted in the summary
(`could not send N file(s) for review`), so the push and the `finish` always follow. A failed PREFLIGHT report is different: the job is released at once with the reason
`could not send ... to the dashboard for review; ...` rather than leaving the lease to lapse.

Why not `claude remote-control` (the server subcommand) for subscription jobs: it accepts `--spawn`, `--name`, `--permission-mode`
but no `--mcp-config` or prompt, and the sessions it spawns read the project's own config. Giving them the job MCP would mean a
`.mcp.json` holding the job token inside the worktree, which is forbidden. `--remote-control` on a normal session takes every
flag a session takes, so the config file can live outside the worktree. Open item for the 2-PC test (step 4): confirm on a real
subscription login that a session started this way shows up on the phone and accepts the first prompt unattended.

Both modes: the child's environment loses every `CREWBOSS_*` variable (and the pre-rename `MALLEN_DASH*` and `MALLEN_RUNNER_*`, which are still read); `GIT_ALLOW_PROTOCOL` is
`https:ssh` (`file:` is added only when `CREWBOSS_RUNNER_ALLOW_FILE=1`, which the test suite sets). The prompt tells the agent to read the task with `job_brief`, commit to the branch, NOT push and NOT call
`job_done`: there is no `job_done` tool any more (an agent reads untrusted task text, so it must not be able to end its own job);
a device job ends only through the runner's `finish`. The agent's tools are named one by one
(`mcp__crewboss-job__job_brief`, `job_comment`, `job_tick_subtask`, `acquire_lock`, `release_lock`, `request_deploy`, `record_preview`), not the whole server. `record_preview` reports a green runner build + test + local preview of an exact commit; it is what lets a production-only project (no dev environment) promote that commit (change #77). The
runner pushes after its final heartbeat succeeds and then sends `finish`. The prompt tells the agent to call `request_deploy` only for a commit already on the target's deploy branch (its own job branch is not); otherwise it says in `job_comment` that the work needs merging, and a person requests the deploy after the merge. On a project that opted in to the merge executor (`docs/multi-user.md`: the repository is enrolled by the instance owner, the bot credential is a 0600 file on the server whose identity is re-checked before each write, approvals expire after 24 hours and approvals from before the opt-in are sent back), Crewboss itself fast-forwards the default branch to the approved commit once the gates are recorded; the job's `report_merge` then only reads back the state, and a job never needs push rights to main.

**Hard rule (k98):** the runner never sends the PC's Claude login, subscription token or API key to the dashboard. Its only
secrets on the wire are the device token (header, WS upgrade) and signatures; the server has no field that accepts anything else.

### Job feedback, finish details and the summarise turn (0.1.1)

* **`job_finish` (the agent's MCP tool) takes `outcome`, `summary`, and optionally `tests` (the checks it ran, one line), `next_steps` (a short list of what a person should do next) and `remaining_work` (what is NOT done; empty when complete).** The server trims and caps each list (`jobs._clean_list`), stores them on the job and shows them on the task's outcome card. An agent that stops without them still gets a card from its summary. A CA reading "needs more work" offers Run again.
* **`base_branch`.** The `claim_result.job` carries `base_branch` (empty, or an earlier job's pushed `dash/j<id>-e<fence>` branch). Run again, a review run and a tests run start FROM that branch; empty, or a branch that is gone, means the repository's default branch. The server accepts only that branch-name shape.
* **`job_activity`** frames (above) feed the live runner view and the task timeline; tool results are shown only to people who can edit the task.
* **The summarise turn.** The task summary (What / Done / Left) is written by a CA turn with `kind:"summarise"`: it arrives through `ca_claim` like a chat turn but has no conversation, no turn token and no tools. The runner runs `claude -p` with `--tools ""` and the cheap model, reading only the scrubbed task facts in `input` (untrusted text), and the answer is stored as the task's AI summary. **Whose runner, what it costs, who triggers it.** It runs on the runner of the person whose view queued it (the project's allocated runner when it has any), never on anyone else's. The cost is one `claude -p` on the cheap model (haiku by default) per task refresh, at most one per person a minute and one per task every two minutes. Only a person who can **edit** the task triggers it (a view-only person gets the auto summary), and the server re-checks that right when the answer comes back before storing it as "Written by the AI". Each person can switch it off with **Use my runner for task summaries** under the Summary (on by default; off means the auto summary only, nothing queued on their runner). If no runner is online, the setting is off, or the turn fails, the task gets an extractive "Auto summary" instead; nothing is said in any chat.


## The runner program (`runner/`, `crewboss-runner`)

`pipx install "git+https://github.com/crewboss-dev/crewboss.git#subdirectory=runner"` (or `pipx install ./runner` from a clone), then `crewboss-runner enrol <host:port#fp8-secret>`, `crewboss-runner run`, `status`, `forget`.

* Enrolment fetches `/runner/identity`, computes the fingerprint itself and aborts unless it equals the code's, before the secret
  or the device public key is sent. The device ed25519 key and the device token live in the OS keyring; with no keyring backend
  they go in `~/.config/crewboss-runner/secrets.json` (0600) and `status` says so.
* `run` reconnects with backoff (1 s doubling to 60 s), waits 5 minutes after 4409, stops on 4401, answers every re-challenge,
  and does not answer a server proof that fails against the pinned key (it closes and waits 5 minutes).
* Preflight before every claim: `claude` and `git` must be found, and in subscription mode Claude Code must be signed in
  (`claude auth status`: no TTY, 20 s timeout, `ANTHROPIC_API_KEY` stripped, output discarded; only the exit code is used, 0 = signed
  in, 1 = not, anything else or a timeout = undecided and never blocks; a signed-in result is cached for 60 s, a not-signed-in one
  is re-checked every idle poll so it clears once the person logs in; nothing from the command is read, logged or sent).
  The problems also go to the dashboard as an `attention` frame.
  If any check fails, the runner claims nothing, keeps retrying, and reports it
  in `status` and on the local page. A job that fails to START afterwards (unreachable remote, no credentials) is released, not
  failed.
* Session-start watchdog (subscription mode): for `CREWBOSS_RUNNER_START_WINDOW` seconds (120) after spawning, the runner watches the
  pty log (`agent.log`, ANSI stripped). Login text ("Select login method", "Please run /login", "Not logged in", ...), a Remote
  Control failure ("Remote Control cannot start", "could not attach"), an exit before registration, or (only with `CREWBOSS_RUNNER_REGISTER_CHECK=1`, off until
  the markers are confirmed on a real run) no sign of registration ("Remote Control active/connected", a claude.ai/code URL) all kill the agent and `release` the job with the reason "Claude Code needs you to sign in on <device>" (login) or "the Claude Code session did not start".
  The patterns are one tuple each in `runner/crewboss_runner/guard.py`; `CREWBOSS_RUNNER_REGISTER_CHECK=1` turns on the
  "no sign of registration" rule. A release with a reason becomes a comment on the task and one Web Push to the owner (server side,
  `jobs.release`); the signed-in preflight then stops the runner claiming again until the person logs in.
* Shutdown (SIGINT/SIGTERM, a revoke, task cancel) kills the running agent before its worktree is removed. The job is not
  released: its lease lapses and the server requeues it.
* Per job: a bare repo cache per project under `~/.local/state/crewboss-runner/repos/`, a worktree on `dash/j<id>-e<fence>` from the
  remote's default branch, the job token only in the MCP config `jobs/j<id>-e<fence>/mcp.json` (0600, in
  the worktree's PARENT; deleted when the job ends). The branch is pushed to the `git_remote` the server supplied, by URL.
* Fencing: a heartbeat every lease/3. A 409, or a whole lease without a good heartbeat, kills the agent and pushes and finishes
  nothing. A final heartbeat must succeed before the push, and heartbeats keep running through the push until `finish` is sent.
* Local status page: `127.0.0.1` only; the one-use bootstrap token is in the URL fragment, exchanged once for an in-memory
  session secret; no cookies; a wrong Host or Origin (or a cross-site Sec-Fetch-Site) gets 403.

## Labels (P4b)

A device carries **labels** (`device_tokens.labels`), a job carries **`requires_labels`** (copied from its profile when it is created,
plus any added on the Run form). The claim is unchanged except for one more condition in the same SQL statement: the job's
`requires_labels` must be contained in the device's `labels` (`<@`). The same condition decides the `job_available` nudges, the
on-connect offer and `claimable_jobs`/`claimable_devices`. A job whose labels no connected device holds simply waits queued.

Labels are lowercase `[a-z0-9-]`, 1 to 32 characters, at most 16 per device. **Only the owner sets them**, on the Machines page (Runners tab)
(`POST /<account>/runners/<id>/labels`, web session); the runner has no message that changes them and enrolment ignores any labels
it sends. The seeded `android` profile needs `android-usb`, so Android jobs go only to the PC with the phone attached.
