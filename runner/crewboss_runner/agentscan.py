"""Agent-setup scan: what will the spawned Claude Code agent load, and is any of it unsafe? (#646)

Looks (read only, never modifies) at the user-level and project-level `.claude/` settings, hooks, MCP server configs, plugins and
CLAUDE.md files, and at stray secrets in a worktree. Result: a list of findings {path, rule, severity, hint}.

SECURITY: a finding carries only a path (relative to the worktree, or ~-relative), a fixed rule id, a severity and a redacted hint
(a kind of thing and a count, never a value, a command or a URL's credentials). File contents and secret values are never stored,
logged or sent anywhere. Severity `block` stops this runner claiming jobs; `warn` is shown only.

Project-level config comes from a repository somebody else controls, so it is judged harder than the user's own `~/.claude`."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from . import settings

BLOCK, WARN = "block", "warn"
MAX_FILE = 1_000_000
MAX_FILES = 4000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".gradle", "build", "dist", "target", ".tox"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

RULES = {
    "perm.allow_all": "permissions allow everything (Bash(*), bypassPermissions)",
    "hook.remote_exec": "a hook downloads and runs code",
    "hook.outside_repo": "a hook runs a shell command from outside the repository",
    "hook.present": "a user-level hook runs on every agent action",
    "mcp.unexpected_host": "an MCP server points at an unexpected host",
    "mcp.remote_exec": "an MCP server command downloads and runs code",
    "plugin.enabled": "a plugin is enabled",
    "claudemd.injection": "a CLAUDE.md file contains instruction-override or fetch-and-run text",
    "secret.private_key": "a private key",
    "secret.aws_key": "an AWS access key id",
    "secret.gcp_key": "a Google service-account key",
    "secret.anthropic_key": "an Anthropic API key",
    "secret.github_token": "a GitHub token",
    "secret.env_file": "an .env file with values",
    "config.unreadable": "a config file could not be read or parsed",
    "hook.http": "a hook posts to a URL",
    "settings.env": "a project setting redirects or replaces the agent's API credentials",
    "settings.exec": "a project setting runs a command or helper (apiKeyHelper, statusLine, sandbox ...)",
    "settings.hooks": "a project settings file declares hooks (a job never runs a repository's hooks)",
    "settings.plugins": "a project settings file enables plugins or adds a plugin marketplace (a plugin is repository code)",
    "settings.allow": "a project settings file pre-approves tools beyond the job's allowed tools",
    "perm.broad_shell": "permissions allow a whole shell or interpreter",
    "config.symlink": "a link inside agent, command or skill files, or a linked .claude folder (Claude follows links)",
    "config.unreviewed": "an agent, command or skill file the project owner has not reviewed and pinned",
    "frontmatter.unreadable": "an agent or skill file's frontmatter could not be read the way Claude reads it",
    "frontmatter.hooks": "an agent or skill file declares hooks",
    "frontmatter.mcp_servers": "an agent or skill file declares MCP servers",
    "frontmatter.permission_mode": "an agent or skill file changes the permission mode",
    "frontmatter.tools": "an agent or skill file names tools outside the job profile's allowed tools",
}

SECRET_PATTERNS = (
    ("secret.private_key", "private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("secret.aws_key", "AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret.gcp_key", "Google service-account key", re.compile(r'"type"\s*:\s*"service_account"')),
    ("secret.anthropic_key", "Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("secret.github_token", "GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
)
REMOTE_EXEC = re.compile(r"(?:curl|wget|iwr|Invoke-WebRequest)\b[^\n|;]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b"
                         r"|base64\s+(?:-d|--decode)[^\n|]*\|\s*(?:ba)?sh\b|\beval\s+[\"'`$]\(?\s*(?:curl|wget)", re.I)
REMOTE_EXEC = re.compile(REMOTE_EXEC.pattern + r"|\$\(\s*(?:curl|wget)\b|`\s*(?:curl|wget)\b", re.I)
PKG_EXEC = re.compile(r"\b(?:npx\s+(?:-y|--yes)\b|pnpm\s+dlx\b|yarn\s+dlx\b|uvx\b|bunx\b)", re.I)
BROAD_SHELL = re.compile(r"^Bash\(\s*(?:sh|bash|zsh|dash|python3?|node|perl|ruby|pwsh|powershell)\s*(?::\*|\s\*|\*)?\s*\)$")
INJECTION = re.compile(r"ignore (?:all |any )?(?:previous|prior|above) instructions|disregard (?:all |the )?(?:previous|prior|above)|"
                       r"do not tell the user|without (?:asking|telling) the user", re.I)
ENV_VALUE = re.compile(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*(?P<v>\S.*)$")
ENV_PLACEHOLDER = re.compile(r"^[\"']?(?:|changeme|change-me|your[-_a-z0-9]*|xxx+|<.*>|\$\{?.*|todo|example|dummy|placeholder|true|false|\d{1,5})[\"']?$", re.I)


def finding(path, rule: str, severity: str, hint: str) -> dict:
    return {"path": str(path)[:140], "rule": rule, "severity": severity, "hint": hint[:100]}


def _json(path: Path, out: list, fail: str = WARN) -> dict | None:
    """Parse a JSON config. Oversize or unparseable -> a `config.unreadable` finding of severity `fail` (BLOCK for project settings:
    fail closed, an agent that can parse what we cannot must not be trusted with it)."""
    try:
        if path.stat().st_size > MAX_FILE:
            out.append(finding(path, "config.unreadable", fail, "too large to read"))
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        out.append(finding(path, "config.unreadable", fail, "not valid JSON"))
        return None
    return data if isinstance(data, dict) else None


def allowed_hosts(server: str | None = None) -> set[str]:
    """Hosts an MCP server may point at: this machine, the dashboard this runner talks to, and CREWBOSS_RUNNER_MCP_HOSTS (comma list)."""
    hosts = set(LOCAL_HOSTS)
    if server:
        hosts.add(server.rsplit(":", 1)[0].lower() if server.count(":") == 1 else server.lower())
    hosts.update(h.strip().lower() for h in (settings.get("MCP_HOSTS") or "").split(",") if h.strip())
    return hosts


def _under(path: str, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


# --- settings.json: permissions, hooks, plugins -------------------------------------------------------------------------
def _scan_settings(path: Path, project: Path | None, user_level: bool, out: list, ceiling: list[str] | None = None) -> None:
    data = _json(path, out, WARN if user_level else BLOCK)
    if not data:
        return
    sev = WARN if user_level else BLOCK   # the user's own choice is a warning; a repository's is untrusted
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    allow = [a for a in perms.get("allow", []) if isinstance(a, str)] if isinstance(perms.get("allow"), list) else []
    everything = [a for a in allow if a.strip() in ("*", "Bash", "Bash(*)", "Bash(**)", "Bash(:*)")]
    if everything or perms.get("defaultMode") == "bypassPermissions" or data.get("skipDangerousModePermissionPrompt") is True:
        out.append(finding(path, "perm.allow_all", sev, "allows every command without asking"))
    if any(BROAD_SHELL.match(a.strip()) for a in allow):
        out.append(finding(path, "perm.broad_shell", WARN, "allows a whole shell or interpreter"))
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    for event, groups in hooks.items():
        for grp in groups if isinstance(groups, list) else []:
            for h in (grp.get("hooks", []) if isinstance(grp, dict) else []):
                if not isinstance(h, dict):
                    continue
                ev = str(event)[:30]
                if h.get("type") == "http" and h.get("url"):
                    out.append(finding(path, "hook.http", sev, f"{ev} hook posts to a URL"))
                    continue
                cmd = h.get("command")
                if not isinstance(cmd, str):
                    continue
                if REMOTE_EXEC.search(cmd):
                    out.append(finding(path, "hook.remote_exec", BLOCK, f"{ev} hook downloads and runs code"))
                elif user_level:
                    out.append(finding(path, "hook.present", WARN, f"{ev} hook runs a shell command"))
                elif PKG_EXEC.search(cmd):
                    out.append(finding(path, "hook.remote_exec", BLOCK, f"{ev} hook runs an unpinned package"))
                elif _outside_repo(cmd, project):
                    out.append(finding(path, "hook.outside_repo", BLOCK, f"{ev} hook runs a command outside the repository"))
    if not user_level:
        _scan_project_settings(path, data, ceiling, out)
    elif isinstance(data.get("enabledPlugins"), dict):
        n = sum(1 for v in data["enabledPlugins"].values() if v)
        if n:
            out.append(finding(path, "plugin.enabled", WARN, f"{n} plugin(s) enabled"))


# Settings keys that run a command, load code or hand over credentials (from the Claude Code 2.1.330 binary's own list of settings that
# carry a command, plus the sandbox / language-server / MCP blocks). A repository may not set ANY of them, pinned or not.
EXEC_NAMES = {k.lower(): k for k in (
    "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh", "otelHeadersHelper", "statusLine", "subagentStatusLine",
    "fileSuggestion", "processWrapper", "policyHelpers", "proxyAuthHelper", "sandbox", "lspServers", "mcpServers", "defaultShell")}
EXEC_KEYS = frozenset(EXEC_NAMES)


PLUGIN_WORDS = ("marketplace", "plugin")   # enabledPlugins, extraKnownMarketplaces, its alias additionalMarketplaces (2.1.330 reads it exactly like
#                                            extraKnownMarketplaces), strictKnownMarketplaces, allowedMarketplaces, pluginDirs ... any current or future spelling


def _plugin_keys(data, limit: int = 20, max_nodes: int = 20000) -> list[str]:
    """Every settings key, at ANY nesting level, whose lower-cased name contains "marketplace" or "plugin" (a fixed key name, never a value).
    Iterative and bounded, so a hostile deeply nested file cannot cost more than max_nodes steps."""
    found: list[str] = []
    stack, seen = [data], 0
    while stack and seen < max_nodes and len(found) < limit:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            for k, v in node.items():
                ks = str(k)
                if v and any(w in ks.lower() for w in PLUGIN_WORDS) and ks[:40] not in found:   # an empty value ({}, [], false) sets nothing
                    found.append(ks[:40])
                stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _scan_project_settings(path: Path, data: dict, ceiling: list[str] | None, out: list) -> None:
    """A repository's `.claude/settings.json` / `settings.local.json` is loaded by Claude Code whatever the job's argv says (the binary has no
    way to load project AGENTS and SKILLS but not project settings: `--setting-sources user` drops both), so it is judged here and any of
    these BLOCKS the job, with no pin that can override it: hooks, plugins / marketplaces, `permissions.allow` beyond the job's allowed
    tools, any command-running or credential key, and any `env` key. (spawn.py also passes `disableAllHooks` as a second layer.)"""
    low = {str(k).lower(): v for k, v in data.items()}
    if low.get("hooks"):
        out.append(finding(path, "settings.hooks", BLOCK, "declares hooks"))
    bad = _plugin_keys(data)
    if bad:
        out.append(finding(path, "settings.plugins", BLOCK, "sets " + ", ".join(bad[:3]) + (f" +{len(bad) - 3} more" if len(bad) > 3 else "")))
    perms = low.get("permissions") if isinstance(low.get("permissions"), dict) else {}
    allow = next((v for k, v in perms.items() if str(k).lower() == "allow"), None)
    if allow and ceiling is not None:
        entries = allow if isinstance(allow, list) else [allow]
        outside = [a for a in entries if not (isinstance(a, str) and within_ceiling(a, ceiling))]
        if outside:
            out.append(finding(path, "settings.allow", BLOCK, f"allows {len(outside)} tool(s) outside the job's allowed tools: "
                               + ", ".join(str(a)[:30] for a in outside[:2])))
    env = low.get("env")
    if env:
        keys = sorted(str(k) for k in env) if isinstance(env, dict) else ["(not a table)"]
        out.append(finding(path, "settings.env", BLOCK, "sets env " + ", ".join(k[:40] for k in keys[:3]) + (f" +{len(keys) - 3} more" if len(keys) > 3 else "")))
    for k in sorted(k for k in low if k in EXEC_KEYS and low[k]):   # one finding per key: the hint names it (a fixed settings key, never a value)
        out.append(finding(path, "settings.exec", BLOCK, "sets " + EXEC_NAMES[k]))


_ABS = re.compile(r"(?:^|[\s\"'=(])(~[/\\]|/[A-Za-z0-9_.-]|[A-Za-z]:[\\/]|\$HOME\b|\$\{HOME\}|%USERPROFILE%)")
_PROJECT_VAR = re.compile(r"\"?\$\{?CLAUDE_PROJECT_DIR\}?\"?")
SYSTEM_PREFIXES = ("/dev/", "/usr/bin/", "/bin/", "/usr/local/bin/")


def _outside_repo(cmd: str, project: Path | None) -> bool:
    """True when the command names an absolute / home path that is not inside the project. `$CLAUDE_PROJECT_DIR` paths are inside."""
    cmd = _PROJECT_VAR.sub("PROJECT", cmd)
    for m in _ABS.finditer(cmd):
        token = re.split(r"[\s\"';|&)]", cmd[m.start(1):], maxsplit=1)[0]
        if token.startswith(SYSTEM_PREFIXES):
            continue
        if not _under(os.path.expanduser(token), project):
            return True
    return False


# --- MCP servers --------------------------------------------------------------------------------------------------------
# Jobs start the agent with --strict-mcp-config (spawn.py): only the job's own MCP config is loaded. So a project .mcp.json or the
# user's ~/.claude.json servers are never loaded by a job; these findings are informational (warn, never block).
def _scan_mcp_servers(path: Path, servers: object, server_host: str | None, out: list) -> None:
    if not isinstance(servers, dict):
        return
    ok_hosts = allowed_hosts(server_host)
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        url = spec.get("url")
        if isinstance(url, str):
            host = (urlparse(url).hostname or "").lower()
            if host not in ok_hosts:
                out.append(finding(path, "mcp.unexpected_host", WARN, f"server '{str(name)[:40]}' at host {host[:60] or '?'}; not loaded by jobs"))
        if "command" in spec:
            cmd = " ".join([str(spec.get("command", ""))] + [str(a) for a in spec.get("args", []) if isinstance(a, (str, int))])
            if REMOTE_EXEC.search(cmd):
                out.append(finding(path, "mcp.remote_exec", WARN, f"server '{str(name)[:40]}' downloads and runs code; not loaded by jobs"))


def _scan_claude_json(path: Path, server_host: str | None, out: list) -> None:
    data = _json(path, out)
    if not data:
        return
    _scan_mcp_servers(path, data.get("mcpServers"), server_host, out)
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    for proj in projects.values():
        if isinstance(proj, dict):
            _scan_mcp_servers(path, proj.get("mcpServers"), server_host, out)


# --- CLAUDE.md and secrets ----------------------------------------------------------------------------------------------
def _text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:2048]:
        return None
    return raw.decode("utf-8", "replace")


def _scan_claudemd(path: Path, out: list) -> None:
    text = _text(path)
    if text and (INJECTION.search(text) or REMOTE_EXEC.search(text)):
        out.append(finding(path, "claudemd.injection", WARN, "contains instruction-override or fetch-and-run text"))


def _scan_secrets_in(path: Path, out: list, committed: bool) -> None:
    """A secret pattern in a COMMITTED file is usually a test fixture or docs: warn. In an untracked file it is probably real: block."""
    text = _text(path)
    if text is None:
        return
    for rule, label, rx in SECRET_PATTERNS:
        if rx.search(text):
            out.append(finding(path, rule, WARN if committed else BLOCK,
                               f"{label} in a committed file (value not shown)" if committed else f"{label} in an untracked file (value not shown)"))
    if path.name == ".env" or path.name.startswith(".env."):
        n = 0
        for line in text.splitlines():
            m = ENV_VALUE.match(line)
            if m and not line.lstrip().startswith("#") and not ENV_PLACEHOLDER.match(m.group("v").strip()):
                n += 1
        if n:
            out.append(finding(path, "secret.env_file", WARN, f"{n} variable(s) with values (not shown)"))


def tracked_files(root: Path) -> set[str] | None:
    """Paths (posix, relative) git tracks in `root`; None when it is not a git work tree (then nothing counts as committed)."""
    try:
        res = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return {p.decode("utf-8", "replace") for p in res.stdout.split(b"\0") if p}


def _relative(findings: list[dict], base: Path, label: str) -> list[dict]:
    for f in findings:
        try:
            f["path"] = f"{label}{Path(f['path']).relative_to(base).as_posix()}"
        except ValueError:
            pass
    return findings


# --- agent, command and skill files (#829): deny by default, owner-reviewed pins ---------------------------------------------
# What a job's Claude Code will load from the repository: `.claude/agents/**`, `.claude/commands/**` and `.claude/skills/**` at ANY depth
# (a nested `<subdir>/.claude/` is loaded when the agent touches files under it), plus `.crewboss/skills/**` (copied into `.claude/skills/`
# by skills.install). Claude follows symlinks and reads whatever it finds, so NO decision rests on parsing these files:
#   1. a symlink anywhere in them (or a symlinked `.claude` dir) BLOCKS the job (`config.symlink`);
#   2. every regular file's (repo path, sha256 of its bytes) must be in the project's owner-reviewed PINS, else the job is blocked
#      (`config.unreviewed`) and the files are reported to the dashboard for the owner to read and pin;
#   3. more files than MAX_AGENT_FILES, a file too big to read in a review, a non-text file or an odd name: blocked too (fail closed).
# The frontmatter reader below is a WARNING layer only (more reasons on a block, and the doctor). It never approves anything.
AGENT_DIRS = ("agents", "commands", "skills")
MAX_AGENT_FILES = 200            # beyond this the owner could not review them: block rather than skip the rest
MAX_REVIEW_BYTES = 96 * 1024     # a file larger than this cannot be shown in a review, so it cannot be pinned
SETTINGS_NAMES = ("settings.json", "settings.local.json")


def _is_link(p: Path | str) -> bool:
    """A symlink, or on Windows a junction (which Claude follows as well)."""
    p = str(p)
    return os.path.islink(p) or bool(getattr(os.path, "isjunction", lambda _p: False)(p))


def _odd_name(rel: str) -> bool:
    return len(rel) > 300 or not rel.isprintable()


def _hash_file(p: Path) -> tuple[str, int, bool]:
    """-> (sha256 of the bytes, size, reviewable). Reviewable = small enough, UTF-8 text with no NUL."""
    h = hashlib.sha256()
    size, head, ok = 0, b"", True
    with open(p, "rb") as fh:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
            if size <= MAX_REVIEW_BYTES:
                head += chunk
    ok = size <= MAX_REVIEW_BYTES and b"\x00" not in head
    if ok:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            ok = False
    return h.hexdigest(), size, ok


def inventory(root: Path) -> tuple[dict[str, dict], list[dict], list[str]]:
    """Walk the WHOLE worktree for every `.claude` directory (root and nested, any depth) and `.crewboss/skills`, and list what Claude would
    load from them. -> ({repo path: {sha256, size, reviewable}}, findings, the `.claude` dirs found). Findings: `config.symlink` (BLOCK) for any link in those paths,
    `config.unreadable` (BLOCK) for anything that cannot be read or named. Read only; never follows a link."""
    root = Path(root)
    files: dict[str, dict] = {}
    out: list[dict] = []
    claude_dirs: list[str] = []

    def rel(p) -> str:
        return Path(p).relative_to(root).as_posix()

    def add_file(full: str) -> None:
        r = rel(full)
        if not os.path.isfile(full):
            out.append(finding(r, "config.unreadable", BLOCK, "not a regular file"))
        elif _odd_name(r):
            out.append(finding(r.encode("ascii", "backslashreplace").decode(), "config.unreadable", BLOCK, "file name has unusual characters"))
        else:
            try:
                sha, size, ok = _hash_file(Path(full))
                files[r] = {"sha256": sha, "size": size, "reviewable": ok}
            except OSError:
                out.append(finding(r, "config.unreadable", BLOCK, "file could not be read"))

    def collect(base: str) -> None:
        for dp, dns, fns in os.walk(base, followlinks=False, onerror=lambda e: out.append(finding(rel(e.filename), "config.unreadable", BLOCK, "folder could not be read"))):
            for n in list(dns) + list(fns):
                full = os.path.join(dp, n)
                if _is_link(full):
                    out.append(finding(rel(full), "config.symlink", BLOCK, "a link inside agent, command or skill files (Claude follows links)"))
            dns[:] = [d for d in dns if not _is_link(os.path.join(dp, d))]
            for n in fns:
                full = os.path.join(dp, n)
                if not _is_link(full):
                    add_file(full)

    def handle_claude(cdir: str) -> None:
        claude_dirs.append(cdir)
        try:
            names = sorted(os.listdir(cdir))
        except OSError:
            out.append(finding(rel(cdir), "config.unreadable", BLOCK, "folder could not be read"))
            return
        for n in names:
            full = os.path.join(cdir, n)
            low = n.lower()
            if low in AGENT_DIRS:
                if _is_link(full):
                    out.append(finding(rel(full), "config.symlink", BLOCK, f"{n} is a link (Claude follows links)"))
                elif os.path.isdir(full):
                    collect(full)
            elif low in SETTINGS_NAMES:   # a project settings file is pinned like an agent file: any change needs the Owner's review
                if _is_link(full):
                    out.append(finding(rel(full), "config.symlink", BLOCK, "settings file is a link"))
                else:
                    add_file(full)

    def walk_error(e):
        out.append(finding(rel(e.filename) if e.filename else ".", "config.unreadable", BLOCK, "folder could not be read"))

    for dp, dns, fns in os.walk(root, followlinks=False, onerror=walk_error):
        dns[:] = [d for d in dns if d != ".git"]
        for n in list(dns) + list(fns):
            full = os.path.join(dp, n)
            if _is_link(full):
                if n.lower() in (".claude", ".crewboss"):
                    out.append(finding(rel(full), "config.symlink", BLOCK, f"{n} is a link (Claude follows links)"))
                elif n in dns and not _under(os.path.realpath(full), root):
                    out.append(finding(rel(full), "config.symlink", BLOCK, "a folder link that leaves the repository"))
        for d in dns:
            full = os.path.join(dp, d)
            if d.lower() == ".claude" and not _is_link(full):
                handle_claude(full)
        # `.crewboss/skills` belongs to the repository ROOT only (skills.install copies nothing else). Names are compared case-insensitively:
        # on NTFS / APFS `.CREWBOSS/Skills` IS `.crewboss/skills` to the installer.
        if Path(dp) == root:
            for cb in [d for d in dns if d.lower() == ".crewboss"]:
                cbp = os.path.join(dp, cb)
                if _is_link(cbp):
                    continue
                try:
                    kids = sorted(os.listdir(cbp))
                except OSError:
                    out.append(finding(rel(cbp), "config.unreadable", BLOCK, "folder could not be read"))
                    continue
                for k in kids:
                    sk = os.path.join(cbp, k)
                    if k.lower() != "skills":
                        continue
                    if _is_link(sk):
                        out.append(finding(rel(sk), "config.symlink", BLOCK, "skills is a link (Claude follows links)"))
                    elif os.path.isdir(sk):
                        collect(sk)
    return files, out, claude_dirs


def pin_findings(files: dict[str, dict], pins, enforce: bool = True) -> tuple[list[dict], list[dict]]:
    """Judge an inventory against the owner's pins (a set of (path, sha256)). -> (findings, unreviewed files [{path, sha256, size, reviewable}]).
    `enforce` False (the doctor: no pin list known here) only warns."""
    sev = BLOCK if enforce else WARN
    pinned = set(pins or ())
    out: list[dict] = []
    unreviewed = [{"path": p, **meta} for p, meta in sorted(files.items()) if (p, meta["sha256"]) not in pinned]
    if len(files) > MAX_AGENT_FILES:
        out.append(finding(".claude", "config.unreviewed", sev, f"{len(files)} agent, command and skill files: more than {MAX_AGENT_FILES} cannot be reviewed"))
        return out, []
    for f in unreviewed:
        why = ("not reviewed yet" if f["reviewable"] else
               f"too large or not text: cannot be reviewed ({f['size']} bytes)")
        out.append(finding(f["path"], "config.unreviewed", sev, why))
    return out, unreviewed


def scan_agent_files(root: Path, pins, ceiling: list[str] | None = None, enforce: bool = True) -> tuple[list[dict], list[dict], dict]:
    """The agents / commands / skills check of one worktree. -> (findings, unreviewed files, inventory). Includes the frontmatter WARNINGS
    for the files that were readable. Paths are relative to `root`."""
    root = Path(root)
    files, out, cdirs = inventory(root)
    more, unreviewed = pin_findings(files, pins, enforce)
    out += more
    for cd in cdirs:                       # settings.json / settings.local.json of EVERY .claude dir keep the existing checks
        for n in sorted(os.listdir(cd)):
            full = Path(cd) / n
            if n.lower() in SETTINGS_NAMES and not _is_link(full) and full.is_file():
                _scan_settings(full, root, False, out, ceiling)
    for r in sorted(files):
        if files[r]["reviewable"] and (r.lower().endswith(".md") or r.lower().endswith("/skill.md")):
            _scan_frontmatter_file(root / r, r, ceiling, out)
    return out, unreviewed, files


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Top-level keys of a leading `---` YAML block -> the raw text of each value. {} when the file has no frontmatter; None when this simple
    reader cannot follow it. WARNING LAYER ONLY: it judges raw text, not what Claude decodes (escaped keys, duplicate keys, other line
    breaks all differ), so nothing may rest on its answer."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    key = None
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            return out
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t" or line.startswith("- ") or line.rstrip() == "-":
            if key is None:
                return None
            out[key] += "\n" + line.strip()
            continue
        m = _FM_KEY.match(line.rstrip())
        if not m:
            return None
        key = m.group("k").strip("\"'")
        out[key] = (m.group("v") or "").strip()
    return None   # never closed


_FM_KEY = re.compile(r"""^(?P<k>"[^"]+"|'[^']+'|[A-Za-z_][A-Za-z0-9_.-]*)\s*:(?:\s+(?P<v>.*))?$""")


