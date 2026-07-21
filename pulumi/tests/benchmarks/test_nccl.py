"""NCCL benchmark suite (prototype on top of pytest + pytest-order).

Each (nodes, collective) pair is one pytest test, so a logical benchmark fans out
to many physical jobs through parametrization. pytest-order schedules them
LARGEST-FIRST (16 -> 8 -> 4 -> 2) via the heuristic hook in conftest.py, so big
jobs are submitted before small ones (avoids Slurm fragmentation).

Run:
  pytest -m benchmark                     # mock (no cluster) -- harness is green
  pytest -m benchmark --collect-only -q   # see the largest-first order
  CAPS_BENCH_REAL=1 pytest -m benchmark   # real cluster (host-launch; TODO)
  CAPS_BENCH_ITERS=10 pytest -m benchmark # debug: fewer iterations
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.benchmark

_SCALES = [16, 8, 4, 2]
_COLLECTIVES = ["allreduce", "allgather", "alltoall"]

# Minimum acceptable bus bandwidth (GB/s) per collective. Placeholder thresholds;
# tune against real V100/IB baselines. Mock numbers are set to clear these.
_MIN_BUSBW = {"allreduce": 10.0, "allgather": 8.0, "alltoall": 6.0}


@pytest.mark.parametrize("collective", _COLLECTIVES)
@pytest.mark.parametrize("nodes", _SCALES)
def test_nccl_busbw(nodes: int, collective: str, benchmark_runner) -> None:
    result = benchmark_runner.run_nccl(nodes=nodes, collective=collective)
    assert result.wrong == 0, (
        f"{collective} @ {nodes} nodes: {result.wrong} incorrect results"
    )
    assert result.busbw_gbps >= _MIN_BUSBW[collective], (
        f"{collective} @ {nodes} nodes: busbw {result.busbw_gbps} GB/s "
        f"< threshold {_MIN_BUSBW[collective]} GB/s"
    )
