from app import tokens
from app.auth import resolve_token


def test_tokens_cli_creates_a_usable_token(capsys, project):
    rc = tokens.main(["create", "--stream", "bugs", "--name", "cli-test"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("cb_")]
    assert len(lines) == 1
    session = resolve_token(lines[0])
    assert session["stream"] == "bugs"
    assert session["name"] == "cli-test"


def test_only_hash_is_stored(project):
    from app.db import get_cursor

    rc_token = None
    import io
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        tokens.main(["create", "--stream", "features"])
    finally:
        sys.stdout = old
    printed = buf.getvalue()
    plaintext = [l for l in printed.splitlines() if l.startswith("cb_")][0]

    with get_cursor() as cur:
        cur.execute("SELECT token_hash FROM session_tokens WHERE stream = 'features'")
        row = cur.fetchone()
    assert plaintext not in row["token_hash"]
    assert row["token_hash"] != plaintext
