#!/usr/bin/env bash
# common-slurm.sh — Slurm-side helpers for the NCCL/MPI benchmark migration.
#
# Slurm analog of the PBS helpers added in hpc-image-val2 PR 740
# (headnode/common.sh::check_pbs_exit_codes). Where PBS relied on
# `job_history_enable=t` + `qstat -xf` to retain a finished job's Exit_status,
# Slurm uses the accounting DB (slurmdbd) queried with `sacct`. This is both the
# durable-history equivalent AND a strict improvement: Slurm records the job
# STATE (COMPLETED / FAILED / TIMEOUT / NODE_FAIL / OOM / CANCELLED / PREEMPTED),
# so a job that ended abnormally with a 0 exit code (e.g. a node failure) is
# still caught — something an exit-code-only check would miss.
#
# REQUIRES accounting to be enabled on the cluster (slurmdbd + a storage
# backend). On caps-self that means `accounting.enabled: true` + the bundled
# MariaDB in caps-self-slurm.yaml (today it is OFF). Without it, `sacct` returns
# nothing and this function reports failure (mirroring the pre-PR-740 PBS gap of
# "no Exit_status ⇒ treat as failure"). For a no-accounting cluster, capture exit
# codes at submit time instead with `sbatch --wait` (see wait_and_check_jobs).

####
# @Brief : Verify every finished Slurm job COMPLETED with ExitCode 0:0.
# @Param : (1) label for log messages (e.g. NCCL / RCCL)
#          (2) comma-separated Slurm job IDs to check
# @RetVal: 0 if all jobs COMPLETED 0:0; 1 if any failed / missing / abnormal
####
check_slurm_exit_codes() {
    local label="$1" ids="$2"
    echo "##[section]Checking ${label} Slurm job exit codes"

    if [ -z "${ids}" ]; then
        echo "##[warning]No ${label} job IDs supplied; cannot verify exit codes."
        return 1
    fi

    # -X  : one row per job (suppress .batch/.extern steps)
    # -n  : no header
    # -P -d| : parsable, pipe-delimited (robust against spaces in State reasons)
    local records
    records=$(sacct -nXP --delimiter='|' -j "${ids}" \
                 --format=JobID,JobName,State,ExitCode 2>/dev/null) || true

    if [ -z "${records}" ]; then
        echo "##[warning]No accounting records found for ${label} jobs."
        echo "##[warning]Is slurmdbd/accounting enabled? (sacct returned nothing.)"
        return 1
    fi

    local job_failure=0 jid jname state exitcode
    while IFS='|' read -r jid jname state exitcode; do
        [ -z "${jid}" ] && continue
        # State carries a trailing reason for some cases (e.g. "CANCELLED by 0").
        state=${state%% *}
        if [ "${state}" != "COMPLETED" ] || [ "${exitcode}" != "0:0" ]; then
            echo "##[error]${label} job ${jid} (${jname}) failed: State=${state} ExitCode=${exitcode}"
            job_failure=1
        else
            echo "##[debug]${label} job ${jid} (${jname}) COMPLETED 0:0"
        fi
    done <<< "${records}"

    return ${job_failure}
}

####
# @Brief : No-accounting alternative — block on each job with `sbatch --wait`
#          and capture its exit code directly (the sbatch exit status equals the
#          job's derived exit code). Use this when slurmdbd is NOT enabled.
#          NOTE: the caller must have submitted with this same process, OR
#          re-attach; this helper expects an associative description of
#          jobid->name and uses `scontrol`/`wait` semantics is not possible, so
#          it instead waits via a dependency gate and inspects scontrol while the
#          jobs are still within MinJobAge.
# @Param : (1) label   (2) comma-separated job IDs
# @RetVal: 0 all ok / 1 any failed
####
wait_and_check_jobs_no_acct() {
    local label="$1" ids="$2"
    echo "##[section]Waiting for ${label} jobs (no-accounting path)"
    local colon="${ids//,/:}"

    # Gate: block until every benchmark job reaches a terminal state.
    sbatch --wait --dependency="afterany:${colon}" \
           --job-name="${label,,}-gate" --nodes=1 --time=00:05:00 \
           --wrap='true' >/dev/null 2>&1 || true

    # Inspect each job while it is still in scontrol's MinJobAge window.
    local job_failure=0 jid line state ec
    IFS=',' read -ra _ids <<< "${ids}"
    for jid in "${_ids[@]}"; do
        line=$(scontrol show job "${jid}" 2>/dev/null) || {
            echo "##[error]${label} job ${jid} aged out of scontrol; cannot verify (enable accounting)."
            job_failure=1; continue
        }
        state=$(sed -n 's/.*JobState=\([A-Z_]*\).*/\1/p' <<< "${line}")
        ec=$(sed -n 's/.*ExitCode=\([0-9]*:[0-9]*\).*/\1/p' <<< "${line}")
        if [ "${state}" != "COMPLETED" ] || [ "${ec}" != "0:0" ]; then
            echo "##[error]${label} job ${jid} failed: JobState=${state} ExitCode=${ec}"
            job_failure=1
        else
            echo "##[debug]${label} job ${jid} COMPLETED 0:0"
        fi
    done
    return ${job_failure}
}
