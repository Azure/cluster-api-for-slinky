#!/usr/bin/env bash
# Run val2-compatible health checks across host-launched Slurm worker nodes.
set -o pipefail

: "${HOSTS:?HOSTS is required}"
: "${HOST_LAUNCH_JOB_ID:?HOST_LAUNCH_JOB_ID is required}"

WORKER_SSH_USER="${WORKER_SSH_USER:-capi}"
RUN_GPU_BURN="${RUN_GPU_BURN:-0}"
GHR_OBJECT_ID="${GHR_OBJECT_ID:-}"
GHR_FAULT_MAP_B64="${GHR_FAULT_MAP_B64:-}"
PIPELINE_ID="${PIPELINE_ID:-}"
ARTIFACT_DIR="/home/${WORKER_SSH_USER}/caps-health-${HOST_LAUNCH_JOB_ID}"
REMOTE_KEY="/home/${WORKER_SSH_USER}/.ssh/caps-hostmpi-id"
SSH_OPTS=(-i "$REMOTE_KEY" -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

mkdir -p "$ARTIFACT_DIR/nodes" "$ARTIFACT_DIR/ghr"
status_file="$ARTIFACT_DIR/status.tsv"
: > "$status_file"

IFS=',' read -r -a host_entries <<< "$HOSTS"
HEAD_IP="${host_entries[0]%%:*}"

run_remote() {
  local ip=$1 command=$2
  if [[ "$ip" == "$HEAD_IP" ]]; then
    bash -lc "$command"
  else
    ssh "${SSH_OPTS[@]}" "$WORKER_SSH_USER@$ip" "bash -lc $(printf '%q' "$command")"
  fi
}

check_remote() {
  local ip=$1 name=$2 command=$3 optional=$4 rc
  local node_dir="$ARTIFACT_DIR/nodes/${ip//./-}"
  local log="$node_dir/${name}.log"
  mkdir -p "$node_dir"
  run_remote "$ip" "$command" >"$log" 2>&1
  rc=$?
  if [[ "$rc" -eq 0 ]]; then
    printf '%s\t%s\tpassed\t\n' "$ip" "$name" >> "$status_file"
    return 0
  fi
  if [[ "$optional" == 1 && "$rc" -eq 3 ]]; then
    printf '%s\t%s\tskipped\tnot available\n' "$ip" "$name" >> "$status_file"
    return 3
  fi
  printf '%s\t%s\tfailed\tcheck failed (rc=%s)\n' "$ip" "$name" "$rc" >> "$status_file"
  return 1
}

trigger_ghr() {
  local ip=$1
  [[ -n "$GHR_OBJECT_ID" && -n "$GHR_FAULT_MAP_B64" ]] || return 0
  local output="$ARTIFACT_DIR/nodes/${ip//./-}/trigger-ghr.log"
  local command
  command="set -e -o pipefail; printf '%s' '$GHR_FAULT_MAP_B64' | base64 -d > /tmp/caps-nhc-fault-map.json; trap 'rm -f /tmp/caps-nhc-fault-map.json' EXIT; sudo -n /usr/local/sbin/caps-health-root ghr '$GHR_OBJECT_ID'"
  if ! run_remote "$ip" "$command" >"$output" 2>&1; then
    return 0
  fi
  python3 - "$output" "$ARTIFACT_DIR/ghr/ghr-${ip//./-}.json" "$ip" <<'PY'
import json
import pathlib
import os
import sys

source, destination, ip = sys.argv[1:]
text = pathlib.Path(source).read_text()
decoder = json.JSONDecoder()
response = None
for index, character in enumerate(text):
    if character != "{":
        continue
    try:
        candidate, _ = decoder.raw_decode(text[index:])
    except json.JSONDecodeError:
        continue
    if isinstance(candidate, dict) and isinstance(candidate.get("properties"), dict):
        response = candidate
        break
if response is None:
    raise SystemExit(0)
properties = response["properties"]
additional = properties.get("additionalProperties") or {}
record = {
    "pipelineId": os.environ.get("PIPELINE_ID", ""),
    "vmIp": ip,
    "impactCategory": properties.get("impactCategory"),
    "impactDescription": properties.get("impactDescription"),
    "physicalHostName": additional.get("PhysicalHostName"),
    "workloadImpactResponse": response,
}
pathlib.Path(destination).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
PY
}

for entry in "${host_entries[@]}"; do
  ip="${entry%%:*}"
  image_command='test -x /opt/azurehpc/test/run-tests.sh || exit 3; timeout 1200 sudo -n /usr/local/sbin/caps-health-root sanity NVIDIA > /tmp/caps-health-sanity.out 2>&1; command_rc=$?; cat /tmp/caps-health-sanity.out; test "$command_rc" -eq 0 && grep -q "ALL OK!" /tmp/caps-health-sanity.out'
  aznhc_command='test -x /opt/azurehpc/test/azurehpc-health-checks/run-health-checks.sh || exit 3; timeout 1200 sudo -n /usr/local/sbin/caps-health-root aznhc >/dev/null 2>&1; command_rc=$?; cat /tmp/caps-health-aznhc.out; test "$command_rc" -eq 0 && grep -q "Health checks completed with exit code: 0." /tmp/caps-health-aznhc.out'
  dcgmi_command='command -v dcgmi >/dev/null || exit 3; timeout 900 sudo -n /usr/local/sbin/caps-health-root dcgmi'
  ib_command='set -e -o pipefail; command -v ofed_info >/dev/null; ofed_info -s; lspci | grep -E "Infiniband controller|Network controller"; ibstat | grep -q "LinkUp"; ibstat'

  check_remote "$ip" image-sanity "$image_command" 0 || true
  check_remote "$ip" aznhc "$aznhc_command" 1
  aznhc_rc=$?
  if [[ "$aznhc_rc" -eq 1 ]]; then
    trigger_ghr "$ip"
  fi
  check_remote "$ip" dcgmi "$dcgmi_command" 1 || true
  check_remote "$ip" ib "$ib_command" 0 || true

  if [[ "${RUN_GPU_BURN,,}" =~ ^(1|true|yes|on)$ ]]; then
    gpu_burn_command='set -e; work=/tmp/caps-gpu-burn; rm -rf "$work"; git clone --depth 1 https://github.com/wilicc/gpu-burn.git "$work"; cd "$work"; COMPUTE=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d .); make COMPUTE="$COMPUTE"; timeout 300 ./gpu_burn 40 | tee /tmp/caps-health-gpu-burn.out; ! grep -qE "Couldn.t init a GPU test|DEAD!" /tmp/caps-health-gpu-burn.out; tested=$(sed -n "s/.*Tested \([0-9]*\) GPUs.*/\1/p" /tmp/caps-health-gpu-burn.out); ok=$(grep -c "GPU [0-9]*: OK" /tmp/caps-health-gpu-burn.out); test -n "$tested" -a "$tested" -gt 0 -a "$ok" -eq "$tested"'
    check_remote "$ip" gpu-burn "$gpu_burn_command" 0 || true
  fi
done

if ! python3 - "$status_file" "$ARTIFACT_DIR/health-summary.json" <<'PY'
import json
import pathlib
import sys

source, destination = sys.argv[1:]
nodes = {}
for line in pathlib.Path(source).read_text().splitlines():
    ip, name, status, reason = (line.split("\t") + [""])[:4]
    nodes.setdefault(ip, {})[name] = {"status": status, "reason": reason}
summary_nodes = []
for ip, checks in sorted(nodes.items()):
    failed = [name for name, value in checks.items() if value["status"] == "failed"]
    summary_nodes.append({"ip": ip, "healthy": not failed, "failedChecks": failed, "checks": checks})
summary = {
    "nodes": summary_nodes,
    "nodeCount": len(summary_nodes),
    "healthyCount": sum(node["healthy"] for node in summary_nodes),
    "ghrRecords": len(list((pathlib.Path(destination).parent / "ghr").glob("*.json"))),
}
pathlib.Path(destination).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
PY
then
  echo "failed to generate health summary" >&2
  exit 1
fi

[[ -s "$ARTIFACT_DIR/health-summary.json" ]] || exit 1
cat "$ARTIFACT_DIR/health-summary.json"
