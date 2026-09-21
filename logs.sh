#!/usr/bin/env bash
# Readable, timestamped view of the OpenClaw log (which is raw JSON-lines).
#
#   ./logs.sh                    last 60 interesting lines
#   ./logs.sh -f                 follow live
#   ./logs.sh -g "DFS|cron"      filter (regex, case-insensitive)
#   ./logs.sh -n 200             more lines
#   ./logs.sh -a                 no filtering, show everything
#   ./logs.sh --sent             only messages actually delivered to a channel
#   ./logs.sh --cron             only scheduled-job activity
set -uo pipefail
FOLLOW=0; GREP=""; N=60; ALL=0; MODE=""
while [ $# -gt 0 ]; do
  case "$1" in
    -f|--follow) FOLLOW=1 ;;
    -g|--grep) GREP="${2:-}"; shift ;;
    -n) N="${2:-60}"; shift ;;
    -a|--all) ALL=1 ;;
    --sent) MODE=sent ;;
    --cron) MODE=cron ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  esac; shift
done
LOG=$(ls -t /tmp/openclaw/openclaw-*.log 2>/dev/null | head -1)
[ -z "$LOG" ] && { echo "no log found in /tmp/openclaw/"; exit 1; }
echo "── $LOG ──" >&2

fmt() { python3 -u "$(dirname "$0")/ingest/_fmtlog.py"; }

export G="$GREP" M="$MODE" A="$ALL"
if [ "$FOLLOW" = "1" ]; then tail -n 40 -f "$LOG" | fmt
else tail -n 4000 "$LOG" | fmt | tail -n "$N"; fi
