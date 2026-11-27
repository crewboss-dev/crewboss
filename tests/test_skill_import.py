"""Importing a community skill (task #718): the host allow-list and redirects, the size caps, zip safety, front matter, and the licence
gate. The network is replaced (skill_import.http_get / httpx.Client): nothing here reaches the internet."""
import io
import stat
import zipfile

import httpx
import pytest

from app import skill_import, skill_md
from app.repo import BadRequest

MIT = """MIT License

Copyright (c) 2025 Someone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction.
"""
GPL = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n"
SKILL = "---\nname: pretty-docs\ndescription: Make documents read well. Use when writing client text.\n---\n\n# Steps\n\nRead references/tone.md first.\n"


def zipped(files: dict, **special) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
        for name, info in special.items():
            z.writestr(info, b"x")
    return buf.getvalue()


# --- the address ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url,expect", [
    ("https://github.com/acme/skills/tree/main/pretty-docs", {"owner": "acme", "repo": "skills", "ref": "main", "dir": "pretty-docs"}),
    ("https://github.com/acme/skills/blob/v1.2/skills/pretty-docs/SKILL.md", {"owner": "acme", "repo": "skills", "ref": "v1.2", "dir": "skills/pretty-docs"}),
    ("https://raw.githubusercontent.com/acme/skills/main/pretty-docs/SKILL.md", {"owner": "acme", "repo": "skills", "ref": "main", "dir": "pretty-docs"}),
    ("https://github.com/acme/skills", {"owner": "acme", "repo": "skills", "ref": "HEAD", "dir": ""}),
    ("https://github.com/acme/skills.git", {"owner": "acme", "repo": "skills", "ref": "HEAD", "dir": ""}),
])
def test_github_addresses_that_are_accepted(url, expect):
    assert skill_import.parse_github_url(url) == expect


@pytest.mark.parametrize("url", [
    "http://github.com/acme/skills",                                  # not https
    "https://gist.github.com/acme/abc",                               # another host
    "https://evil.example/acme/skills",
    "https://github.com.evil.example/acme/skills",                    # a look-alike
    "https://raw.githubusercontent.com.evil.example/a/b/main/SKILL.md",
    "https://user:pw@github.com/acme/skills",                         # user-info
    "https://github.com:8443/acme/skills",                            # another port
    "file:///etc/passwd", "ftp://github.com/a/b", "git@github.com:acme/skills.git", "javascript:alert(1)", "",
    "https://github.com/acme",                                        # no repo
    "https://github.com/acme/skills/tree/main/../../etc",             # path climbing
    "https://github.com/acme/skills/tree/main/a%2F..%2Fb",
    "https://github.com/acme/skills/issues/1",                        # not a folder or file
])
def test_github_addresses_that_are_refused(url):
    with pytest.raises(BadRequest):
        skill_import.parse_github_url(url)


REAL_CLIENT = httpx.Client


def _client_with(handler, monkeypatch):
    real = REAL_CLIENT
    monkeypatch.setattr(skill_import, "_public", lambda host: None)
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}))


def test_a_redirect_off_the_allowed_hosts_is_refused(monkeypatch):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})
    _client_with(handler, monkeypatch)
    with pytest.raises(BadRequest, match="not github.com"):
        skill_import.http_get("https://raw.githubusercontent.com/a/b/main/SKILL.md", 1000)
    assert seen == ["https://raw.githubusercontent.com/a/b/main/SKILL.md"]        # the second hop was never requested


def test_a_redirect_to_http_or_another_port_is_refused(monkeypatch):
    for target in ("http://github.com/a/b", "https://github.com:81/a/b", "https://user@github.com/a/b"):
        _client_with(lambda req, t=target: httpx.Response(301, headers={"location": t}), monkeypatch)
        with pytest.raises(BadRequest):
            skill_import.http_get("https://github.com/a/b", 1000)


def test_a_redirect_between_the_allowed_hosts_is_followed_and_loops_stop(monkeypatch):
    def handler(req):
        if req.url.host == "github.com":
            return httpx.Response(301, headers={"location": "https://raw.githubusercontent.com/a/b/main/SKILL.md"})
        return httpx.Response(200, content=b"hello")
    _client_with(handler, monkeypatch)
    assert skill_import.http_get("https://github.com/a/b", 1000) == (200, b"hello")
    _client_with(lambda req: httpx.Response(302, headers={"location": "https://github.com/loop"}), monkeypatch)
    with pytest.raises(BadRequest, match="too many redirects"):
        skill_import.http_get("https://github.com/loop", 1000)


