-- Task #129 audit fixes.
-- plan_stale is addressed to the overseer project's ONE 'all' session explicitly (stream 'all'), so adding a
-- features session to that project later can't misroute it. A stream session never selects stream 'all'.
ALTER TABLE outbox DROP CONSTRAINT IF EXISTS outbox_stream_check;
ALTER TABLE outbox ADD CONSTRAINT outbox_stream_check
    CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors', 'all'));

-- Re-plan cooldown: changes inside the cooldown are remembered here and sent as ONE follow-up nudge after it.
ALTER TABLE global_plan_state ADD COLUMN IF NOT EXISTS pending_since TIMESTAMPTZ;
ALTER TABLE global_plan_state ADD COLUMN IF NOT EXISTS pending_why TEXT;

-- Planning isn't atomic (dash reads get_global_view, then writes set_global_plan): view_at = dash's latest look; each
-- plan run records the look it was based on, and only a plan based on a look AFTER the pending changes clears them.
ALTER TABLE global_plan_state ADD COLUMN IF NOT EXISTS view_at TIMESTAMPTZ;
ALTER TABLE global_plan_runs ADD COLUMN IF NOT EXISTS view_at TIMESTAMPTZ;
