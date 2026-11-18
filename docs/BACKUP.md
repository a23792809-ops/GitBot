# Backup, Migration, and Restore

GitBot uses SQLite and forward-only migrations. This procedure is for the default
profile and for temporary test profiles. **Back up before applying any migration
to a database you care about.**

## Locate the database

Default: `$XDG_DATA_HOME/gitbot/gitbot.db`, usually
`~/.local/share/gitbot/gitbot.db`. Override with `GITBOT_DB_PATH`. The config
file may be `./.env` or `~/.config/gitbot/.env`.

Print the resolved path without changing anything:

```bash
uv run gitbot --check
```

`--check` applies pending migrations to the configured database, so do not run it
against the real profile unless you intend to migrate. To inspect the path from
configuration only, read the effective `GITBOT_DB_PATH` and `XDG_DATA_HOME`.

## Back up

The `sqlite3` CLI is not required. Use Python's standard library in a read-only
connection so a live server is not modified:

```bash
DB="${GITBOT_DB_PATH:-$HOME/.local/share/gitbot/gitbot.db}"
BACKUP="$HOME/gitbot-backups/gitbot-$(date -u +%Y%m%dT%H%M%SZ).db"
mkdir -p "$(dirname "$BACKUP")"
python3 - "$DB" "$BACKUP" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
dst = sqlite3.connect(target)
with dst:
    src.backup(dst)
dst.close()
src.close()
print(target)
PY
```

Verify the backup:

```bash
python3 - "$BACKUP" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
print("migrations:", [row[0] for row in conn.execute(
    "SELECT version FROM schema_migrations ORDER BY version"
)])
conn.close()
PY
```

Keep the backup outside the repository and outside the data directory.

## Apply migrations

Migrations are applied in filename order by the app. Review the file first:

```bash
ls backend/gitbot/schema/
sed -n '1,200p' backend/gitbot/schema/0006_explain_scheduler_tests.sql
```

Then start the app or run the check against the intended profile:

```bash
uv run gitbot --check
```

The migration runner disables foreign-key enforcement around table rebuilds and
verifies `PRAGMA foreign_key_check` before committing. If it fails, stop and
restore the backup rather than editing the migration.

### Important current state

Migrations `0005_analysis_state` and `0006_explain_scheduler_tests` may not be
applied to an existing profile. Both rebuild `tasks`; review them, back up, and
apply deliberately. Do not apply them as part of an unrelated change, and do not
start the real profile (`--check` or `--serve`) until that decision is approved.

For all safe local testing before then, use
[`scripts/run_test_profile.sh`](../scripts/run_test_profile.sh) as described in
[`TESTING.md`](TESTING.md).

## Restore

Stop the server first. Replace the database with the backup, preserving
permissions:

```bash
DB="${GITBOT_DB_PATH:-$HOME/.local/share/gitbot/gitbot.db}"
cp "$BACKUP" "$DB"
chmod 600 "$DB"
```

Then run `uv run gitbot --check` against the restored file and confirm the
migration list. If the backup predates a migration, the app will try to apply
pending migrations again; restore a backup consistent with the code you are
running.

## Test-profile practice

For destructive migration or smoke tests, use a throwaway `HOME`, data directory,
config directory, and database. The supported way is:

```bash
scripts/run_test_profile.sh --check    # migrations only
scripts/run_test_profile.sh --serve    # dashboard on 127.0.0.1:8766
```

It refuses the real data/config paths and never copies the real token. See
[`TESTING.md`](TESTING.md). The equivalent manual command is:

```bash
TMP=/tmp/opencode/gitbot-smoke
mkdir -p "$TMP/home" "$TMP/data" "$TMP/config"
HOME="$TMP/home" \
XDG_DATA_HOME="$TMP/data" \
XDG_CONFIG_HOME="$TMP/config" \
GITBOT_DRY_RUN=true \
uv run gitbot --check
```

Never point a destructive test at the real profile, and never delete or recreate
the real database to "fix" a migration.
