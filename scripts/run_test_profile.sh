#!/usr/bin/env bash
# Start GitBot against a disposable test profile.
#
# This script deliberately overrides HOME, XDG_DATA_HOME, and XDG_CONFIG_HOME so
# the real profile (~/.local/share/gitbot and ~/.config/gitbot) can never be
# opened, migrated, or rotated by accident.  It never copies the real API token:
# GitBot creates a new token in the disposable profile on first start.
#
# Usage:
#   scripts/run_test_profile.sh --check   # migrate + validate the throwaway DB
#   scripts/run_test_profile.sh --serve   # start the local dashboard (default)
#
# Optional environment:
#   GITBOT_TEST_PROFILE   profile root (default: /tmp/gitbot-test-profile)
#   GITBOT_TEST_PYTHON    interpreter (default: <repo>/.venv/bin/python)
#   GITBOT_TEST_PORT      loopback port (default: 8766, avoids the real 8765)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_HOME="${HOME:?HOME must be set}"
PROFILE="${GITBOT_TEST_PROFILE:-/tmp/gitbot-test-profile}"
PYTHON="${GITBOT_TEST_PYTHON:-$ROOT/.venv/bin/python}"
PORT="${GITBOT_TEST_PORT:-8766}"

if [ ! -x "$PYTHON" ]; then
    echo "python interpreter not found: $PYTHON" >&2
    exit 2
fi

abs() {
    "$PYTHON" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

PROFILE_ABS="$(abs "$PROFILE")"
REAL_DATA="$(abs "$REAL_HOME/.local/share/gitbot")"
REAL_CONFIG="$(abs "$REAL_HOME/.config/gitbot")"
REAL_HOME_ABS="$(abs "$REAL_HOME")"

case "$PROFILE_ABS" in
    /|"$REAL_HOME_ABS"|"$REAL_DATA"|"$REAL_CONFIG"|"$REAL_DATA"/*|"$REAL_CONFIG"/*)
        echo "refusing to use the real profile as a test profile: $PROFILE_ABS" >&2
        exit 2
        ;;
esac

DATA="$PROFILE_ABS/data"
CONFIG="$PROFILE_ABS/config"
TEST_HOME="$PROFILE_ABS/home"
mkdir -p "$DATA" "$CONFIG" "$TEST_HOME"
chmod 700 "$PROFILE_ABS" "$DATA" "$CONFIG" "$TEST_HOME"

# Drop inherited GITBOT_* values, then apply the testing defaults.  Nothing in
# the caller's environment can point this profile at the real database.
while IFS='=' read -r name _; do
    case "$name" in
        GITBOT_*) unset "$name" || true ;;
    esac
done < <(env)

export HOME="$TEST_HOME"
export XDG_DATA_HOME="$DATA"
export XDG_CONFIG_HOME="$CONFIG"
export GITBOT_HOST="127.0.0.1"
export GITBOT_PORT="$PORT"
export GITBOT_DRY_RUN="true"
export GITBOT_SCHEDULER_ENABLED="false"
export GITBOT_AI_GATEWAY_MODEL="opencode/inkling-2-small-free"
export GITBOT_LOG_LEVEL="INFO"
unset GITBOT_AI_GATEWAY_ENABLED GITBOT_AI_GATEWAY_RELAY_PORT GITBOT_AI_GATEWAY_TIMEOUT_S || true
export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"

DB="$DATA/gitbot/gitbot.db"
TOKEN="$CONFIG/gitbot/token"

MODE="${1:---serve}"
case "$MODE" in
    --check)
        "$PYTHON" -m gitbot.main --check
        echo "disposable profile: $PROFILE_ABS"
        echo "disposable database: $DB"
        echo "token (created on serve): $TOKEN"
        ;;
    --serve|"")
        echo "GitBot test profile: $PROFILE_ABS"
        echo "database:            $DB"
        echo "dashboard:           http://127.0.0.1:$PORT/dashboard"
        echo "token file:          $TOKEN"
        echo "Stop with Ctrl+C.  This profile is disposable; delete $PROFILE_ABS to reset."
        exec "$PYTHON" -m gitbot.main --serve
        ;;
    *)
        echo "unknown mode: $MODE (use --check or --serve)" >&2
        exit 2
        ;;
esac
