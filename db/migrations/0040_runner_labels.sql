-- Runner labels (task #627, change #50, phase P4b): a device carries labels the OWNER sets on the Runners page (the runner
-- can never set its own), a profile says which labels its jobs need, and a job copies them when it is created (plus any
-- added on the Run form). A job is claimable only by a device whose labels contain all of the job's (requires_labels <@ labels).
-- First use: Android jobs go only to the PC with the phone attached (label android-usb).
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS labels TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS requires_labels TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS requires_labels TEXT[] NOT NULL DEFAULT '{}';

INSERT INTO agent_profiles (name, model, description, skills, allowed_tools, task_types, requires_labels, unattended_ok) VALUES
 ('android', 'sonnet', 'Android work (Kotlin, adb sideloading) on the PC with the phone attached', '{}',
  '{Read,Edit,Write,Glob,Grep,Bash}', '{android}', '{android-usb}', false)
ON CONFLICT (name) DO NOTHING;
