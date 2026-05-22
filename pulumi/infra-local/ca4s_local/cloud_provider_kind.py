"""Pulumi dynamic resource: the ``cloud-provider-kind`` host daemon.

Design notes
------------
``cloud-provider-kind`` (sigs.k8s.io/cloud-provider-kind) is the "cloud
controller" missing from a vanilla kind cluster — it watches all kind
clusters via the local Docker daemon and provisions ``Service`` objects
of type ``LoadBalancer`` with real host-reachable IPs (typically
``127.0.0.1:<port>``). Without it, ``type: LoadBalancer`` Services on
kind stay forever in ``<pending>``.

The daemon is **host-singleton, not per-cluster** — one process services
every kind cluster running on the host. So it's modeled as a sibling
resource of ``CtlptlCluster``, not a child concern of it. If you ever
declare multiple ``CtlptlCluster`` resources you still only want one
``CloudProviderKind``.

Lifecycle
---------
* ``create`` spawns the daemon detached via
  ``Popen(..., start_new_session=True, close_fds=True)``. The child
  becomes the session leader of its own group; SIGHUP isn't sent when
  the Pulumi language host exits, so the daemon survives across
  ``pulumi up`` invocations. PID 1 adopts it once Pulumi exits.
* ``read`` probes the stored PID via ``os.kill(pid, 0)`` and (on Linux)
  re-checks ``/proc/<pid>/comm`` to defend against PID reuse after a
  host reboot. If the daemon is gone, ``read`` returns ``id_=None``
  so the next ``pulumi up`` re-spawns.
* ``delete`` SIGTERMs the PID with a short grace window, then SIGKILLs.

Inputs
------
* ``enable_lb_port_mapping`` (bool, default ``True``) — passes
  ``--enable-lb-port-mapping=<bool>`` to the daemon. When ``True``,
  the daemon publishes each ``LoadBalancer`` Service's port via a
  per-Service haproxy container's ``-p 127.0.0.1:<port>:<port>``
  mapping and reports ``EXTERNAL-IP=127.0.0.1``. When ``False``,
  the EXTERNAL-IP is an address on the kind docker bridge, which is
  routable on native Linux but **not** from inside a WSL2 distro
  (Docker Desktop runs the daemon in a separate VM). Default ``True``
  is the right choice for Mac/Windows/WSL2; flip to ``False`` on a
  pure-Linux host if you prefer bridge-IP semantics.

State stored
------------
* ``pid``                    — daemon PID; also the resource ID.
* ``log_path``               — absolute path to the rolling log file
                               (``.state/cloud-provider-kind.log``
                               under the Pulumi project directory).
* ``binary_path``            — absolute path to the binary resolved
                               at create time.
* ``enable_lb_port_mapping`` — echoed back as an output for
                               visibility / debugging.

Caveats
-------
* PIDs are reused across host reboots, hence the ``/proc/<pid>/comm``
  cross-check in ``read``. On non-Linux hosts that check is skipped
  (best-effort) and we rely solely on ``os.kill(pid, 0)``.
* This provider does **not** detect a ``cloud-provider-kind`` daemon
  that was started outside Pulumi (e.g., a leftover from manual
  experimentation). If one is already running, ``pulumi up`` will
  spawn a second copy and the two will fight over Service
  reconciliation. Kill any existing daemon before the first ``up``.
* On Linux, ``CAP_NET_ADMIN`` + ``CAP_NET_BIND_SERVICE`` on the binary
  are only useful in the **niche** case of bridge-IP mode
  (``enable_lb_port_mapping=False``) on a host where the kind docker
  bridge isn't already in your shell's network namespace — e.g.,
  Docker Desktop's split-namespace WSL2 integration, which puts
  dockerd in a separate distro. On native Docker (recommended:
  Linux desktop or WSL2 with native ``dockerd``) the kind bridge
  *is* in your shell's netns, the kernel routes to it natively, and
  the LB ``EXTERNAL-IP`` is curlable with no caps required. The
  default ``enable_lb_port_mapping=True`` mode publishes ports via
  per-Service envoy containers and likewise needs no caps. ``create``
  emits a non-fatal diagnostic via ``pulumi.log.warn`` when the caps
  are missing, naming the exact ``setcap`` command and the specific
  scenario where it helps; no resource failure occurs.

Pickling note
-------------
Same constraint as the other dynamic resources in this package: keep
this module stdlib + ``pulumi``-only and stable in module path so the
cloudpickled provider in stack state round-trips cleanly.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from pulumi import Output, ResourceOptions, log
from pulumi.dynamic import (
    CheckResult,
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
)


# ---------------------------------------------------------------------------
# Helpers (module-level so the unpickled provider can resolve them).
# ---------------------------------------------------------------------------

# Linux truncates /proc/<pid>/comm to TASK_COMM_LEN-1 = 15 chars.
_COMM_MAX = 15
_EXPECTED_COMM = "cloud-provider-kind"[:_COMM_MAX]


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"required binary '{name}' not found in PATH; "
            "install it via `go install sigs.k8s.io/cloud-provider-kind@latest` "
            "(needs Go) and ensure $GOPATH/bin is on PATH."
        )
    return path


def _is_pid_alive(pid: int, check_comm: bool = True) -> bool:
    """Return True iff *pid* refers to a running ``cloud-provider-kind``.

    On Linux, additionally cross-check ``/proc/<pid>/comm`` to defend
    against PID reuse across host reboots. On non-Linux hosts ``/proc``
    isn't available and we accept the bare ``kill(pid, 0)`` answer.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        # ProcessLookupError == no such pid.
        # PermissionError typically means "exists but you can't signal it" —
        # for our purposes that's "alive but not ours"; treat as not-alive
        # so we don't pretend to manage a process we can't kill.
        return False
    if check_comm:
        try:
            with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as fh:
                comm = fh.read().strip()
            if comm != _EXPECTED_COMM:
                return False
        except (FileNotFoundError, PermissionError):
            # /proc not available (non-Linux) or unreadable; skip.
            pass
    return True


