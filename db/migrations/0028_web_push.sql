-- Phone notifications (#487, owner 2026-10-19): the dashboard, installed as a PWA on the owner's phone, is where estate
-- (Mallen's own servers) problems are pushed; the ERP sends none. app/push.py sends Web Push (VAPID); the rule is q292.

-- One row per browser push subscription (a PushSubscription: endpoint + its two keys). No app login yet (#394), so
-- every device is the owner's. A 404/410 from the push service deletes the row.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    user_agent  TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ok_at  TIMESTAMPTZ,
    last_error  TEXT
);

-- The sweep's flag: when this problem (this occurrence: a drift finding that comes back resets started_at) was put in a
-- push. The dispatcher sweeps for open rows without it; it never relies on a NOTIFY.
ALTER TABLE health_problems ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ;
-- Everything already open when push arrives counts as seen, as a first sync's baseline does: switching push on (or
-- subscribing the first phone) must not fire a burst for problems the owner already has on the Health page.
UPDATE health_problems SET pushed_at = now() WHERE pushed_at IS NULL AND resolved_at IS NULL;

-- One row per notification GROUP: problems that qualified within CREWBOSS_PUSH_GROUP_SECONDS of its first push share it (one
-- notification, updated quietly in place). members = what it named, so the one quiet "resolved" notification can go
-- when every one of them has cleared (resolved_sent_at).
CREATE TABLE IF NOT EXISTS estate_pushes (
    id                BIGSERIAL PRIMARY KEY,
    opened_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    members           JSONB NOT NULL DEFAULT '[]',
    result            TEXT,
    resolved_sent_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS estate_pushes_open ON estate_pushes (id) WHERE resolved_sent_at IS NULL;
