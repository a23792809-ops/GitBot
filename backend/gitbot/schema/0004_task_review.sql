-- Migration 0004: store the reviewed diff and AI-response size metadata.
--
-- The reviewed patch is persisted so the task detail endpoint can show exactly
-- what an approval is bound to without touching the repository on a GET, and so
-- the approval diff hash stays verifiable after the working tree changes.

ALTER TABLE tasks ADD COLUMN diff_patch TEXT;
ALTER TABLE tasks ADD COLUMN diff_files TEXT;
ALTER TABLE tasks ADD COLUMN diff_truncated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN output_truncated INTEGER NOT NULL DEFAULT 0;
