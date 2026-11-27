"""Live updates (task #177): the /all/events SSE stream and its change-cursor.

The stream pushes a `changed` ping (carrying the latest activity id) whenever the board advances; open
tabs then dispatch `mdd:board-stale` and every panel refetches. These tests cover the change-cursor
function on its own (no infinite loop), the endpoint's content-type + initial frames, that a `changed`
event arrives after an activity row is added, that no DB connection is leaked by the cursor, and that the
client wiring (base.html loads live.js; the partials still carry the trigger) is in place."""
import asyncio

import pytest

from app import repo
from app.db import get_cursor


# --- the change-cursor, tested directly (no stream, no loop) --------------------------------------------------

def test_activity_cursor_is_int_and_rises_on_a_board_write(project):
    before = repo.activity_cursor()
    assert isinstance(before, int)  # a monotonic int (0 on an empty board), never None
    repo.create_task(project["id"], "A task", "", "should", "feature", [], {}, "claude:features")
    after = repo.activity_cursor()
    assert after > before  # a board write appended an activity row -> the cursor advanced ("changed since X")


def test_activity_cursor_is_zero_on_an_empty_board(project):
    with get_cursor() as cur:
        cur.execute("DELETE FROM activity")
    assert repo.activity_cursor() == 0  # COALESCE(MAX(id), 0), never None


def test_activity_cursor_does_not_leak_connections(project):
    """The cursor opens a short-lived read-only connection per call and closes it — nothing left open."""
    def _open_backends():
        with get_cursor(commit=False) as cur:
            cur.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE datname = current_database() AND state IS NOT NULL"
            )
            return cur.fetchone()["n"]

    baseline = _open_backends()
    for _ in range(25):
        repo.activity_cursor()
    assert _open_backends() <= baseline + 1  # +1 slack for this test's own connection; no accumulation


# --- the endpoint --------------------------------------------------------------------------------------------
#
# The stream is an infinite loop, so we drive its async generator directly (stepping it frame by frame with a
# fake request) rather than through the test client, which would block waiting for an end that never comes.

class _FakeRequest:
    """Enough of a Starlette Request for the stream: an is_disconnected that reports 'still connected'."""
    def __init__(self, disconnected=False):
        self._disconnected = disconnected

    async def is_disconnected(self):
        return self._disconnected


def test_events_endpoint_is_an_event_stream_with_no_buffering():
    """The route returns a text/event-stream response with the headers a proxy needs to not buffer it."""
    from app.routers import pages

    resp = asyncio.run(pages.board_events(_FakeRequest()))
    assert resp.media_type == "text/event-stream"
    assert resp.headers.get("cache-control") == "no-cache"
    assert resp.headers.get("x-accel-buffering") == "no"  # a reverse proxy must not buffer the stream


def test_stream_opens_with_a_retry_hint_and_heartbeat():
    """The first two frames establish the connection before any DB poll: a retry hint + an opening comment."""
    from app.routers import pages

    async def first_two():
        gen = pages._board_event_stream(_FakeRequest())
        return await gen.__anext__(), await gen.__anext__()

    f1, f2 = asyncio.run(first_two())
    assert f1 == b"retry: 5000\n\n"      # tells EventSource how long to wait before reconnecting
    assert f2.startswith(b": ")          # an SSE comment -> browser onopen fires, proxies flush headers


def test_stream_emits_changed_after_an_activity_row_is_added(project, monkeypatch):
    from app.routers import pages

    monkeypatch.setattr(pages, "EVENTS_POLL_SECONDS", 0.02)  # fast poll so the test is quick, not flaky

    async def scenario():
        gen = pages._board_event_stream(_FakeRequest())
        await gen.__anext__()  # retry
        await gen.__anext__()  # : connected  -> next __anext__ reads the initial cursor, then polls
        # Add a row AFTER the stream has read its initial cursor (0.2s >> the 0.02s poll), so the next
        # poll sees the board advance and the generator yields a `changed` frame.
        async def add_row_soon():
            await asyncio.sleep(0.2)
            await asyncio.to_thread(
                repo.create_task, project["id"], "Live task", "", "should", "feature", [], {}, "claude:features")

        adder = asyncio.create_task(add_row_soon())
        frame = await asyncio.wait_for(gen.__anext__(), 5)
        await adder
        await gen.aclose()
        return frame

    frame = asyncio.run(scenario())
    text = frame.decode()
    assert text.startswith("event: changed\n")
    # the frame carries the new activity id so the client can de-dupe a replayed frame on reconnect
    data = text.split("data:", 1)[1].strip()
    assert int(data) == repo.activity_cursor() and int(data) > 0


# --- client wiring (static assertions over templates) --------------------------------------------------------

def test_base_html_loads_the_live_script():
    html = open("app/templates/base.html", encoding="utf-8").read()
    assert "js/live.js" in html


def test_live_script_opens_the_events_stream_and_dispatches_board_stale():
    js = open("app/static/js/live.js", encoding="utf-8").read()
    assert "/all/events" in js
    assert "mdd:board-stale" in js
    assert "EventSource" in js


@pytest.mark.parametrize("partial", [
    "board_status.html", "focus.html", "global_plan.html", "tasklist.html",
])
def test_partials_still_carry_the_board_stale_trigger(partial):
    """The client plumbing this feature drives — untouched. (triage_banner has no trigger of its own; it
    refreshes transitively inside board_status / tasklist, which do.)"""
    html = open(f"app/templates/partials/{partial}", encoding="utf-8").read()
    assert "mdd:board-stale from:body" in html
