-- Auto-rescan (scanner/auto_rescan.py): a narrow 'scanner' ROLE token that may only import catalogs
-- (app/auth.py require_catalog_importer), and a 'broken' catalog diff for annotated workflows whose
-- references no longer resolve (the scanner's per-workflow `problems`, docs/catalog-schema.md).

ALTER TABLE session_tokens DROP CONSTRAINT IF EXISTS session_tokens_stream_check;
ALTER TABLE session_tokens ADD CONSTRAINT session_tokens_stream_check
    CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors', 'scanner'));

ALTER TABLE catalog_diffs DROP CONSTRAINT IF EXISTS catalog_diffs_change_type_check;
ALTER TABLE catalog_diffs ADD CONSTRAINT catalog_diffs_change_type_check
    CHECK (change_type IN ('added', 'removed', 'modified', 'stale', 'broken'));
