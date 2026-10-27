-- Migration 0002: tasks, approvals, and task events.

CREATE TABLE tasks (
    id                TEXT PRIMARY KEY,
    repo_id           INTEGER NOT NULL REFERENCES repos(id) ON DELETE RESTRICT,
    task_type         TEXT    NOT NULL CHECK (task_type IN ('analyze','bugfix','feature','docs','deps','tests')),
    state             TEXT    NOT NULL CHECK (state IN ('queued','running','awaiting_review','approved','committed','failed','cancelled','expired')),
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
    finished_at       TEXT
);

CREATE INDEX idx_tasks_repo_id ON tasks (repo_id);
CREATE INDEX idx_tasks_state ON tasks (state);
CREATE INDEX idx_tasks_created_at ON tasks (created_at);

CREATE TABLE approvals (
    id          TEXT PRIMARY KEY,
    task_id     TEXT    NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE RESTRICT,
    action      TEXT    NOT NULL CHECK (action IN ('commit','push','create_pr','merge','delete_repo')),
    diff_hash   TEXT    NOT NULL,
    argv_json   TEXT    NOT NULL DEFAULT '[]',
    token_hash  TEXT,
    expires_at  TEXT    NOT NULL,
    used_at     TEXT,
    created_at  TEXT    NOT NULL,
    actor       TEXT    NOT NULL
);

CREATE INDEX idx_approvals_task_id ON approvals (task_id);
CREATE INDEX idx_approvals_expires_at ON approvals (expires_at);

CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT    NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    created_at TEXT    NOT NULL,
    event_type TEXT    NOT NULL,
    actor      TEXT    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_task_events_task_id ON task_events (task_id, id);

-- approvals are single-use and time-limited; no global update/delete triggers like audit_log
-- but we enforce semantics in code
CREATE TRIGGER task_events_no_update
BEFORE UPDATE ON task_events
BEGIN
    SELECT RAISE(ABORT, 'task_events is append-only');
END;

CREATE TRIGGER task_events_no_delete
BEFORE DELETE ON task_events
BEGIN
    SELECT RAISE(ABORT, 'task_events is append-only');
END;
