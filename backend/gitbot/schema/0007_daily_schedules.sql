-- Migration 0007: persistent recurring daily repository schedules.
--
-- A schedule stores a local wall-clock time and the IANA timezone that was
-- local to the machine when it was created. The scheduler resolves the next
-- occurrence in that zone on every tick, so daylight-saving transitions are
-- handled by the timezone database rather than by fixed UTC offsets.
--
-- `last_run_date` is the local calendar date (YYYY-MM-DD) of the most recent
-- claimed execution. The scheduler claims a date atomically before creating a
-- task, which prevents duplicate runs after a restart or a concurrent tick.
-- Multiple schedules per repository are intentionally allowed.

CREATE TABLE repo_schedules (
    id            TEXT PRIMARY KEY,
    repo_id       INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    task_type     TEXT    NOT NULL CHECK (task_type IN ('analyze','explain','bugfix','feature','docs','deps','tests')),
    prompt        TEXT    NOT NULL,
    local_time    TEXT    NOT NULL CHECK (
        length(local_time) = 5
        AND substr(local_time, 3, 1) = ':'
        AND CAST(substr(local_time, 1, 2) AS INTEGER) BETWEEN 0 AND 23
        AND CAST(substr(local_time, 4, 2) AS INTEGER) BETWEEN 0 AND 59
    ),
    timezone      TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_run_at   TEXT,
    last_run_date TEXT CHECK (last_run_date IS NULL OR length(last_run_date) = 10),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX idx_repo_schedules_repo_id ON repo_schedules (repo_id);
CREATE INDEX idx_repo_schedules_enabled ON repo_schedules (enabled, local_time);
