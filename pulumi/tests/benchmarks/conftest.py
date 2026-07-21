"""Benchmark test harness: largest-first ordering + a mock/real runner.

Benchmarks are modelled as ordinary pytest tests, parametrized by node scale and
collective. The largest-first scheduling heuristic (16 -> 8 -> 4 -> 2 nodes, to
avoid Slurm fragmentation) is injected via ``pytest-order``: this hook assigns
each test an ``order`` index derived from its ``nodes`` param, and pytest-order
does the actual reordering.

Runner mode (env-driven so no rootdir ``pytest_addoption`` is needed):
  * default            = MOCK (deterministic, no cluster; the harness stays green).
  * CAPS_BENCH_REAL=1  = wire to scripts/nccl-slurm/submit-nccl-host.sh (TODO).
  * CAPS_BENCH_ITERS   = iterations (debug: 10/100 vs 1000).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest


# --- largest-first ordering heuristic ----------------------------------------
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Order benchmark tests largest-first via pytest-order.

    Each benchmark gets a NEGATIVE ``order`` index equal to ``-nodes``. In
    pytest-order, tests run ``order>=0`` first, then unordered, then the negative
    ones ascending -- so ``-16`` (16 nodes) runs before ``-2`` (2 nodes), and all
    benchmarks run AFTER the (unordered) unit suite, leaving its order untouched.
    pytest-order registers its own hook ``trylast``, so the markers added here
    are seen by it.
    """
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is None:
            continue
        nodes = callspec.params.get("nodes")
        if isinstance(nodes, int):
            item.add_marker(pytest.mark.order(index=-nodes))


# --- runner ------------------------------------------------------------------
@dataclass(frozen=True)
class BenchResult:
    nodes: int
    collective: str
    busbw_gbps: float
    wrong: int


class BenchmarkRunner:
    """Submit a benchmark and return its parsed result.

    Mock mode returns deterministic passing numbers so the harness runs without a
    cluster. Real mode is the integration seam to the proven host-launch path.
    """

    # Mock bus bandwidth (GB/s) per collective. NCCL busbw is roughly constant
    # per bus (not linear in node count), so these are flat, plausible values.
    _MOCK_BUSBW = {"allreduce": 15.5, "allgather": 12.0, "alltoall": 9.0}

    def __init__(self, *, real: bool, iters: int) -> None:
        self.real = real
        self.iters = iters

    def run_nccl(self, *, nodes: int, collective: str) -> BenchResult:
        if self.real:
            return self._run_real(nodes=nodes, collective=collective)
        return self._run_mock(nodes=nodes, collective=collective)

    def _run_mock(self, *, nodes: int, collective: str) -> BenchResult:
        busbw = self._MOCK_BUSBW.get(collective, 10.0)
        return BenchResult(nodes=nodes, collective=collective, busbw_gbps=busbw, wrong=0)

    def _run_real(self, *, nodes: int, collective: str) -> BenchResult:
        # TODO(caps): shell out to scripts/nccl-slurm/submit-nccl-host.sh (Slurm
        #   allocates the nodes; mpirun runs on the worker host over IB) with
        #   NODES=<nodes> / the collective / self.iters, then parse busbw + #wrong
        #   from the raw output (scripts/nccl-slurm/process-nccl-slurm.sh already
        #   does that parsing). Runs on the mgmt VM. pytest-order here only decides
        #   the SUBMISSION ORDER (largest-first); the run itself is host-launched.
        raise NotImplementedError(
            "real benchmark path not wired yet; integrate submit-nccl-host.sh"
        )


@pytest.fixture
def benchmark_runner() -> BenchmarkRunner:
    real = os.environ.get("CAPS_BENCH_REAL", "0").lower() in ("1", "true", "yes")
    iters = int(os.environ.get("CAPS_BENCH_ITERS", "1000"))
    return BenchmarkRunner(real=real, iters=iters)
