"""Best-effort scrub of text before it leaves this PC for another model provider (#861). A port of the dashboard's
app/deploys.redact (the runner cannot import the app): key blocks, credential headers, URL passwords, credential query parameters,
name=value secrets, bearer and known token shapes, AWS keys, JWTs, password hashes and long opaque strings (a 40-hex commit id and a sha256:-prefixed image digest are kept).
Best effort, not a guarantee: the per-project switch is the real boundary."""
from __future__ import annotations

import re

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_QUERY_KEYS = r"key|pwd|passwd|password|auth|sig|signature|session|sid|token|access_token|refresh_token|client_secret|api_key|apikey"
_REDACT = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[private key removed]"),
    (re.compile(r"(?im)\b((?:proxy-)?authorization|set-cookie|cookie)(\s*[:=]\s*)[^\r\n]*"), r"\1\2[removed]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,31}://[^\s/:@]+:)[^\s/@]+(@)"), r"\1[removed]\2"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [removed]"),
    (re.compile(r"(?i)(?<![A-Za-z0-9_])((?:" + _QUERY_KEYS + r")=)[^&\s\"'<>]+"), r"\1[removed]"),
    # a YAML key whose value is on the NEXT line (`password:` / `password: |` then an indented line). Runs before the same-line rule below.
    # The key is exactly password / passwd / pwd, with an optional prefix ending in _ . or - (db_password) and an optional short suffix
    # (password_hash, password_2): `compass:` and `password_policy:` are not password keys and keep their next line.
    (re.compile(r"(?im)^([ \t]*(?:-[ \t]+)?[\"']?(?:[A-Za-z0-9_.-]{0,64}[_.-])?(?:password|passwd|pwd)"
                r"(?:[_-](?:value|new|old|hash|secret|[0-9]))?[\"']?[ \t]*:[ \t]*(?:[|>][+-]?[0-9]?[ \t]*)?\r?\n[ \t]+)[^\r\n]+"), r"\1[removed]"),
    # *_PW=value / PW: value (an env name that ends in PW; "pw" followed by = or : is a password, nothing else)
    (re.compile(r"(?i)(pw[ \t]*[=:][ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\S+)"), r"\1[removed]"),
    # sshpass -p PASSWORD (also -p'PASSWORD'). The gap is bounded (40) so a long run of `sshpass ` words stays linear.
    (re.compile(r"(?i)\b(sshpass\b[^\r\n]{0,40}?\s-p[ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\S+)"), r"\1[removed]"),
    # curl -u user:pass / --user user:pass (a colon in the value; uid:gid such as `docker run -u 1000:1000` is not a credential)
    # (`-ubob:pass` with no space, `--user=`, and a quoted value that holds spaces: -u 'bob:pa ss')
    (re.compile(r"(?<![\w-])((?:-u[ \t]*=?|--user(?:[ \t]+|=))[\"']?)(?!\d+:\d+(?:[\"'\s]|$))"
                r"(?:(?<=\")[^\":\r\n]{1,64}:[^\"\r\n]*|(?<=')[^':\r\n]{1,64}:[^'\r\n]*|[^\s:'\"]{1,64}:[^\s'\"]+)"), r"\1[removed]"),
    # name=value / name: value secrets, also JSON-quoted ("password": "x", "db_password":"x", 'k': 'v'); pwd/passwd names; names that end in
    # key/secret/token with no word boundary before it (STRIPE_KEY=, GITHUBTOKEN=, db_secret:). Anchored on the KEYWORD (the name's prefix is left
    # where it is) with bounded parts and a same-line separator: linear time on a long unbroken run, and it never eats the next line's token.
    (re.compile(r"(?i)((?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|[_-]key|authorization|credential)[A-Za-z0-9_.-]{0,64}[\"']?"
                r"[ \t]*[=:][ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\S+)"), r"\1[removed]"),
    (re.compile(r"(?<=[A-Z0-9_])(KEY[ \t]*[=:][ \t]*)\S+"), r"\1[removed]"),      # an upper-case env name ending in KEY (MYKEY=, SSH_KEY=)
    # a 32-64 hex value after key / token / secret (a 64-hex there goes even when it carries a sha256: prefix) (kept apart from the commit-id rule below, which leaves a bare 40-hex alone)
    (re.compile(r"(?i)((?:key|token|secret)[\"']?[ \t]*[=:][ \t]*[\"']?(?:sha256:)?)[0-9a-f]{32,64}\b"), r"\1[removed]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[token removed]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[token removed]"),          # a Google API key
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), "[token removed]"),
    (re.compile(r"(?i)\b((?:mysql|mariadb|mysqldump|mysqladmin)\b[^\r\n]{0,80}?\s)-p\S+"), r"\1-p[removed]"),
    (re.compile(r"\$(?:6|5|1|y|7|2[abxy]?)\$[./A-Za-z0-9$,=-]{8,}"), "[hash removed]"),
    (re.compile(r"\$(?:apr1|argon2(?:id|i|d)|pbkdf2(?:-sha(?:1|256|512))?|scrypt|sha1|md5|P|H|S)\$[./A-Za-z0-9$,=+-]{8,}"), "[hash removed]"),
    (re.compile(r"\{(?:S?SHA(?:1|256|512)?|MD5|CRYPT|SHA(?:256|512)-CRYPT|ARGON2|PBKDF2(?:-SHA(?:1|256|512))?)\}[A-Za-z0-9+/=$.-]{8,}"), "[hash removed]"),
    (re.compile(r"\b(?:cbj|cbr|cb|mdj|mdd|mdt|sk|ghp|gho|xox[a-z])[_-][A-Za-z0-9_-]{16,}"), "[token removed]"),
    (re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/])"), None),   # long opaque blobs, but not a 40-hex sha
]


def _blob(m) -> str:
    """A long opaque run is a secret, except a git commit id (40 hex) and an image digest (`sha256:` + 64 hex). A bare 32-hex (an md5 or a
    session id) and a bare 64-hex (a sha256 that is not labelled as one) go. Between 32 and 39 characters it must mix upper, lower and
    digits (a plain word or a path is left alone); from 40 on any such run goes."""
    t = m.group(0)
    if re.fullmatch(r"[0-9a-fA-F]{32}", t):
        return "[removed]"
    if re.fullmatch(r"[0-9a-fA-F]{64}", t):
        return t if m.string[max(m.start() - 7, 0):m.start()].lower() == "sha256:" else "[removed]"
    if SHA_RE.match(t) or re.fullmatch(r"[0-9a-f]{40,64}", t):
        return t
    if len(t) < 40 and not (re.search(r"[A-Z]", t) and re.search(r"[a-z]", t) and re.search(r"[0-9]", t)):
        return t
    return "[removed]"


def redact(text: str) -> str:
    for pat, repl in _REDACT:
        text = pat.sub(_blob, text) if repl is None else pat.sub(repl, text)
    return text