def test_a_body_over_the_cap_is_refused_while_it_streams(monkeypatch):
    _client_with(lambda req: httpx.Response(200, content=b"x" * 5000), monkeypatch)
    with pytest.raises(BadRequest, match="over"):
        skill_import.http_get("https://raw.githubusercontent.com/a/b/main/SKILL.md", 1000)


def test_a_host_that_resolves_to_a_private_address_is_refused(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
    with pytest.raises(BadRequest, match="private"):
        skill_import.http_get("https://github.com/a/b", 1000)


# --- GitHub: what is fetched ------------------------------------------------------------------------------------------------

def _fake_net(monkeypatch, files: dict, log: list | None = None):
    def get(url, limit):
        if log is not None:
            log.append(url)
        path = url.split("/main/", 1)[1]
        return (200, files[path]) if path in files else (404, b"")
    monkeypatch.setattr(skill_import, "http_get", get)


def test_a_github_import_reads_skill_licence_and_the_files_it_names(monkeypatch):
    log = []
    _fake_net(monkeypatch, {"pretty-docs/SKILL.md": SKILL.encode(), "LICENSE": MIT.encode(), "pretty-docs/references/tone.md": b"Plain words."}, log)
    got = skill_import.prepare_github("https://github.com/acme/skills/tree/main/pretty-docs")
    assert got.key == "pretty-docs" and got.license["status"] == "permissive" and got.license["spdx"] == "MIT"
    assert [f["path"] for f in got.files] == ["LICENSE", "references/tone.md"] and got.body.startswith("# Steps")
    assert got.source["via"] == "github" and got.source["author"] == "acme" and got.source["ref"] == "main"
    assert got.source["url"] == "https://github.com/acme/skills/tree/main/pretty-docs"
    assert all(u.startswith("https://raw.githubusercontent.com/acme/skills/main/") for u in log)       # only the raw host is ever asked


def test_a_missing_skill_file_and_a_missing_named_file(monkeypatch):
    _fake_net(monkeypatch, {})
    with pytest.raises(BadRequest, match="no SKILL.md"):
        skill_import.prepare_github("https://github.com/acme/skills/tree/main/nope")
    _fake_net(monkeypatch, {"s/SKILL.md": SKILL.encode(), "s/LICENSE": MIT.encode()})
    got = skill_import.prepare_github("https://github.com/acme/skills/tree/main/s")
    assert any("references/tone.md" in w for w in got.warnings) and got.license["status"] == "permissive"


# --- the licence gate -------------------------------------------------------------------------------------------------------

def test_no_licence_is_flagged_unknown_and_kept_with_a_note(monkeypatch):
    _fake_net(monkeypatch, {"s/SKILL.md": SKILL.encode()})
    got = skill_import.prepare_github("https://github.com/acme/skills/tree/main/s")
    assert got.license["status"] == "unknown" and any("licence" in w.lower() for w in got.warnings)
    assert got.fields()["license"]["status"] == "unknown" and got.source["notes"]


def test_a_copyleft_or_restricted_licence_is_refused(monkeypatch):
    for text in (GPL, "Creative Commons Attribution-NonCommercial 4.0", "Mozilla Public License Version 2.0", "Copyright X. All rights reserved."):
        _fake_net(monkeypatch, {"s/SKILL.md": SKILL.encode(), "s/LICENSE": text.encode()})
        with pytest.raises(BadRequest, match="not a permissive licence"):
            skill_import.prepare_github("https://github.com/acme/skills/tree/main/s")


def test_the_front_matter_licence_word_counts_only_when_there_is_no_file():
    assert skill_md.detect_license("MIT", "")["status"] == "permissive"
    assert skill_md.detect_license("Apache 2.0", None)["spdx"] == "Apache-2.0"
    assert skill_md.detect_license("Proprietary", "")["status"] == "refused"
    assert skill_md.detect_license("Complete terms in LICENSE.txt", "")["status"] == "unknown"
    assert skill_md.detect_license("MIT", GPL)["status"] == "refused"            # the file wins


@pytest.mark.parametrize("name,text,spdx", [
    ("mit", MIT, "MIT"),
    ("mit with all rights reserved in the notice", "Copyright (c) 2024 A. All rights reserved.\n" + MIT, "MIT"),
    ("apache", "Apache License\nVersion 2.0, January 2004", "Apache-2.0"),
    ("bsd2", "Redistribution and use in source and binary forms, with or without\nmodification, are permitted provided that the following conditions are met:", "BSD-2-Clause"),
    ("bsd3", "Redistribution and use in source and binary forms, with or without\nmodification, are permitted provided that the following conditions are met: 3. Neither the name of X", "BSD-3-Clause"),
    ("isc", "Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above", "ISC"),
    ("0bsd", "Zero-Clause BSD\n=============", "0BSD"),
    ("unlicense", "This is free and unencumbered software released into the public domain.", "Unlicense"),
    ("cc0", "CC0 1.0 Universal", "CC0-1.0"),
])
def test_each_permissive_licence_is_recognised(name, text, spdx):
    got = skill_md.detect_license(None, text)
    assert (got["status"], got["spdx"]) == ("permissive", spdx), name


def test_front_matter_needs_a_description_and_a_name():
    with pytest.raises(BadRequest, match="description"):
        skill_import.parse_folder({"SKILL.md": b"---\nname: x\n---\nbody"}, {})
    with pytest.raises(BadRequest, match="name"):
        skill_import.parse_folder({"SKILL.md": b"---\ndescription: x\n---\nbody"}, {})
    with pytest.raises(BadRequest, match="valid YAML"):
        skill_import.parse_folder({"SKILL.md": b"---\nname: [unclosed\n---\nbody"}, {})
    ok = skill_import.parse_folder({"SKILL.md": b"---\ndescription: x\n---\nbody"}, {}, fallback_name="From File")
    assert ok.key == "from-file"


def test_front_matter_is_data_not_code():
    evil = b"---\nname: ok\ndescription: !!python/object/apply:os.system ['echo pwned']\n---\nbody"
    with pytest.raises(BadRequest):
        skill_import.parse_folder({"SKILL.md": evil}, {})


# --- an upload ----------------------------------------------------------------------------------------------------------------

def test_a_single_skill_md_upload_and_a_wrapped_zip():
    one = skill_import.prepare_upload("pretty-docs.md", SKILL.encode())
    assert one.key == "pretty-docs" and one.source["via"] == "upload" and one.license["status"] == "unknown"
    z = skill_import.prepare_upload("x.zip", zipped({"my-skill/SKILL.md": SKILL, "my-skill/LICENSE": MIT, "my-skill/scripts/run.py": "print(1)"}))
    assert z.license["status"] == "permissive" and sorted(f["path"] for f in z.files) == ["LICENSE", "scripts/run.py"]
    top = skill_import.prepare_upload("x.zip", zipped({"SKILL.md": SKILL, "notes.txt": "hi"}))
    assert [f["path"] for f in top.files] == ["notes.txt"]


def test_zip_with_hostile_paths_is_refused():
    for name in ("/etc/passwd", "../evil.txt", "a/../../evil.txt", "C:/evil.txt", "a\\b.txt"):
        with pytest.raises(BadRequest):
            skill_import.safe_unzip(zipped({"SKILL.md": SKILL, name: "x"}))


def test_zip_with_a_symlink_a_bomb_too_many_files_or_garbage_is_refused():
    link = zipfile.ZipInfo("link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(BadRequest, match="symbolic link"):
        skill_import.safe_unzip(zipped({"SKILL.md": SKILL}, link=link))
    with pytest.raises(BadRequest, match="over"):
        skill_import.safe_unzip(zipped({"SKILL.md": SKILL, "big.txt": "a" * (skill_import.MAX_FILE + 1)}))       # compresses to a few bytes
    with pytest.raises(BadRequest, match="unpacks to more"):
        skill_import.safe_unzip(zipped({"SKILL.md": SKILL, **{f"f{i}.txt": "b" * (skill_import.MAX_FILE - 1) for i in range(6)}}))
    with pytest.raises(BadRequest, match="more than"):
        skill_import.safe_unzip(zipped({f"f{i}.txt": "x" for i in range(skill_import.MAX_ZIP_ENTRIES + 1)}))
    with pytest.raises(BadRequest, match="not a zip"):
        skill_import.safe_unzip(b"PK\x03\x04 this is not really a zip")
    with pytest.raises(BadRequest, match="over"):
        skill_import.safe_unzip(b"PK" + b"0" * (skill_import.MAX_ZIP + 1))


def test_a_lying_zip_header_cannot_inflate_past_the_cap(monkeypatch):
    raw = zipped({"SKILL.md": SKILL, "big.txt": "a" * (skill_import.MAX_FILE + 10)})
    real = zipfile.ZipFile.infolist

    def lying(self):
        infos = real(self)
        for i in infos:
            if i.filename == "big.txt":
                i.file_size = 10                     # the header says 10 bytes
        return infos
    monkeypatch.setattr(zipfile.ZipFile, "infolist", lying)
    with pytest.raises(BadRequest, match="damaged|over"):
        skill_import.safe_unzip(raw)


def test_a_zip_with_two_skills_or_none_or_too_many_other_files_is_refused():
    with pytest.raises(BadRequest, match="one skill"):
        skill_import.prepare_upload("x.zip", zipped({"a/SKILL.md": SKILL, "b/SKILL.md": SKILL}))
    with pytest.raises(BadRequest, match="no SKILL.md"):
        skill_import.prepare_upload("x.zip", zipped({"a/readme.md": "x"}))
    with pytest.raises(BadRequest, match="most a skill takes"):
        skill_import.prepare_upload("x.zip", zipped({"SKILL.md": SKILL, **{f"f{i}.txt": "x" for i in range(40)}}))


def test_odd_file_names_are_left_out_with_a_warning_and_text_must_be_utf8():
    got = skill_import.prepare_upload("x.zip", zipped({"SKILL.md": SKILL, "with space.txt": "x", "ok.txt": "y"}))
    assert [f["path"] for f in got.files] == ["ok.txt"] and any("with space.txt" in w for w in got.warnings)
    with pytest.raises(BadRequest, match="UTF-8"):
        skill_import.prepare_upload("SKILL.md", b"\xff\xfe\x00bad")


def test_referenced_files_are_read_from_the_body_text_only():
    body = "See references/a.md and ./scripts/run.py, https://example.com/x/y.md and ../../etc/passwd.txt and docs/b.txt."
    assert skill_import.referenced_files(body) == ["references/a.md", "scripts/run.py", "docs/b.txt"]


def test_an_imported_skill_never_pre_approves_tools():
    md = b"---\nname: x\ndescription: d\nallowed-tools: Bash, Write\n---\nbody"
    got = skill_import.prepare_upload("x.md", md)
    assert got.allowed_tools == [] and "allowed_tools" not in got.fields() and any("allowed-tools" in w for w in got.warnings)


def test_a_yaml_alias_bomb_is_refused_not_expanded():
    bomb = "---\nname: x\ndescription: d\nlol: &a [1,1,1,1,1,1,1,1,1]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\nc: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\nd: [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n---\nbody"
    with pytest.raises(BadRequest, match="front matter"):
        skill_import.parse_folder({"SKILL.md": bomb.encode()}, {})
    with pytest.raises(BadRequest, match="front matter"):
        skill_md.parse("---\nname: x\nk: &anchor value\n---\nbody")
    assert skill_md.parse("---\nname: x\nk: plain\n---\nbody")[0] == {"name": "x", "k": "plain"}


def test_a_github_import_has_a_total_time_budget(monkeypatch):
    import time
    monkeypatch.setattr(skill_import, "TOTAL_SECONDS", 0.05)

    def slow(url, limit):
        time.sleep(0.06)
        return 200, SKILL.encode()
    monkeypatch.setattr(skill_import, "http_get", slow)
    with pytest.raises(BadRequest, match="took longer"):
        skill_import.prepare_github("https://github.com/acme/skills/tree/main/pretty-docs")
    assert skill_import.TIMEOUT <= 5.0 and skill_import.TOTAL_SECONDS == 0.05
