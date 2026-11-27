"""A cheap static look at an imported skill (task #718): phrases and files a person should read twice before letting every job follow it.
This is a warning list for the review screen, not a verdict: it finds text, it never runs or evaluates anything."""
from __future__ import annotations

import re

_RULES = (
    ("shell", "tells the agent to run a shell command", re.compile(
        r"\b(?:ba|z)?sh\s+-c\b|\b(?:run|execute|invoke)\s+[`'\"]?(?:ba|z)?sh\b|\b(?:run|execute)\s+[`'\"]?bash\b|"
        r"\bcurl\b[^\n]*\|\s*(?:ba)?sh|\bwget\b|\bbase64\s+(?:-d|--decode)\b|\beval\b\s*[\(\$`]|\beval\s+[\"'$`]|os\.system|subprocess\.", re.I)),
    ("secrets", "mentions secrets, tokens or keys", re.compile(
        r"\.env\b|credentials?\b|api[_-]?keys?\b|\bsecrets?\b|\btokens?\b|id_rsa|id_ed25519|\.ssh\b|\.pem\b|private key", re.I)),
    ("push", "tells the agent to push, force-push or skip checks", re.compile(
        r"\bgit\s+push\b|force[- ]push|push\s+--force|--force\b|--no-verify|\b(?:disable|skip|delete|remove)\s+(?:the\s+)?(?:tests?|checks?|hooks?)\b", re.I)),
)
_EXEC_FILE = re.compile(r"(?:^|/)scripts/|\.(?:sh|bash|py|ps1|bat|cmd|js|rb|pl)$", re.I)
MAX = 30


def scan(body: str, files: list[dict]) -> list[dict]:
    """[{"kind", "what", "where", "text"}]: `files` = [{"path", "text" (None for binary)}]. One entry per (rule, place), first match only."""
    out: list[dict] = []
    seen: set[tuple] = set()
    places = [("SKILL.md", body or "")] + [(f["path"], f.get("text") or "") for f in files]
    for where, text in places:
        for n, line in enumerate(text.splitlines(), 1):
            for kind, what, rx in _RULES:
                if (kind, where) not in seen and rx.search(line):
                    seen.add((kind, where))
                    out.append({"kind": kind, "what": what, "where": f"{where}:{n}", "text": line.strip()[:120]})
    for f in files:
        if _EXEC_FILE.search(f["path"]):
            out.append({"kind": "executable", "what": "ships a script an agent could run", "where": f["path"], "text": ""})
    return out[:MAX]
