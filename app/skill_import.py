"""Importing a community skill (task #718): from a public GitHub URL or from an uploaded SKILL.md / zipped skill folder.

THE CONTENT IS UNTRUSTED TEXT. Nothing here executes, imports or evaluates it. What it may do is bounded:
- GitHub: only https://github.com and https://raw.githubusercontent.com, default port, no user-info; a redirect is followed by hand
  and only to one of those same two hosts (at most 3 hops); every body is read with a size cap; the host's addresses must be public.
- A zip: no absolute paths, no `..`, no backslashes or drive letters, no symlinks, no encrypted entries, a cap on entries, on each
  file and on the total unpacked size (read in bounded chunks, so a header that lies cannot inflate past the cap).
- The front matter is yaml.safe_load; only a few plain fields are kept.
Licence rule: a recognised permissive licence is recorded; a recognised other one is refused; no licence found is recorded as
"unknown", and the skill is not used until a person confirms it (app/asset_skills.py).

`prepare_*` return a ParsedSkill (nothing is stored); app/asset_skills.py stores it."""
from __future__ import annotations

import io
import ipaddress
import re
import socket
import stat
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlsplit

from app import skill_md
from app.asset_store import PATH_RE
from app.repo import BadRequest

ALLOWED_HOSTS = ("github.com", "raw.githubusercontent.com")
MAX_REDIRECTS = 3
MAX_SKILL_MD = 256 * 1024
MAX_FILE = 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024
MAX_FILES = 32
MAX_ZIP = 5 * 1024 * 1024
MAX_ZIP_ENTRIES = 300
MAX_FETCHES = 24
TIMEOUT = 5.0
TOTAL_SECONDS = 30.0                    # one whole GitHub import
LICENSE_NAMES = ("LICENSE", "LICENSE.txt", "LICENSE.md", "LICENCE", "LICENCE.txt", "COPYING", "COPYING.txt")
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_JUNK = ("__MACOSX/", ".git/", ".github/")
_JUNK_NAMES = (".DS_Store", "Thumbs.db")


@dataclass
class ParsedSkill:
    key: str
    name: str
    description: str
    body: str
    allowed_tools: list[str]
    files: list[dict]                    # [{"path", "raw"}] (SKILL.md itself is not in here; its text is `body`)
    license: dict                        # {"status", "spdx", "evidence"}: status permissive | unknown (refused never gets here)
    source: dict
    warnings: list[str] = field(default_factory=list)

    def fields(self) -> dict:
        lic = {"status": self.license["status"], "spdx": self.license.get("spdx", ""), "evidence": self.license.get("evidence", "")}
        out = {"description": self.description, "license": lic, "source": self.source}
        if self.allowed_tools:
            out["allowed_tools"] = self.allowed_tools
        return out


# --- the folder: SKILL.md + files -> ParsedSkill ----------------------------------------------------------------------------

