"""SKILL.md text helpers (task #718): parse and write the front matter, make a key from a name, detect a licence.

Everything here works on text only. Nothing from a community skill is ever executed or imported as code: the front matter is read
with yaml.safe_load and only a few plain fields are kept."""
from __future__ import annotations

import re

import yaml

from app.repo import BadRequest

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FRONT_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
MAX_FRONT = 8 * 1024

# Licences a skill may be installed under (the estate's rule: permissive only). SPDX id -> the names people write for it.
PERMISSIVE = {
    "MIT": ("mit", "mit license"),
    "Apache-2.0": ("apache-2.0", "apache 2.0", "apache2", "apache license 2.0", "apache-2"),
    "BSD-2-Clause": ("bsd-2-clause", "bsd-2", "bsd 2-clause", "simplified bsd"),
    "BSD-3-Clause": ("bsd-3-clause", "bsd-3", "bsd 3-clause", "bsd", "new bsd"),
    "ISC": ("isc",),
    "0BSD": ("0bsd", "zero-clause bsd"),
    "Unlicense": ("unlicense", "the unlicense"),
    "CC0-1.0": ("cc0-1.0", "cc0", "cc0 1.0"),
}
_NAME_TO_SPDX = {alias: spdx for spdx, names in PERMISSIVE.items() for alias in names}

# What a licence FILE says. A copyleft or restricted licence wins over a permissive word elsewhere in the file; "all rights
# reserved" alone (no grant) is a refusal, but it also sits in the notice line of many MIT files, so a grant wins over it.
_NOT_PERMISSIVE = (
    (re.compile(r"GNU AFFERO GENERAL PUBLIC LICENSE", re.I), "AGPL"),
    (re.compile(r"GNU LESSER GENERAL PUBLIC LICENSE", re.I), "LGPL"),
    (re.compile(r"GNU GENERAL PUBLIC LICENSE", re.I), "GPL"),
    (re.compile(r"Mozilla Public License", re.I), "MPL"),
    (re.compile(r"Creative Commons[^\n]{0,80}(NonCommercial|NoDerivatives|ShareAlike)|CC[ -]BY[ -](NC|ND|SA)", re.I), "CC-BY-restricted"),
    (re.compile(r"Eclipse Public License|Common Development and Distribution|European Union Public Licen[cs]e", re.I), "copyleft"),
)
_ALL_RIGHTS = re.compile(r"\ball rights reserved\b", re.I)    # only counts when the file grants no permissive licence
_PERMISSIVE_TEXT = (
    (re.compile(r"CC0 1\.0 Universal|Creative Commons Zero", re.I), "CC0-1.0"),
    (re.compile(r"This is free and unencumbered software released into the public domain", re.I), "Unlicense"),
    (re.compile(r"Apache License[\s,]+Version 2\.0", re.I), "Apache-2.0"),
    (re.compile(r"Zero-Clause BSD|Permission to use, copy, modify, and/or distribute this software for any purpose "
                r"with or without fee is hereby granted\.", re.I), "0BSD"),
    (re.compile(r"Permission to use, copy, modify, and(/or)? distribute this software for any purpose with or without fee "
                r"is hereby granted, provided", re.I), "ISC"),
    (re.compile(r"Permission is hereby granted, free of charge, to any person obtaining a copy", re.I), "MIT"),
    (re.compile(r"Redistribution and use in source and binary forms, with or without\s+modification, are permitted "
                r"provided that.{0,400}?(Neither the name|endorse or promote)", re.I | re.S), "BSD-3-Clause"),
    (re.compile(r"Redistribution and use in source and binary forms, with or without\s+modification, are permitted "
                r"provided that", re.I), "BSD-2-Clause"),
)


class _PlainLoader(yaml.SafeLoader):
    """SafeLoader without anchors and aliases: an alias bomb ('&a [*a, *a, ...]') expands without bound from a few KB."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("aliases are not allowed")
        if getattr(self.peek_event(), "anchor", None) is not None:
            raise yaml.YAMLError("anchors are not allowed")
        return super().compose_node(parent, index)


def slugify(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:64].strip("-")
    return key


def parse(text: str) -> tuple[dict, str]:
    """A SKILL.md -> (front matter dict, body). No front matter = ({}, whole text). A malformed one is a BadRequest."""
    text = text or ""
    m = FRONT_RE.match(text)
    if not m:
        return {}, text.lstrip("﻿")
    if len(m.group(1)) > MAX_FRONT:
        raise BadRequest("the SKILL.md front matter is too long")
    try:
        data = yaml.load(m.group(1), Loader=_PlainLoader) or {}
    except yaml.YAMLError:
        raise BadRequest("the SKILL.md front matter is not valid YAML") from None
    if not isinstance(data, dict):
        raise BadRequest("the SKILL.md front matter must be a list of fields")
    return {str(k): v for k, v in data.items()}, text[m.end():]


def render(key: str, description: str, body: str, allowed_tools=None) -> str:
    """The SKILL.md a Claude Code agent reads: front matter (name, description, optional allowed-tools) then the body."""
    front = {"name": key, "description": " ".join((description or "").split())}
    if allowed_tools:
        front["allowed-tools"] = ", ".join(allowed_tools)
    head = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=10_000, default_flow_style=False).strip()
    return f"---\n{head}\n---\n\n{(body or '').lstrip(chr(10))}".rstrip("\n") + "\n"


def spdx_of(name: str) -> str | None:
    """'MIT', 'Apache 2.0', 'bsd-3-clause' -> the SPDX id when it is one of the permissive licences, else None."""
    return _NAME_TO_SPDX.get(" ".join((name or "").lower().split()).rstrip("."))


def detect_license(front_license: str | None, license_text: str | None) -> dict:
    """What licence a skill carries: {"status": "permissive" | "unknown" | "refused", "spdx": str, "evidence": str}.
    A licence FILE beats a front-matter word (a front matter that says 'see LICENSE.txt' is no licence by itself). A recognised
    permissive licence is `permissive`; a recognised other one (GPL, a non-commercial CC, all rights reserved) is `refused`;
    nothing recognisable is `unknown` (the owner must confirm before the skill is used)."""
    text = (license_text or "")[:60_000]
    if text.strip():
        for rx, label in _NOT_PERMISSIVE:
            if rx.search(text):
                return {"status": "refused", "spdx": label, "evidence": "licence file"}
        for rx, spdx in _PERMISSIVE_TEXT:
            if rx.search(text):
                return {"status": "permissive", "spdx": spdx, "evidence": "licence file"}
        if _ALL_RIGHTS.search(text):
            return {"status": "refused", "spdx": "All rights reserved", "evidence": "licence file"}
    word = str(front_license or "").strip()
    if word:
        spdx = spdx_of(word)
        if spdx:
            return {"status": "permissive", "spdx": spdx, "evidence": "SKILL.md front matter"}
        low = word.lower()
        if re.search(r"\b(gpl|agpl|lgpl|mpl|proprietary|commercial|noncommercial|non-commercial|all rights reserved)\b", low) \
                or re.search(r"cc[ -]by[ -](nc|nd|sa)", low):
            return {"status": "refused", "spdx": word[:64], "evidence": "SKILL.md front matter"}
    return {"status": "unknown", "spdx": "", "evidence": ""}
