-- #91 review: "Use my runner for task summaries", a per-person setting. ON by default; OFF = the task summary is the extractive auto summary only
-- and none of this person's views queues a summarise turn on their runner.
ALTER TABLE users ADD COLUMN IF NOT EXISTS use_runner_summaries BOOLEAN NOT NULL DEFAULT true;
