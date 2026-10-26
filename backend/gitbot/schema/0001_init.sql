-- Migration 0001: repository allowlist, settings, append-only audit log.

CREATE TABLE repos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    path           TEXT    NOT NULL UNIQUE,
    slug           TEXT    NOT NULL,
    default_branch TEXT    NOT NULL DEFAULT 'main',
    allow_commit   INTEGER NOT NULL DEFAULT 0 CHECK (allow_commit IN (0, 1)),
    allow_push     INTEGER NOT NULL DEFAULT 0 CHECK (allow_push IN (0, 1)),
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE INDEX idx_repos_slug ON repos (slug);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT,
    outcome    TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'denied', 'info')),
    detail     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_audit_log_created_at ON audit_log (created_at DESC, id DESC);
CREATE INDEX idx_audit_log_event_type ON audit_log (event_type);

-- The audit log is append-only. Corrections are new rows, never edits.
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