def _text(raw: bytes, what: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BadRequest(f"{what} is not UTF-8 text") from None


def parse_folder(files: dict[str, bytes], source: dict, fallback_name: str = "") -> ParsedSkill:
    """`files` = {relative path: bytes} of ONE skill folder (SKILL.md at its root)."""
    skill_raw = files.get("SKILL.md")
    if skill_raw is None:
        raise BadRequest("no SKILL.md found at the top of the skill folder")
    if len(skill_raw) > MAX_SKILL_MD:
        raise BadRequest(f"SKILL.md is over {MAX_SKILL_MD // 1024} KB")
    front, body = skill_md.parse(_text(skill_raw, "SKILL.md"))
    body = body.lstrip("\n")
    description = " ".join(str(front.get("description") or "").split())
    if not description:
        raise BadRequest("the SKILL.md front matter needs a description (what the skill is for and when to use it)")
    if len(description) > 1024:
        description = description[:1024].rstrip()
    key = skill_md.slugify(str(front.get("name") or fallback_name))
    if not skill_md.KEY_RE.match(key):
        raise BadRequest("the skill needs a name made of letters and numbers (the front matter's name:)")
    warnings: list[str] = []
    license_text = ""
    for n in LICENSE_NAMES:
        if n in files:
            license_text = files[n].decode("utf-8", "replace")
            break
    lic = skill_md.detect_license(front.get("license") if isinstance(front.get("license"), str) else "", license_text)
    if lic["status"] == "refused":
        raise BadRequest(f"licence {lic['spdx']} is not a permissive licence (MIT, Apache-2.0, BSD, ISC, 0BSD, Unlicense or CC0): "
                         "this skill can't be imported")
    if lic["status"] == "unknown":
        warnings.append("No recognisable licence was found: the skill is saved but not used until an admin confirms its licence.")
    if front.get("allowed-tools") or front.get("allowed_tools"):
        warnings.append("The skill asked to pre-approve tools (allowed-tools); an imported skill never gets that, so it was left out.")
    extra = [{"path": p, "raw": b} for p, b in sorted(files.items()) if p != "SKILL.md"]
    if len(extra) > MAX_FILES:
        raise BadRequest(f"the skill has {len(extra)} other files; the most a skill takes is {MAX_FILES}")
    for f in extra:
        if len(f["raw"]) > MAX_FILE:
            raise BadRequest(f"{f['path']} is over {MAX_FILE // 1024} KB")
    if sum(len(f["raw"]) for f in extra) + len(skill_raw) > MAX_TOTAL:
        raise BadRequest(f"the skill is over {MAX_TOTAL // (1024 * 1024)} MB in all")
    src = {k: v for k, v in source.items() if v}
    src["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if warnings:
        src["notes"] = [w[:300] for w in warnings][:20]
    return ParsedSkill(key=key, name=key.replace("-", " ").capitalize(),
                       description=description, body=body, allowed_tools=[], files=extra, license=lic,
                       source=src, warnings=warnings)


# --- an uploaded file or zip -------------------------------------------------------------------------------------------------

def _clean_member(name: str) -> str | None:
    """A zip member's name -> a safe relative path, None to skip it (a folder, junk), or BadRequest for a hostile one."""
    if "\x00" in name or "\\" in name:
        raise BadRequest("the zip has a file name with a backslash or a null byte")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise BadRequest("the zip has an absolute path")
    parts = name.split("/")
    if any(p == ".." for p in parts):
        raise BadRequest("the zip has a path that climbs out of the folder (..)")
    if name.endswith("/"):
        return None
    if any(name.startswith(j) or f"/{j}" in f"/{name}" for j in _JUNK) or parts[-1] in _JUNK_NAMES:
        return None
    return "/".join(p for p in parts if p not in ("", "."))


def safe_unzip(raw: bytes) -> dict[str, bytes]:
    """{path: bytes} of a zip's files, or BadRequest. Nothing is written to disk."""
    if len(raw) > MAX_ZIP:
        raise BadRequest(f"the zip is over {MAX_ZIP // (1024 * 1024)} MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise BadRequest("that is not a zip file") from None
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise BadRequest(f"the zip has more than {MAX_ZIP_ENTRIES} entries")
    out: dict[str, bytes] = {}
    total = 0
    for info in infos:
        path = _clean_member(info.filename)
        if path is None:
            continue
        if info.flag_bits & 0x1:
            raise BadRequest("the zip has an encrypted file")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise BadRequest("the zip has a symbolic link")
        if info.file_size > MAX_FILE:
            raise BadRequest(f"{path} is over {MAX_FILE // 1024} KB")
        try:
            with zf.open(info) as fh:
                data = fh.read(MAX_FILE + 1)           # the header may lie: the cap is on what is actually read
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
            raise BadRequest(f"{path} is damaged or uses a method this importer does not read") from None
        if len(data) > MAX_FILE:
            raise BadRequest(f"{path} is over {MAX_FILE // 1024} KB")
        total += len(data)
        if total > MAX_TOTAL:
            raise BadRequest(f"the zip unpacks to more than {MAX_TOTAL // (1024 * 1024)} MB")
        if path in out:
            raise BadRequest(f"the zip lists {path} twice")
        out[path] = data
    return out


def _single_folder(files: dict[str, bytes]) -> dict[str, bytes]:
    """Zips often wrap the skill in one folder: if SKILL.md is not at the top, it must be in the only top-level folder."""
    if "SKILL.md" in files:
        return files
    tops = {p.split("/", 1)[0] for p in files if "/" in p}
    cands = [t for t in tops if f"{t}/SKILL.md" in files]
    if len(cands) != 1 or any("/" not in p for p in files):
        if any(p.endswith("/SKILL.md") for p in files):
            raise BadRequest("the zip holds more than one skill, or other files beside the skill folder: upload one skill at a time")
        raise BadRequest("no SKILL.md found in the zip")
    top = cands[0]
    if sum(1 for p in files if p.endswith("/SKILL.md") or p == "SKILL.md") != 1:
        raise BadRequest("the zip holds more than one skill: upload one skill at a time")
    return {p[len(top) + 1:]: b for p, b in files.items() if p.startswith(top + "/")}


def _check_paths(files: dict[str, bytes]) -> tuple[dict[str, bytes], list[str]]:
    keep, warn = {}, []
    for p, b in files.items():
        if p == "SKILL.md" or PATH_RE.match(p) and ".." not in p:
            keep[p] = b
        else:
            warn.append(f"{p[:80]} was left out: file names are letters, numbers, dots, dashes and slashes only")
    return keep, warn


def prepare_upload(filename: str, raw: bytes) -> ParsedSkill:
    """An uploaded SKILL.md (a single text file) or a zip of the skill folder."""
    name = (filename or "").rsplit("/", 1)[-1]
    if raw[:4] == b"PK\x03\x04":
        files = _single_folder(safe_unzip(raw))
        fallback = ""
    else:
        if len(raw) > MAX_SKILL_MD:
            raise BadRequest(f"SKILL.md is over {MAX_SKILL_MD // 1024} KB")
        files, fallback = {"SKILL.md": raw}, name.rsplit(".", 1)[0]
    files, warn = _check_paths(files)
    out = parse_folder(files, {"via": "upload", "url": ""}, fallback_name="" if fallback.lower() in ("skill", "") else fallback)
    out.warnings += warn
    return out


# --- GitHub ------------------------------------------------------------------------------------------------------------------

def parse_github_url(url: str) -> dict:
    """-> {"owner", "repo", "ref", "dir"} (dir '' = the repo root). Only github.com and raw.githubusercontent.com over https."""
    url = (url or "").strip()
    try:
        u = urlsplit(url)
    except ValueError:
        raise BadRequest("that is not a web address") from None
    if u.scheme != "https" or (u.hostname or "").lower() not in ALLOWED_HOSTS or u.username or u.password or u.port not in (None, 443):
        raise BadRequest("only https://github.com/… and https://raw.githubusercontent.com/… addresses can be imported")
    segs = [unquote(s) for s in u.path.split("/") if s]
    if any(s in (".", "..") or "\x00" in s or "/" in s or "\\" in s for s in segs):
        raise BadRequest("that address has a path that is not allowed")
    host = (u.hostname or "").lower()
    if len(segs) < 2:
        raise BadRequest("the address needs an owner and a repository: https://github.com/owner/repo/tree/main/skill-folder")
    owner, repo = segs[0], segs[1].removesuffix(".git")
    ref, rest = "HEAD", segs[2:]
    if host == "raw.githubusercontent.com":
        if len(rest) < 2:
            raise BadRequest("a raw address needs a branch and a path")
        ref, rest = rest[0], rest[1:]
    elif rest:
        if rest[0] not in ("tree", "blob") or len(rest) < 2:
            raise BadRequest("use the address of the skill's folder or its SKILL.md on github.com")
        ref, rest = rest[1], rest[2:]
    if rest and rest[-1].lower() == "skill.md":
        rest = rest[:-1]
    if not (_PART.match(owner) and _PART.match(repo) and (ref == "HEAD" or _REF.match(ref)) and all(_PART.match(s) for s in rest)):
        raise BadRequest("that address has a name this importer will not fetch")
    return {"owner": owner, "repo": repo, "ref": ref, "dir": "/".join(rest)}


def _public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise BadRequest(f"could not look up {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise BadRequest(f"{host} resolves to a private address; refusing to fetch it")


def http_get(url: str, limit: int) -> tuple[int, bytes]:
    """GET `url` (already checked against the host list) -> (status, body up to `limit` bytes). Redirects are followed here, by
    hand, and only to the two allowed hosts over https. Tests replace this function; nothing else in the module opens a socket."""
    import httpx
    cur = url
    with httpx.Client(follow_redirects=False, timeout=TIMEOUT, trust_env=False, headers={"User-Agent": "crewboss-skill-import"}) as c:
        for _ in range(MAX_REDIRECTS + 1):
            u = urlsplit(cur)
            if u.scheme != "https" or (u.hostname or "").lower() not in ALLOWED_HOSTS or u.username or u.port not in (None, 443):
                raise BadRequest("the address redirected somewhere that is not github.com")
            _public(u.hostname)
            try:
                with c.stream("GET", cur) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location") or ""
                        cur = urljoin(cur, loc)
                        continue
                    chunks, n = [], 0
                    for part in r.iter_bytes():
                        n += len(part)
                        if n > limit:
                            raise BadRequest(f"a file is over {limit // 1024} KB")
                        chunks.append(part)
                    return r.status_code, b"".join(chunks)
            except httpx.HTTPError:
                raise BadRequest("could not fetch that address") from None
    raise BadRequest("too many redirects")


def _raw(g: dict, path: str) -> str:
    return f"https://raw.githubusercontent.com/{g['owner']}/{g['repo']}/{g['ref']}/{path}"


def _join(d: str, name: str) -> str:
    return f"{d}/{name}" if d else name


_REF_PATH = re.compile(r"(?<![\w/.:-])((?:\./)?[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9._-]+)+\.[A-Za-z0-9]{1,6})(?![\w/])")


def referenced_files(body: str) -> list[str]:
    """Relative file paths a SKILL.md body mentions (references/x.md, scripts/run.py), in order, without duplicates."""
    out: list[str] = []
    for m in _REF_PATH.finditer(body or ""):
        p = m.group(1).removeprefix("./")
        if ".." not in p and not p.startswith(("http", "www.")) and PATH_RE.match(p) and p not in out and p != "SKILL.md":
            out.append(p)
    return out


def prepare_github(url: str) -> ParsedSkill:
    """Fetch SKILL.md (+ the licence file + the files SKILL.md names) of a public GitHub skill folder, over the allowed hosts only.
    Other files in the folder can't be listed without the GitHub API, so a skill with files SKILL.md does not name is better
    imported from a zip."""
    g = parse_github_url(url)
    fetched = 0
    deadline = time.monotonic() + TOTAL_SECONDS

    def get(path: str, limit: int) -> bytes | None:
        nonlocal fetched
        if time.monotonic() > deadline:
            raise BadRequest(f"the import took longer than {int(TOTAL_SECONDS)} seconds; try again or upload a zip")
        if fetched >= MAX_FETCHES:
            return None
        fetched += 1
        status, data = http_get(_raw(g, path), limit)
        return data if status == 200 else None

    main = get(_join(g["dir"], "SKILL.md"), MAX_SKILL_MD)
    if main is None:
        raise BadRequest("no SKILL.md was found at that address (is the repository public, and is this the skill's folder?)")
    files: dict[str, bytes] = {"SKILL.md": main}
    warnings: list[str] = []
    for n in LICENSE_NAMES:
        got = get(_join(g["dir"], n), MAX_FILE)
        if got is None and g["dir"]:
            got = get(n, MAX_FILE)                       # the repository's own licence at its root
        if got is not None:
            files[n] = got
            break
    front, body = skill_md.parse(_text(main, "SKILL.md"))
    for p in referenced_files(body)[:MAX_FILES]:
        got = get(_join(g["dir"], p), MAX_FILE)
        if got is None:
            warnings.append(f"{p} is named in SKILL.md but could not be fetched")
        elif p not in files:
            files[p] = got
    files, warn = _check_paths(files)
    folder_name = g["dir"].rsplit("/", 1)[-1] if g["dir"] else g["repo"]
    src = {"via": "github", "url": url.strip()[:500], "ref": g["ref"] if g["ref"] != "HEAD" else "", "author": g["owner"]}
    out = parse_folder(files, src, fallback_name=folder_name)
    out.warnings += warnings + warn
    return out
