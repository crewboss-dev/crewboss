"""The thin GitHub client behind the merge executor (task #829) and the protection card. Plain HTTPS to api.github.com (stdlib urllib; the
image has no `gh`). The credential is a fine-grained token of a dedicated bot account, read from a 0600 FILE (config.GITHUB_TOKEN_FILE,
CREWBOSS_GITHUB_TOKEN_FILE; never the environment, the DB or a log) at the moment of each request.

The executor's credential only ever merges: it is NEVER used to change branch protection (a human admin applies that once, from the card).
Tests never reach it: they pass a fake with the same `request(method, path, json=None)` method."""
from __future__ import annotations

import json as _json
import os
import re
import stat
import urllib.error
import urllib.request

from app import config
from app.repo import BadRequest

API = "https://api.github.com"
TIMEOUT = 20
MIN_TOKEN_LEN = 30
_REMOTE = re.compile(r"github\.com[:/]+([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")
_PATH = re.compile(r"^/(user|repos/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(/[A-Za-z0-9._~/:=-]*)?)\Z")


class GitHubError(BadRequest):
    """GitHub (or the network) refused a call. `status` is the HTTP status, 0 for a network failure or a missing credential."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        """Worth retrying: the network, a 5xx, a timeout or a rate limit. Every other 4xx is a refusal that retrying cannot change."""
        text = str(self).lower()
        return self.status == 0 or self.status >= 500 or self.status in (408, 429) or (self.status == 403 and "rate limit" in text)


def repo_full_name(git_remote: str) -> str:
    """'owner/name' from a project's git remote (https or ssh GitHub URL), else BadRequest."""
    m = _REMOTE.search((git_remote or "").strip())
    if not m:
        raise BadRequest("this project's git remote is not a GitHub repository")
    return f"{m.group(1)}/{m.group(2)}"


def read_token(path: str | None = None) -> str:
    """The token from its 0600 file, read now. Refused when the file is missing, not a plain file (Docker made a directory because it was
    missing at `compose up`), readable by anyone but its owner, or too short. Messages name the file, never its content."""
    path = path if path is not None else config.GITHUB_TOKEN_FILE
    if not path:
        raise GitHubError("Crewboss has no GitHub token configured (CREWBOSS_GITHUB_TOKEN_FILE)")
    try:
        st = os.stat(path)
    except OSError:
        raise GitHubError("Crewboss has no GitHub token configured: the token file is not mounted") from None
    if not stat.S_ISREG(st.st_mode):
        raise GitHubError("the GitHub token path is not a file (it was missing when Crewboss started)")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise GitHubError("the GitHub token file is readable by others: chmod 600 it first")
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        raise GitHubError("the GitHub token file cannot be read") from None
    if len(token) < MIN_TOKEN_LEN or any(c.isspace() for c in token):
        raise GitHubError("the GitHub token file holds no usable token")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization header to wherever it points."""

    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class GitHubClient:
    def __init__(self, token_file: str | None = None):
        self._token_file = token_file

    def request(self, method: str, path: str, json: dict | None = None) -> dict:
        if method not in ("GET", "PATCH", "PUT", "POST") or not _PATH.match(path) or "//" in path \
                or any(seg in (".", "..") for seg in path.split("/")):
            raise GitHubError("Crewboss refused to build that GitHub call")
        token = read_token(self._token_file)
        data = _json.dumps(json).encode() if json is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "crewboss-merge-executor", **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as resp:
                body = resp.read(2_000_000)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = str((_json.loads(exc.read(20_000).decode("utf-8", "replace")) or {}).get("message") or "")
            except Exception:  # noqa: BLE001 - an unreadable error body is just no detail
                pass
            raise GitHubError(f"GitHub refused: HTTP {exc.code} {' '.join(detail.split())[:200]}".strip(), exc.code) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise GitHubError(f"could not reach GitHub ({type(exc).__name__})", 0) from None
        try:
            out = _json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return {}
        return out if isinstance(out, dict) else {"items": out}


def default_client() -> GitHubClient:
    return GitHubClient()


def credential_login(client=None) -> str:
    """Whose credential Crewboss holds, asked from GitHub (never typed). BadRequest unless it is a usable, non-human identity. FAILS
    CLOSED: with no bot login configured (CREWBOSS_GITHUB_BOT_LOGIN / instance.yaml merge_executor.bot_login) nothing is accepted, so a
    person's token can never pass by default. The login GitHub reports must equal the configured bot and not be on the deny-list."""
    if not config.GITHUB_BOT_LOGIN:
        raise BadRequest("no GitHub bot account is configured: set CREWBOSS_GITHUB_BOT_LOGIN (or instance.yaml merge_executor.bot_login) "
                         "to the dedicated bot's login first")
    client = client or default_client()
    login = str((client.request("GET", "/user") or {}).get("login") or "").strip()
    if not login:
        raise BadRequest("GitHub did not say whose credential Crewboss holds")
    low = login.lower()
    if low in config.GITHUB_DENY_LOGINS:
        raise BadRequest(f"Crewboss's GitHub credential belongs to {login}, a person's account: use the dedicated bot account's token")
    if low != config.GITHUB_BOT_LOGIN:
        raise BadRequest(f"Crewboss's GitHub credential belongs to {login}, not the configured bot account")
    return login
