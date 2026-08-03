from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "scripts" / "slurm-host-mpi.sh"
NCCL_LAUNCHER = (
    Path(__file__).parents[3] / "scripts" / "nccl-slurm" / "run-nccl-host.sh"
)


def test_host_launcher_is_staged_instead_of_streamed() -> None:
    source = SCRIPT.read_text()

    assert 'scp "${SSH_OPTS[@]}" "$LAUNCHER"' in source
    assert 'bash /home/$WORKER_SSH_USER/$REMOTE_LAUNCHER' in source
    assert '"$LAUNCH_ENV bash -s" < "$LAUNCHER"' not in source
    assert '"rm -f ~/$REMOTE_KEY ~/$REMOTE_LAUNCHER"' in source
    assert "| head -1" not in source
    assert "-o jsonpath='{.items[0].metadata.name}'" in source


def test_nccl_launcher_supports_full_collective_matrix() -> None:
    source = NCCL_LAUNCHER.read_text()

    for collective, binary in {
        "allreduce": "all_reduce_perf",
        "allgather": "all_gather_perf",
        "alltoall": "alltoall_perf",
        "reducescatter": "reduce_scatter_perf",
        "broadcast": "broadcast_perf",
    }.items():
        assert f'"{collective}")' in source
        assert f'TEST="{binary}"' in source
