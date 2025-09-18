#!/usr/bin/env bash
# slurm_load_generator.sh
# Continuously create a cyclic, sawtooth-shaped Slurm job submission load to
# exercise cluster autoscaling. It submits N exclusive 60s jobs per minute,
# where N ramps from MIN_RATE to MAX_RATE (default 1->5), changes by 1 every
# RATE_ADJUST_INTERVAL_MINUTES (default 2 minutes), then ramps back down to
# MIN_RATE, and repeats forever.
#
# Each job uses the existing batch script sleep-exclusive.slurm (expected to be
# in the current working directory or path you supply via JOB_SCRIPT).
#
# Environment variable overrides:
#   MIN_RATE=1                 # lowest jobs/minute
#   MAX_RATE=5                 # highest jobs/minute
#   RATE_ADJUST_INTERVAL_MINUTES=2  # how many minutes at a given rate before adjusting
#   JOB_SCRIPT="sleep-exclusive.slurm"  # batch script to submit
#   DRY_RUN=0                  # set to 1 to print what would submit without sbatch
#   LOG_FILE="slurm_load_generator.log" # append logs here (empty disables file logging)
#   SPREAD_SUBMISSIONS=0       # set 1 to spread submissions evenly across the minute
#
# Stop with Ctrl-C; trap prints a short summary.
#
# Example:
#   ./slurm_load_generator.sh
#   MIN_RATE=2 MAX_RATE=8 RATE_ADJUST_INTERVAL_MINUTES=1 ./slurm_load_generator.sh
#
set -euo pipefail

MIN_RATE=${MIN_RATE:-1}
MAX_RATE=${MAX_RATE:-5}
RATE_ADJUST_INTERVAL_MINUTES=${RATE_ADJUST_INTERVAL_MINUTES:-2}
JOB_SCRIPT=${JOB_SCRIPT:-sleep-exclusive.slurm}
DRY_RUN=${DRY_RUN:-0}
LOG_FILE=${LOG_FILE:-slurm_load_generator.log}
SPREAD_SUBMISSIONS=${SPREAD_SUBMISSIONS:-0}

if [[ ! -f "$JOB_SCRIPT" ]]; then
  echo "ERROR: Job script '$JOB_SCRIPT' not found (override with JOB_SCRIPT=...)" >&2
  exit 1
fi
if (( MIN_RATE < 1 )); then
  echo "ERROR: MIN_RATE must be >=1" >&2; exit 1; fi
if (( MAX_RATE < MIN_RATE )); then
  echo "ERROR: MAX_RATE must be >= MIN_RATE" >&2; exit 1; fi
if (( RATE_ADJUST_INTERVAL_MINUTES < 1 )); then
  echo "ERROR: RATE_ADJUST_INTERVAL_MINUTES must be >=1" >&2; exit 1; fi

current_rate=$MIN_RATE
direction=up
minutes_at_rate=0
total_jobs=0
cycle_count=0
start_time=$(date +%s)

log() {
  local ts
  ts=$(date +"%Y-%m-%dT%H:%M:%S%z")
  echo "[$ts] $*"
  if [[ -n "$LOG_FILE" ]]; then
    echo "[$ts] $*" >> "$LOG_FILE"
  fi
}

print_summary() {
  local now elapsed
  now=$(date +%s)
  elapsed=$(( now - start_time ))
  log "SUMMARY: runtime=${elapsed}s total_jobs=${total_jobs} current_rate=${current_rate} cycles=${cycle_count} direction=${direction}" || true
}

trap 'log "Caught SIGINT; exiting..."; print_summary' INT
trap 'log "Caught SIGTERM; exiting..."; print_summary' TERM

log "Starting Slurm load generator: MIN_RATE=${MIN_RATE} MAX_RATE=${MAX_RATE} INTERVAL=${RATE_ADJUST_INTERVAL_MINUTES}m SPREAD=${SPREAD_SUBMISSIONS}" \
    "JOB_SCRIPT=${JOB_SCRIPT}"

while true; do
  minute_start=$(date +%s)
  log "SUBMIT minute_start rate=${current_rate} direction=${direction} minutes_at_rate=${minutes_at_rate}"

  if (( SPREAD_SUBMISSIONS == 1 )) && (( current_rate > 0 )); then
    # Spread submissions roughly evenly across the minute
    # Use floating spacing truncated to integer seconds with final sleep compensating.
    interval=$(python3 - <<'PY'
import math
import os
r=int(os.environ['RATE'])
print(max(1, 60//r))
PY
 RATE=$current_rate )
    for ((i=1;i<=current_rate;i++)); do
      if (( DRY_RUN == 1 )); then
        log "DRY-RUN sbatch $JOB_SCRIPT (job $i/$current_rate)"
      else
        jid=$(sbatch "$JOB_SCRIPT" | awk '{print $NF}') || jid=?
        log "Submitted job $jid ($i/$current_rate)"
      fi
      total_jobs=$(( total_jobs + 1 ))
      # Avoid sleeping after last submission to let minute boundary logic handle it
      if (( i < current_rate )); then
        sleep "$interval"
      fi
    done
    # Sleep remaining time in minute
    now=$(date +%s)
    elapsed=$(( now - minute_start ))
    if (( elapsed < 60 )); then
      sleep $(( 60 - elapsed ))
    fi
  else
    # Burst submit then sleep remaining minute
    for ((i=1;i<=current_rate;i++)); do
      if (( DRY_RUN == 1 )); then
        log "DRY-RUN sbatch $JOB_SCRIPT (job $i/$current_rate)"
      else
        jid=$(sbatch "$JOB_SCRIPT" | awk '{print $NF}') || jid=?
        log "Submitted job $jid ($i/$current_rate)"
      fi
      total_jobs=$(( total_jobs + 1 ))
    done
    # Sleep until a full minute has passed
    now=$(date +%s)
    elapsed=$(( now - minute_start ))
    if (( elapsed < 60 )); then
      sleep $(( 60 - elapsed ))
    fi
  fi

  minutes_at_rate=$(( minutes_at_rate + 1 ))
  if (( minutes_at_rate >= RATE_ADJUST_INTERVAL_MINUTES )); then
    minutes_at_rate=0
    if [[ $direction == up ]]; then
      if (( current_rate < MAX_RATE )); then
        current_rate=$(( current_rate + 1 ))
        if (( current_rate == MAX_RATE )); then direction=down; fi
      else
        direction=down
        current_rate=$(( current_rate - 1 ))
      fi
    else
      if (( current_rate > MIN_RATE )); then
        current_rate=$(( current_rate - 1 ))
        if (( current_rate == MIN_RATE )); then direction=up; cycle_count=$(( cycle_count + 1 )); fi
      else
        direction=up
        current_rate=$(( current_rate + 1 ))
      fi
    fi
    log "RATE-ADJUST new_rate=${current_rate} direction=${direction} cycles=${cycle_count}"
  fi

done
