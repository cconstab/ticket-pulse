#!/usr/bin/env bash
# Monthly ticket-pulse run, cron-safe. Writes a snapshot row, the dashboard,
# the digest, and (if an LLM command is available) the narrative.
#
# Usage:
#   ./monthly-report.sh OWNER [LLM_CMD]
#
#   OWNER    GitHub org or user to report on
#   LLM_CMD  command for the narrative (default: "claude -p";
#            e.g. "ollama run llama3.1"). Skipped if not installed.
#
# crontab example — 07:00 UTC on the 1st of each month:
#   0 7 1 * * /path/to/ticket-pulse/monthly-report.sh your-org >> $HOME/ticket-pulse.log 2>&1
#
# Outputs land next to this script, named for the month they report on
# (running on 1 Aug writes dashboard-2026-07.html, etc). snapshots.csv
# accumulates the backlog history — don't delete it.

set -euo pipefail
cd "$(dirname "$0")"

OWNER="${1:?usage: monthly-report.sh OWNER [LLM_CMD]}"
LLM_CMD="${2:-claude -p}"

# cron's PATH is minimal; cover the usual install locations.
export PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

command -v python3 >/dev/null || { echo "FATAL: python3 not on PATH"; exit 1; }
command -v gh >/dev/null      || { echo "FATAL: gh not on PATH"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "FATAL: gh not authenticated (run: gh auth login)"; exit 1; }

# The month being reported on = last complete month (GNU date, then BSD fallback).
MONTH=$(date -d "$(date +%Y-%m-15) -1 month" +%Y-%m 2>/dev/null || date -v-1m +%Y-%m)

NARRATIVE_ARGS=()
if command -v "$(echo "$LLM_CMD" | awk '{print $1}')" >/dev/null; then
  NARRATIVE_ARGS=(--narrative "narrative-$MONTH.md" --llm-cmd "$LLM_CMD")
else
  echo "WARN: '$LLM_CMD' not available — skipping the narrative for $MONTH"
fi

BUCKETS_ARGS=()
[ -f buckets.json ] && BUCKETS_ARGS=(--buckets buckets.json)

echo "=== $(date -u '+%Y-%m-%d %H:%M UTC') — ticket-pulse for $OWNER, $MONTH ==="
python3 ticket_pulse.py "$OWNER" \
  --snapshot snapshots.csv \
  --json stats.json \
  --html "dashboard-$MONTH.html" \
  --digest "digest-$MONTH.md" \
  "${BUCKETS_ARGS[@]}" \
  "${NARRATIVE_ARGS[@]}"

# Optional: keep the history in git. Uncomment if this checkout may push.
# git add snapshots.csv && git commit -m "chore: ticket-pulse snapshot for $MONTH" && git push

echo "=== done ==="