def _wait_pid_exit(pid: int, timeout: float) -> bool:
    """Poll until *pid* no longer exists, up to *timeout* seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid, check_comm=False):
            return True
        time.sleep(0.1)
    return False


# Linux file capabilities the daemon *can* use. They matter only in a
# narrow scenario (see ``create`` for the full description): bridge-IP
# mode on a host where the kind docker bridge lives in a different
# network namespace than your shell, so the daemon has to actively add
# the LB IP to a host interface for the kernel to route to it.
# ``cap_net_admin`` enables that interface manipulation;
# ``cap_net_bind_service`` lets it bind privileged ports if it ever
# needs to. On native Docker (the recommended setup) neither is
# required — the kernel routes via the bridge interface natively.
_OPTIONAL_CAPS = ("cap_net_admin", "cap_net_bind_service")


def _check_binary_caps(binary: str) -> Optional[set]:
    """Return the set of capabilities granted to *binary*.

    Returns ``None`` when the check could not be performed (non-Linux
    host, or ``getcap`` not on PATH) — caller should treat that as
    "unknown" and skip enforcement. Returns ``set()`` when ``getcap``
    ran and the binary has no caps set.

    ``getcap`` prints either nothing (no caps) or a single line like::

        /home/u/go/bin/cloud-provider-kind cap_net_admin,cap_net_bind_service=eip
    """
    if platform.system() != "Linux":
        return None
    getcap = shutil.which("getcap")
    if getcap is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 — getcap path resolved above
            [getcap, binary],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return set()
    line = out.stdout.strip()
    if not line:
        return set()
    parts = line.split(None, 1)
    if len(parts) < 2:
        return set()
    # "cap_a,cap_b=eip" → drop the "=mode" suffix, split on commas.
    rhs = parts[1].split("=", 1)[0]
    return {c.strip().lower() for c in rhs.split(",") if c.strip()}


def _setcap_command(binary: str) -> str:
    """Return the exact one-liner the user should paste."""
    caps = ",".join(_OPTIONAL_CAPS)
    return f"sudo setcap '{caps}=+eip' {binary}"


def _missing_caps(binary: str) -> Optional[set]:
    """Return the subset of ``_OPTIONAL_CAPS`` not granted to *binary*.

    Returns ``None`` when the check is inconclusive (non-Linux, no
    ``getcap``, or process is running as root). Callers should treat a
    non-empty return value as *diagnostic*, not as an error — these
    caps are only useful in the bridge-IP-without-shared-netns case;
    see ``create`` for the full picture.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return None  # root already has all caps; no setcap needed.
    caps = _check_binary_caps(binary)
    if caps is None:
        return None
    return {c for c in _OPTIONAL_CAPS if c not in caps}