def _fm_tokens(value: str) -> list[str]:
    """Tool names from a frontmatter value: a comma / space separated string, a `[a, b]` flow list or `- a` block list. Commas and spaces
    inside parentheses belong to the tool specifier."""
    tokens, buf, depth = [], "", 0
    for ch in value.replace("\n- ", "\n").replace("\n", ",") + ",":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        if depth == 0 and ch in ", \t[]":
            if buf.strip(" -\"'"):
                tokens.append(buf.strip(" -\"'"))
            buf = ""
        else:
            buf += ch
    return tokens


def within_ceiling(tool: str, ceiling: list[str]) -> bool:
    """Is `tool` (as named in frontmatter) no wider than something the job profile allows? Used for a WARNING only."""
    allowed = {c.strip() for c in ceiling}
    t = tool.strip()
    if t in allowed:
        return True
    base = t.split("(", 1)[0]
    if "(" in t and base in allowed:
        return True
    if t.startswith("mcp__"):
        stem = t[:-3] if t.endswith("__*") else t
        return any(stem == a or stem.startswith(a + "__") for a in allowed if a.startswith("mcp__"))
    return False


def _scan_frontmatter_file(path: Path, shown: str, ceiling: list[str] | None, out: list) -> None:
    """The warning layer: what the raw frontmatter text says. Severity is always WARN: a pinned file the owner read is allowed to declare
    these, and an unpinned one is already blocked."""
    text = _text(path)
    if text is None:
        return
    fm = parse_frontmatter(text)
    if fm is None:
        out.append(finding(shown, "frontmatter.unreadable", WARN, "frontmatter could not be read by the simple reader"))
        return
    for key, value in fm.items():
        k = key.lower().replace("_", "").replace("-", "")
        if k == "hooks":
            out.append(finding(shown, "frontmatter.hooks", WARN, "declares hooks"))
        elif k == "mcpservers":
            out.append(finding(shown, "frontmatter.mcp_servers", WARN, "declares MCP servers"))
        elif k == "permissionmode":
            if value.strip("\"' ").lower() not in ("", "default"):
                out.append(finding(shown, "frontmatter.permission_mode", WARN, "sets a permission mode other than default"))
        elif k in ("tools", "allowedtools"):
            toks = _fm_tokens(value)
            wild = [t for t in toks if t.strip() in ("*", "Bash(*)", "Bash(**)", "Bash(:*)")]
            outside = wild if ceiling is None else [t for t in toks if not within_ceiling(t, ceiling)]
            if outside:
                out.append(finding(shown, "frontmatter.tools", WARN,
                                   f"names {len(outside)} tool(s) outside the job profile's allowed tools: " + ", ".join(outside[:3])))
    if any(ch in text[:4000] for ch in ("\x85", "\u2028", "\u2029")):
        out.append(finding(shown, "frontmatter.unreadable", WARN, "has line breaks that YAML readers disagree on"))


