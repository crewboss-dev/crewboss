-- Instance-specific URL clean-up from the original installation, now a no-op in the public release.
-- The file keeps its name so the migration sequence stays intact; a fresh install has no rows to change.
SELECT 1;