# ---------------------------------------------------------------------------
# Provider.
# ---------------------------------------------------------------------------


class _CloudProviderKindProvider(ResourceProvider):
    """Lifecycle hooks for the CloudProviderKind dynamic resource."""

    def check(self, olds: dict, news: dict) -> CheckResult:
        # No tunable inputs — pure pass-through.
        return CheckResult(inputs=dict(news), failures=[])

    def create(self, props: dict) -> CreateResult:
        binary = _require_binary("cloud-provider-kind")

        # Bool input → CLI flag. Defaults match the constructor default.
        enable_lb_port_mapping = bool(props.get("enable_lb_port_mapping", True))
        argv = [
            binary,
            f"--enable-lb-port-mapping={'true' if enable_lb_port_mapping else 'false'}",
        ]

        # Capability diagnostic (non-fatal). ``cap_net_admin`` +
        # ``cap_net_bind_service`` on the binary only matter in a
        # narrow case: bridge-IP mode (``enable_lb_port_mapping=False``)
        # on a host where the kind docker bridge lives in a network
        # namespace different from your shell — e.g., Docker Desktop's
        # split-namespace WSL2 integration, where dockerd runs in a
        # separate ``docker-desktop`` distro. In that case the daemon
        # needs to add the LB IP to a host interface for the kernel to
        # route to it. On native Docker (the recommended setup on
        # Linux/WSL2) the bridge already is in your shell's netns, the
        # kernel routes to it natively, and these caps are not needed.
        # The default ``enable_lb_port_mapping=True`` publishes per-
        # Service host ports via Docker and likewise needs no caps.
        # We emit a warning if caps are missing, so users who later flip
        # ``enable_lb_port_mapping=False`` on a split-namespace host have
        # a paper trail of what to do — but we never fail the resource.
        missing = _missing_caps(binary)
        if missing:
            log.warn(
                f"cloud-provider-kind at {binary} has no Linux file "
                f"capabilities ({', '.join(sorted(missing))} missing). "
                "This is only relevant if you flip "
                "`enable_lb_port_mapping=False` AND your host has the "
                "kind docker bridge in a different network namespace "
                "than your shell (the classic example is Docker Desktop's "
                "WSL2 integration). On native Docker — the recommended "
                "setup on Linux and WSL2 — the bridge is in your netns "
                "and the LB EXTERNAL-IP is reachable without any caps. "
                "If you ever need to enable bridge-IP mode on a split-"
                f"namespace setup, grant them with: {_setcap_command(binary)}"
            )

        # Log file lives next to Pulumi state. The cwd at create-time is the
        # Pulumi project directory; ``.state/`` is the same directory the
        # local-filesystem backend already uses.
        log_dir = Path(".state").resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "cloud-provider-kind.log"

        # Open in append mode so successive ``create()`` calls (after a host
        # reboot wiped the previous process) keep history. Hand the fd to
        # the child; close ours immediately after Popen so we don't keep a
        # lingering reference.
        log_fh = open(log_path, "ab")  # noqa: SIM115 — handed to child
        try:
            proc = subprocess.Popen(  # noqa: S603 — binary path is resolved above
                argv,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            log_fh.close()

        # Briefly verify the daemon is still alive. It can fail-fast if the
        # Docker socket isn't accessible, or if it lacks privileges.
        time.sleep(0.5)
        if not _is_pid_alive(proc.pid, check_comm=False):
            try:
                tail = log_path.read_text(encoding="utf-8").splitlines()[-20:]
            except OSError:
                tail = []
            msg = (
                "cloud-provider-kind died immediately after spawn. "
                f"Last log lines from {log_path}:\n  "
                + ("\n  ".join(tail) if tail else "(log file empty)")
            )
            # Most common death cause is now docker socket access (user
            # not in the docker group) or a stale older daemon. Capability
            # issues are *not* a common death cause on native Docker
            # — see the diagnostic emitted above for the niche where
            # they'd matter.
            raise RuntimeError(msg)

        return CreateResult(
            id_=str(proc.pid),
            outs={
                "pid": proc.pid,
                "log_path": str(log_path),
                "binary_path": binary,
                "enable_lb_port_mapping": enable_lb_port_mapping,
            },
        )

    def delete(self, id_: str, props: dict) -> None:
        try:
            pid = int(id_)
        except (TypeError, ValueError):
            return
        if not _is_pid_alive(pid):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        if not _wait_pid_exit(pid, timeout=5.0):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        # The daemon's CLI flags are immutable for a running process, so
        # any change to a tracked input forces a replace (kill + respawn).
        # Treat "olds is missing the key" (state predates this input) as
        # a change so the next ``up`` after upgrading the resource code
        # respawns the daemon with the new argv.
        replaces: list[str] = []
        old_flag = olds.get("enable_lb_port_mapping")
        new_flag = news.get("enable_lb_port_mapping", True)
        if old_flag is None or bool(old_flag) != bool(new_flag):
            replaces.append("enable_lb_port_mapping")
        return DiffResult(changes=bool(replaces), replaces=replaces)

    def read(self, id_: str, props: dict) -> ReadResult:
        try:
            pid = int(id_)
        except (TypeError, ValueError):
            return ReadResult(id_=None, outs={})
        if not _is_pid_alive(pid):
            # Daemon is gone (manual kill, OOM, host reboot with PID
            # reuse). Signal "deleted" so the next ``pulumi up`` re-spawns.
            return ReadResult(id_=None, outs={})
        return ReadResult(id_=id_, outs=dict(props))


# ---------------------------------------------------------------------------
# Resource class consumed by user code.
# ---------------------------------------------------------------------------


class CloudProviderKind(Resource):
    """The ``cloud-provider-kind`` daemon, running detached on the host.

    Spawns a host-singleton process that watches all kind clusters via
    the Docker daemon and gives ``type: LoadBalancer`` Services real
    host-reachable IPs.

    Parameters
    ----------
    enable_lb_port_mapping
        When ``True`` (default), pass ``--enable-lb-port-mapping=true``
        so the daemon publishes each ``LoadBalancer`` Service's port via
        ``docker run -p 127.0.0.1:<port>:<port>`` on the host. This is
        the right setting for Mac/Windows/WSL2 where the kind docker
        bridge isn't routable from the host. On a pure-Linux host with
        native Docker you can set this to ``False`` to get bridge-IP
        semantics instead.
    """

    pid: Output[int]
    log_path: Output[str]
    binary_path: Output[str]
    enable_lb_port_mapping: Output[bool]

    def __init__(
        self,
        name: str,
        enable_lb_port_mapping: bool = True,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            _CloudProviderKindProvider(),
            name,
            {
                # Tracked input — toggling this triggers a replace
                # (daemon restart with new argv).
                "enable_lb_port_mapping": enable_lb_port_mapping,
                # Outputs only — declared as ``None`` inputs so Pulumi
                # treats them as expected output names during preview.
                "pid": None,
                "log_path": None,
                "binary_path": None,
            },
            opts,
        )
