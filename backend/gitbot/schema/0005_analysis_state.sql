-- Migration 0005: terminal read-only analysis state and sandbox metadata.
--
-- SQLite cannot alter a CHECK constraint in place, so the tasks table is
-- rebuilt: create the new shape, copy every row, drop the old table, rename.
-- The migration runner turns foreign-key enforcement off around migrations and
-- verifies `PRAGMA foreign_key_check` before committing, because approvals and
-- task_events reference tasks(id).
--
-- No repository is touched and no task is re-run by this migration. Existing
-- rows keep their state; `sandboxed` defaults to 0 for rows written before the
-- sandbox existed.

CREATE TABLE tasks_new (
    id                TEXT PRIMARY KEY,
    repo_id           INTEGER NOT NULL REFERENCES repos(id) ON DELETE RESTRICT,
    task_type         TEXT    NOT NULL CHECK (task_type IN ('analyze','bugfix','feature','docs','deps','tests')),
    state             TEXT    NOT NULL CHECK (state IN ('queued','running','awaiting_review','analyzed','approved','committed','failed','cancelled','expired')),
    prompt            TEXT    NOT NULL,
    model             TEXT,
    session_id        TEXT,
    branch_name       TEXT,
    diff_hash         TEXT,
    approval_id       TEXT,
    error_message     TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    output            TEXT,
    diff_patch        TEXT,
    diff_files        TEXT,
    diff_truncated    INTEGER NOT NULL DEFAULT 0,
    output_truncated  INTEGER NOT NULL DEFAULT 0,
    sandboxed         INTEGER NOT NULL DEFAULT 0 CHECK (sandboxed IN (0, 1)),
    snapshot_head     TEXT
);

INSERT INTO tasks_new (
    id, repo_id, task_type, state, prompt, model, session_id, branch_name,
    diff_hash, approval_id, error_message, created_at, updated_at, started_at,
    finished_at, output, diff_patch, diff_files, diff_truncated,
    output_truncated, sandboxed, snapshot_head
)
SELECT
    id, repo_id, task_type, state, prompt, model, session_id, branch_name,
    diff_hash, approval_id, error_message, created_at, updated_at, started_at,
    finished_at, output, diff_patch, diff_files, diff_truncated,
    output_truncated, 0, NULL
FROM tasks;

DROP TABLE tasks;

ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX idx_tasks_repo_id ON tasks (repo_id);
CREATE INDEX idx_tasks_state ON tasks (state);
CREATE INDEX idx_tasks_created_at ON tasks (created_at);