def scan_tree(root: Path, ceiling: list[str] | None = None, pins=frozenset()) -> list[dict]:
    """Project-level scan of one worktree / repository directory. Paths in the result are relative to it. `pins` = the owner-reviewed
    {(repo path, sha256)} of the project: DENY BY DEFAULT (omitted = nothing is pinned, so any agent, command or skill file blocks).
    `pins=None` means "no pin list known here" (the doctor): unreviewed files are then a warning, not a block."""
    return scan_worktree(root, ceiling, pins)[0]


def scan_worktree(root: Path, ceiling: list[str] | None = None, pins=frozenset()) -> tuple[list[dict], list[dict], dict]:
    """scan_tree, plus the unreviewed files and the inventory (path -> sha256 ...) the worker needs. -> (findings, unreviewed, inventory)."""
    out: list[dict] = []
    root = Path(root)
    if not root.is_dir():
        return out, [], {}
    found, unreviewed, inv = scan_agent_files(root, pins, ceiling, enforce=pins is not None)
    out.extend(found)
    mcp = _json(root / ".mcp.json", out)
    if mcp:
        _scan_mcp_servers(root / ".mcp.json", mcp.get("mcpServers"), None, out)
    tracked = tracked_files(root) or set()
    seen = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            seen += 1
            if seen > MAX_FILES:   # the secrets / CLAUDE.md look is best effort, but it says so (agent files are all inventoried above)
                out.append(finding(".", "config.unreadable", WARN, f"only the first {MAX_FILES} files were checked for secrets"))
                return _relative(out, root, ""), unreviewed, inv
            p = Path(dirpath) / f
            if p.is_symlink():
                continue
            if f in ("CLAUDE.md", "CLAUDE.local.md"):
                _scan_claudemd(p, out)
            _scan_secrets_in(p, out, p.relative_to(root).as_posix() in tracked)
    return _relative(out, root, ""), unreviewed, inv


