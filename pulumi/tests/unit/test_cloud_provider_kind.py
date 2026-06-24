"""Unit tests for :mod:`ctlptl.cloud_provider_kind`.

The provider spawns a detached host process. Tests here need to mock
``subprocess.Popen`` (not ``run``) and ``os.kill`` + ``os.waitpid``.
"""

from __future__ import annotations

import pytest

from ctlptl.cloud_provider_kind import CloudProviderKind  # noqa: F401


@pytest.mark.skip(reason="TODO: assert .check() warns (not errors) when ``cloud-provider-kind`` binary lacks ``cap_net_admin`` AND enable_lb_port_mapping=False; should ERROR only if the user explicitly opted into bridge-IP mode on a split-netns host")
def test_check_capabilities_warning_vs_error() -> None:
    pass


@pytest.mark.skip(reason="TODO: stub subprocess.Popen and assert .create() returns a result dict containing the spawned PID, the chosen log path (absolute, under .state/), and ``enable_lb_port_mapping`` echoed from input")
def test_create_records_pid_and_logpath() -> None:
    pass


@pytest.mark.skip(reason="TODO: stub os.kill(pid, 0) to raise ProcessLookupError; assert .read() detects the daemon is dead and surfaces that as drift Pulumi can heal (re-create) on the next up")
def test_read_detects_dead_daemon() -> None:
    pass


@pytest.mark.skip(reason="TODO: assert .diff() marks ``enable_lb_port_mapping`` as replace=True -- the daemon must restart with new argv to honor the flag flip")
def test_diff_flag_change_triggers_replace() -> None:
    pass


@pytest.mark.skip(reason="TODO: stub os.kill(pid, SIGTERM); assert .delete() sends SIGTERM, waits with a deadline, then escalates to SIGKILL on timeout. Idempotent on already-dead PID.")
def test_delete_terminates_then_kills() -> None:
    pass
