-- Migration 0003: persist OpenCode's text response for each task.
ALTER TABLE tasks ADD COLUMN output TEXT;
