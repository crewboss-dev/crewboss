"""Fixture mimicking app/decisions.py's @executor / @inline_handler / produce_* shape."""
EXECUTORS = {}
INLINE = {}


def executor(dtype):
    def deco(fn):
        EXECUTORS[dtype] = fn
        return fn
    return deco


def inline_handler(dtype):
    def deco(fn):
        INLINE[dtype] = fn
        return fn
    return deco


@executor("fixture.thing.execute")
def _execute_fixture(cur, decision_id, action):
    """Fixture executor."""
    cur.execute("INSERT INTO fixture_decisions (id) VALUES (%s)", (decision_id,))
    return {}


@inline_handler("fixture.inline.apply")
def _apply_fixture_inline(cur, subject_id, action, params):
    """Fixture inline handler."""
    cur.execute("UPDATE fixture_things SET applied=true WHERE id=%s", (subject_id,))
    return {}


def produce_fixture_decisions(conn):
    """Fixture producer."""
    return 0
