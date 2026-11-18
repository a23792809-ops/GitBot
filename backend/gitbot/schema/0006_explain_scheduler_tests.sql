-- Migration 0006: explain tasks, per-repository task policy, scheduling/retries,
-- and sandboxed test-run results.
--
-- SQLite cannot add values to an existing CHECK constraint, so `tasks` is
-- rebuilt. The migration runner disables foreign-key enforcement around the
-- rebuild and verifies `PRAGMA foreign_key_check` before committing. Existing
-- rows are copied unchanged; all new columns have safe defaults.
--
-- `repos` only gains nullable/defaulted columns, so it can be altered in place.

CREATE TABLE tasks_new (
    id                    TEXT PRIMARY KEY,
    repo_id               INTEGER NOT NULL REFERENCES repos(id) ON DELETE RESTRICT,
    task_type             TEXT    NOT NULL CHECK (task_type IN ('analyze','explain','bugfix','feature','docs','deps','tests')),
    state                 TEXT    NOT NULL CHECK (state IN ('queued','running','awaiting_review','analyzed','explained','approved','committed','failed','cancelled','expired')),
    prompt                TEXT    NOT NULL,
    model                 TEXT,
    session_id            TEXT,
    branch_name           TEXT,
    diff_hash             TEXT,
    approval_id           TEXT,
    error_message         TEXT,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    started_at            TEXT,
    finished_at           TEXT,
    output                TEXT,
    diff_patch            TEXT,
    diff_files            TEXT,
    diff_truncated        INTEGER NOT NULL DEFAULT 0,
    output_truncated      INTEGER NOT NULL DEFAULT 0,
    sandboxed             INTEGER NOT NULL DEFAULT 0 CHECK (sandboxed IN (0, 1)),
    snapshot_head         TEXT,
    scheduled_at          TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    max_retries           INTEGER NOT NULL DEFAULT 0 CHECK (max_retries BETWEEN 0 AND 5),
    parent_task_id        TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    tests_status          TEXT    NOT NULL DEFAULT 'not_run'
                                  CHECK (tests_status IN ('not_run','passed','failed','error')),
    tests_output          TEXT,
    tests_output_truncated INTEGER NOT NULL DEFAULT 0 CHECK (tests_output_truncated IN (0, 1)),
    tests_command         TEXT,
    test_exit_code        INTEGER
);

INSERT INTO tasks_new (
    id, repo_id, task_type, state, prompt, model, session_id, branch_name,
    diff_hash, approval_id, error_message, created_at, updated_at, started_at,
    finished_at, output, diff_patch, diff_files, diff_truncated,
    output_truncated, sandboxed, snapshot_head, scheduled_at, retry_count,
    max_retries, parent_task_id, tests_status, tests_output,
    tests_output_truncated, tests_command, test_exit_code
)
SELECT
    id, repo_id, task_type, state, prompt, model, session_id, branch_name,
    diff_hash, approval_id, error_message, created_at, updated_at, started_at,
    finished_at, output, diff_patch, diff_files, diff_truncated,
    output_truncated, sandboxed, snapshot_head, NULL, 0, 0, NULL, 'not_run',
    NULL, 0, NULL, NULL
FROM tasks;

DROP TABLE tasks;

ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX idx_tasks_repo_id ON tasks (repo_id);
CREATE INDEX idx_tasks_state ON tasks (state);
CREATE INDEX idx_tasks_created_at ON tasks (created_at);
CREATE INDEX idx_tasks_scheduled_at ON tasks (scheduled_at);
CREATE INDEX idx_tasks_parent_task_id ON tasks (parent_task_id);

ALTER TABLE repos ADD COLUMN allowed_task_types TEXT NOT NULL
    DEFAULT 'analyze,explain,bugfix,feature,docs,deps,tests';
ALTER TABLE repos ADD COLUMN test_command TEXT;
