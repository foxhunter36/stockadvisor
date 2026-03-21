#!/bin/bash
# run_cron.sh — Wrapper für Stock Advisor Cron Jobs
# Loggt Output und touched .last_run bei Erfolg
# Usage: bash run_cron.sh <script.py> [args]

SA_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$SA_DIR/venv/bin/python3"
LOG="$SA_DIR/cron.log"
SCRIPT="$1"
shift

echo "" >> "$LOG"
echo "── $(date '+%Y-%m-%d %H:%M:%S') — $SCRIPT $* ──" >> "$LOG"

cd "$SA_DIR"
$VENV_PY "$SCRIPT" "$@" >> "$LOG" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    date -Iseconds > "$SA_DIR/.last_run"
    echo "── $(date '+%H:%M:%S') — $SCRIPT OK ──" >> "$LOG"
else
    echo "── $(date '+%H:%M:%S') — $SCRIPT FAILED (exit $EXIT_CODE) ──" >> "$LOG"
fi

exit $EXIT_CODE
