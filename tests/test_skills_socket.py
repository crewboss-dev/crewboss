"""Skills over the runner's verified socket (task #718): the claim reply carries the bundle, and the runner's `skills` frame marks what it
wrote. A stale fence or another device changes nothing."""
import json

import pytest

from app import access, asset_skills, config, skill_delivery
from app.db import get_cursor

from test_devices import _secret, est  # noqa: F401  (fixtures)
from test_jobs import _queue, world  # noqa: F401  (fixtures)
from test_runner_socket_jobs import _enrol_alice, _rpc
from test_runner_ws import Runner
from test_web_auth import auth_on  # noqa: F401


@pytest.fixture(autouse=True)
def _assets_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ASSETS_DIR", str(tmp_path / "assets"))


def test_the_claim_reply_carries_skills_and_the_runner_reports_what_it_wrote(world, client):
    asset_skills.create(access.OWNER_PRINCIPAL, name="Voice", key="voice", description="d", body="Short words.", install_to=None,
                        files=[{"path": "notes.md", "raw": b"n"}])
    dev, key = _enrol_alice(world, "sock-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        job = _queue(world)
        reply = _rpc(r, type="claim", id=1)
        claim = reply["job"]
        assert claim["job_id"] == job["id"] and [s["key"] for s in claim["skills"]["skills"]] == ["voice"]
        s = claim["skills"]["skills"][0]
        assert s["skill_md"].startswith("---\nname: voice") and [f["path"] for f in s["files"]] == ["notes.md"] and s["n"] == 1
        assert "SECRET TASK TEXT" not in json.dumps(reply)
        got = _rpc(r, type="skills", id=2, job_id=claim["job_id"], fence=claim["fence"], written=["voice"], skipped=[])
        assert got["type"] == "skills_result" and got["ok"] is True
        assert {u["key"]: u["state"] for u in skill_delivery.uses_for_jobs([job["id"]])[job["id"]]} == {"voice": "delivered"}
        stale = _rpc(r, type="skills", id=3, job_id=claim["job_id"], fence=claim["fence"] + 5, written=["voice"], skipped=[])
        assert stale["ok"] is False and stale["status"] == 409
        bad = _rpc(r, type="skills", id=4, job_id="x", fence=claim["fence"], written=[], skipped=[])
        assert bad["ok"] is False and bad["status"] == 400
        junk = _rpc(r, type="skills", id=5, job_id=claim["job_id"], fence=claim["fence"], written="not a list", skipped={"a": 1})
        assert junk["ok"] is True
    finally:
        r.close()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state FROM jobs WHERE id = %s", (job["id"],))
        assert cur.fetchone()["state"] == "running"
