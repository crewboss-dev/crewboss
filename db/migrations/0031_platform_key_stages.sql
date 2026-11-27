-- Platform keys (#562, 1143): the last stage the Dashboard saw for each key of each key store, so a stage change
-- (ok -> due_30 -> due_7 -> overdue, the store's #563 clock) raises its Health reminder and phone notification ONCE.
-- Metadata only, read from the store's status document: a key NAME, never a value (the store never sends one).
-- The reminder itself is a health_problems row (source 'keys', eventid = a stable hash of '<store>:<NAME>').
CREATE TABLE IF NOT EXISTS platform_key_stages (
    store       TEXT NOT NULL,                  -- the dashboard project slug the store belongs to (config)
    name        TEXT NOT NULL,                  -- the key's NAME
    stage       TEXT NOT NULL,                  -- ok | due_30 | due_7 | overdue (or what the store sent)
    next_due    DATE,
    days_left   INTEGER,
    seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store, name)
);
