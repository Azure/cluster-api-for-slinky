#!/usr/bin/env bash
# Bounded single-node SuperBench validation for A100 host-launch.
# Slurm owns the exclusive allocation; this script runs on the allocated worker.
set -euo pipefail

SKU="${SKU:-Standard_ND96amsr_A100_v4}"
HOST_LAUNCH_JOB_ID="${HOST_LAUNCH_JOB_ID:?HOST_LAUNCH_JOB_ID is required}"
SUPERBENCH_IMAGE="${SUPERBENCH_IMAGE:-ghcr.io/microsoft/superbenchmark/superbench@sha256:dfb17e9b44a62e6e038fa0ab3b7e0a565afc069451a576244aa9110e94548067}"
PULL_TIMEOUT_SECONDS="${PULL_TIMEOUT_SECONDS:-900}"
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-1800}"
ARTIFACT_DIR="${SUPERBENCH_ARTIFACT_DIR:-$HOME/caps-superbench-${HOST_LAUNCH_JOB_ID}}"
WORK_DIR="$(mktemp -d "/tmp/caps-superbench-${HOST_LAUNCH_JOB_ID}.XXXXXX")"
CONFIG="$WORK_DIR/bounded-a100.yaml"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

case "$SKU" in
  Standard_ND96asr_v4|Standard_ND96amsr_A100_v4) ;;
  *) echo "ERROR: bounded SuperBench currently supports A100 NDv4 only, got $SKU" >&2; exit 1 ;;
esac

command -v docker >/dev/null || { echo "ERROR: docker is not installed" >&2; exit 1; }
command -v nvidia-smi >/dev/null || { echo "ERROR: nvidia-smi is not installed" >&2; exit 1; }
[[ "$(nvidia-smi -L | wc -l)" -eq 8 ]] \
  || { echo "ERROR: expected 8 GPUs on $SKU" >&2; exit 1; }

rm -rf "$ARTIFACT_DIR"
mkdir -p "$ARTIFACT_DIR"

cat > "$CONFIG" <<'YAML'
version: v0.8
superbench:
  enable:
  - kernel-launch
  - gemm-flops
  - mem-bw
  - gpu-copy-bw:correctness
  - nccl-bw:nvlink
  monitor:
    enable: true
    sample_duration: 1
    sample_interval: 10
  var:
    default_timeout: 300
    default_local_mode:
      modes:
      - name: local
        proc_num: 8
        prefix: CUDA_VISIBLE_DEVICES={proc_rank}
        parallel: true
    nccl_parameter:
      minbytes: 1K
      maxbytes: 1G
      stepfactor: 2
      check: 1
      warmup_iters: 5
      iters: 20
  benchmarks:
    kernel-launch:
      timeout: 300
      modes:
      - name: local
        proc_num: 8
        prefix: CUDA_VISIBLE_DEVICES={proc_rank}
        parallel: true
        env: {}
    gemm-flops:
      timeout: 600
      modes:
      - name: local
        proc_num: 8
        prefix: CUDA_VISIBLE_DEVICES={proc_rank}
        parallel: true
        env: {}
      parameters:
        num_warmup: 2
        n: 4096
        k: 4096
        m: 4096
        precision: [fp16, fp16_tc]
    mem-bw:
      timeout: 300
      modes:
      - name: local
        proc_num: 8
        prefix: CUDA_VISIBLE_DEVICES={proc_rank} numactl -N $(({proc_rank}/2))
        parallel: false
        env: {}
      parameters:
        sleep: 3
    gpu-copy-bw:correctness:
      timeout: 300
      modes:
      - name: local
        parallel: false
        env: {}
        proc_num: 8
        prefix: ''
      parameters:
        mem_type: [htod, dtoh, dtod]
        copy_type: [sm, dma]
        size: 4096
        num_warm_up: 0
        num_loops: 1
        check_data: true
    nccl-bw:nvlink:
      timeout: 600
      modes:
      - name: mpi
        proc_num: 8
        node_num: 1
        env:
          PATH: null
          LD_LIBRARY_PATH: null
          SB_MICRO_PATH: null
          SB_WORKSPACE: null
        mca:
          pml: ob1
          btl: ^openib
          btl_tcp_if_exclude: lo,docker0
          coll_hcoll_enable: 0
      parameters:
        minbytes: 1K
        maxbytes: 1G
        stepfactor: 2
        check: 1
        warmup_iters: 5
        iters: 20
  env: {}
YAML

echo "# SuperBench host: $(hostname)"
echo "# SKU: $SKU"
echo "# image: $SUPERBENCH_IMAGE"
echo "# artifact: $ARTIFACT_DIR"
echo "# limits: pull=${PULL_TIMEOUT_SECONDS}s run=${RUN_TIMEOUT_SECONDS}s"

timeout "$PULL_TIMEOUT_SECONDS" docker pull "$SUPERBENCH_IMAGE"

# Prove the pinned benchmark image can see every GPU before starting the suite.
timeout 120 docker run --rm --gpus all \
  "$SUPERBENCH_IMAGE" nvidia-smi -L \
  | tee "$ARTIFACT_DIR/docker-gpu-smoke.log"
[[ "$(grep -c '^GPU ' "$ARTIFACT_DIR/docker-gpu-smoke.log")" -eq 8 ]] \
  || { echo "ERROR: Docker did not expose all 8 GPUs" >&2; exit 1; }

timeout "$RUN_TIMEOUT_SECONDS" docker run --rm \
  --gpus all \
  --privileged \
  --net=host \
  --ipc=host \
  --cpus 88 \
  --memory 800g \
  --shm-size=20480m \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  -v "$ARTIFACT_DIR:/opt/superbench/superbench/config/outputs" \
  -v "$CONFIG:/opt/superbench/superbench/config/bounded-a100.yaml:ro" \
  "$SUPERBENCH_IMAGE" \
  bash -lc 'source /opt/hpcx/hpcx-init.sh && hpcx_load && cd /opt/superbench/superbench/config && sb run -l localhost -c bounded-a100.yaml --no-docker' \
  2>&1 | tee "$ARTIFACT_DIR/superbench.log"

# SuperBench runs as root in the privileged container. Return ownership to the
# host-launch user so the management VM can copy and remove the artifact tree.
docker run --rm \
  -v "$ARTIFACT_DIR:/artifacts" \
  "$SUPERBENCH_IMAGE" \
  chown -R "$(id -u):$(id -g)" /artifacts

RESULT_FILE="$(find "$ARTIFACT_DIR" -mindepth 2 -maxdepth 2 -name results-summary.jsonl -type f -print -quit)"
[[ -s "$RESULT_FILE" ]] \
  || { echo "ERROR: SuperBench did not produce results-summary.jsonl" >&2; exit 1; }
echo "# result: $RESULT_FILE"
