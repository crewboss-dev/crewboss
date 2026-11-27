-- Tracks, per (stream, project), when that Claude session last called ground(). Powers "recently
-- changed tasks and owner comments since it last grounded" in the briefing.
CREATE TABLE IF NOT EXISTS ground_checkpoints (
    id            SERIAL PRIMARY KEY,
    stream        TEXT NOT NULL CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors')),
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    checkpoint_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (stream, project_id)
);
