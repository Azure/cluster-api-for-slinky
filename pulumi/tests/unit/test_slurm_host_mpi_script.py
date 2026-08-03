from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "scripts" / "slurm-host-mpi.sh"


def test_host_launcher_is_staged_instead_of_streamed() -> None:
    source = SCRIPT.read_text()

    assert 'scp "${SSH_OPTS[@]}" "$LAUNCHER"' in source
    assert 'bash /home/$WORKER_SSH_USER/$REMOTE_LAUNCHER' in source
    assert '"$LAUNCH_ENV bash -s" < "$LAUNCHER"' not in source
    assert '"rm -f ~/$REMOTE_KEY ~/$REMOTE_LAUNCHER"' in source