def config_dir(home: Path) -> Path:
    """Where Claude Code keeps its user config: CLAUDE_CONFIG_DIR when set (spawn.py passes it on to jobs), else ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else home / ".claude"


def scan_user(home: Path | None = None, server_host: str | None = None) -> list[dict]:
    """User-level scan: settings, hooks, plugins, CLAUDE.md and MCP servers of the user's Claude config. Paths are ~-relative."""
    out: list[dict] = []
    home = Path(home) if home else Path.home()
    cdir = config_dir(home)
    custom = bool(os.environ.get("CLAUDE_CONFIG_DIR"))
    for name in ("settings.json", "settings.local.json"):
        _scan_settings(cdir / name, None, True, out)
    _scan_claude_json((cdir / ".claude.json") if custom else (home / ".claude.json"), server_host, out)
    _scan_claudemd(cdir / "CLAUDE.md", out)
    plug_path = cdir / "plugins" / "installed_plugins.json"
    plug = _json(plug_path, out)
    if plug:
        inner = plug.get("plugins", plug)
        if isinstance(inner, dict) and inner:
            out.append(finding(plug_path, "plugin.enabled", WARN, f"{len(inner)} plugin(s) installed"))
    try:
        under_home = cdir.resolve().is_relative_to(home.resolve())
    except OSError:
        under_home = False
    out = _relative(out, home, "~/") if under_home else _relative(out, cdir, "$CLAUDE_CONFIG_DIR/")
    return out


def scan_all(home: Path | None = None, worktrees: list[Path] | None = None, server_host: str | None = None) -> list[dict]:
    out = scan_user(home, server_host)
    for wt in worktrees or []:
        out.extend({**x, "path": f"{Path(wt).name}/{x['path']}"} for x in scan_tree(wt))
    return out


USER_PREFIXES = ("~/", "$CLAUDE_CONFIG_DIR/")


def blocking(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] == BLOCK]


def wire(findings: list[dict], limit: int = 8) -> list[dict]:
    """What may be sent to the dashboard: the four fields only, blocks first."""
    # The user's own warnings (their hooks, plugins, MCP servers) are permanent noise on a real PC: `doctor` shows them, the dashboard does not.
    findings = [f for f in findings if f["severity"] == BLOCK or not f["path"].startswith(USER_PREFIXES)]
    ordered = sorted(findings, key=lambda f: (f["severity"] != BLOCK, f["path"], f["rule"]))
    return [{k: f[k] for k in ("path", "rule", "severity", "hint")} for f in ordered[:limit]]
