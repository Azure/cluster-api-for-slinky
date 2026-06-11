"""Pulumi dynamic resource that syncs a local Git commit into Gitea.

After the Gitea repository resource lands an empty repo inside
Gitea, this resource makes sure the repo's default branch points at the
current ``HEAD`` of a local Git working tree. It always attempts a normal
non-force push during replacement; if the remote already matches, Git exits
successfully with no change, and if the branch diverged, Git rejects the push.

The resource id is ``<owner>/<repo>@<head_sha>``. Pulumi diffs that against
the previous-run id; if ``head_sha`` changed, it schedules a replacement and
the replacement syncs the new commit. The optional ``triggers`` input is an
operator-controlled replacement knob for forcing a reconciliation without
changing the local commit. Remote branch checks are deliberately avoided so
``pulumi preview`` stays local and does not depend on live Gitea availability.
"""

from __future__ import annotations

import os
import re
import select
import shlex
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any, Optional

import pulumi
from cryptography.hazmat.primitives import serialization
from pulumi.dynamic import (
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
)


def _git_head_sha(source_dir: str) -> str:
    """Resolve ``HEAD`` of ``source_dir`` to a commit SHA."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _GiteaSyncProvider(ResourceProvider):
    """Synchronizes a local Git tree to a Gitea repo's default branch."""

    def _redact(self, text: str, props: dict) -> str:
        for secret_key in ("ssh_private_key",):
            secret = props.get(secret_key)
            if not secret:
                continue
            raw = str(secret)
            text = text.replace(raw, "<redacted>")
        return text

    @contextmanager
    def _temp_file(self, content: str, mode: int) -> Iterator[str]:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=True,
        ) as temp_file:
            temp_file.write(str(content))
            temp_file.flush()
            os.chmod(temp_file.name, mode)
            yield temp_file.name

    def _openssh_private_key(self, private_key_pem: str) -> str:
        if private_key_pem.lstrip().startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
            return private_key_pem

        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("ascii"),
            password=None,
        )
        return private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")

    @contextmanager
    def _port_forward(
        self,
        props: dict,
        kubeconfig_path: str,
    ) -> Iterator[int]:
        namespace = str(props["service_namespace"])
        service_name = str(props["service_name"])
        service_port = str(int(props["service_port"]))

        with ExitStack() as stack:
            process = stack.enter_context(
                subprocess.Popen(
                    [
                        "kubectl",
                        "--kubeconfig",
                        kubeconfig_path,
                        "-n",
                        namespace,
                        "port-forward",
                        f"service/{service_name}",
                        f":{service_port}",
                        "--address",
                        "127.0.0.1",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
            stack.callback(process.terminate)
            assert process.stdout is not None
            output: list[str] = []
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    remainder = process.stdout.read() or ""
                    raise RuntimeError(
                        "kubectl port-forward for Gitea SSH exited early: "
                        f"{self._redact((''.join(output) + remainder).strip(), props)[:500]}"
                    )
                ready, _, _ = select.select([process.stdout], [], [], 0.1)
                if not ready:
                    continue
                line = process.stdout.readline()
                if not line:
                    continue
                output.append(line)
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) ->", line)
                if match:
                    yield int(match.group(1))
                    return

            raise RuntimeError(
                "timed out waiting for kubectl port-forward to Gitea SSH: "
                f"{self._redact(''.join(output).strip(), props)[:500]}"
            )

    def _build_git_url(self, props: dict, local_port: int) -> str:
        """Construct the local port-forwarded SSH clone URL."""
        owner = props["owner"]
        repo = props["repo_name"]
        return f"ssh://git@127.0.0.1:{local_port}/{owner}/{repo}.git"

    def _git_env(self) -> dict[str, str]:
        return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def _ssh_host_alias(self, props: dict) -> str:
        return (
            f"{props['service_name']}.{props['service_namespace']}.svc.cluster.local"
        )

    def _push(self, props: dict) -> None:
        kubeconfig = props.get("kubeconfig")
        if not kubeconfig:
            raise RuntimeError("kubeconfig is required for Gitea SSH sync")
        with (
            self._temp_file(str(kubeconfig), 0o600) as kubeconfig_path,
            self._temp_file(
                self._openssh_private_key(str(props["ssh_private_key"])),
                0o600,
            ) as key_path,
            self._temp_file(str(props["ssh_known_hosts"]), 0o644) as known_hosts_path,
        ):
            with self._port_forward(props, kubeconfig_path) as local_port:
                git_url = self._build_git_url(props, local_port)
                ssh_command = " ".join(
                    [
                        "ssh",
                        "-i",
                        shlex.quote(key_path),
                        "-o",
                        "IdentitiesOnly=yes",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "StrictHostKeyChecking=yes",
                        "-o",
                        f"UserKnownHostsFile={shlex.quote(known_hosts_path)}",
                        "-o",
                        f"HostKeyAlias={shlex.quote(self._ssh_host_alias(props))}",
                    ]
                )
                subprocess.run(
                    [
                        "git",
                        "push",
                        git_url,
                        f"HEAD:refs/heads/{props['default_branch']}",
                    ],
                    cwd=props["source_dir"],
                    check=True,
                    env={**self._git_env(), "GIT_SSH_COMMAND": ssh_command},
                    stderr=subprocess.PIPE,
                    text=True,
                )

    def create(self, props: dict) -> CreateResult:
        head = _git_head_sha(props["source_dir"])
        try:
            self._push(props)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "git push to sync Gitea repo "
                f"{props['owner']}/{props['repo_name']} failed without --force. "
                "The remote branch may have diverged: "
                f"{self._redact((exc.stderr or '').strip(), props)[:500]}"
            ) from exc

        return CreateResult(
            id_=f"{props['owner']}/{props['repo_name']}@{head}",
            outs={
                **props,
                "head_sha": head,
                "pushed": True,
            },
        )

    def diff(self, _id: str, old: dict, new: dict) -> DiffResult:
        replaces = []
        for key in (
            "owner",
            "repo_name",
            "default_branch",
            "source_dir",
            "service_namespace",
            "service_name",
            "service_port",
            "head_sha",
        ):
            if old.get(key) != new.get(key):
                replaces.append(key)

        if (old.get("triggers") or {}) != (new.get("triggers") or {}):
            replaces.append("triggers")

        return DiffResult(changes=bool(replaces), replaces=replaces or None)

    def read(self, id_: str, props: dict) -> ReadResult:
        return ReadResult(id_=id_, outs=props)

    def delete(self, _id: str, _props: dict) -> None:
        return


class GiteaSync(Resource):
    """Keeps ``<owner>/<repo>:<branch>`` aligned with local ``HEAD``."""

    head_sha: pulumi.Output[str]
    pushed: pulumi.Output[bool]
    ssh_private_key: pulumi.Output[str]
    ssh_known_hosts: pulumi.Output[str]
    owner: pulumi.Output[str]
    repo_name: pulumi.Output[str]
    default_branch: pulumi.Output[str]
    source_dir: pulumi.Output[str]
    triggers: pulumi.Output[dict[str, Any]]

    def __init__(
        self,
        name: str,
        *,
        ssh_private_key: pulumi.Input[str],
        ssh_known_hosts: pulumi.Input[str],
        owner: pulumi.Input[str],
        repo_name: pulumi.Input[str],
        default_branch: pulumi.Input[str],
        source_dir: str,
        kubeconfig: pulumi.Input[str],
        service_namespace: pulumi.Input[str],
        service_name: pulumi.Input[str],
        service_port: pulumi.Input[int],
        triggers: Optional[pulumi.Input[dict[str, Any]]] = None,
        opts: Optional[pulumi.ResourceOptions] = None,
    ) -> None:
        head = _git_head_sha(source_dir)
        super().__init__(
            _GiteaSyncProvider(),
            name,
            {
                "ssh_private_key": ssh_private_key,
                "ssh_known_hosts": ssh_known_hosts,
                "owner": owner,
                "repo_name": repo_name,
                "default_branch": default_branch,
                "source_dir": source_dir,
                "head_sha": head,
                "pushed": None,
                "kubeconfig": kubeconfig,
                "service_namespace": service_namespace,
                "service_name": service_name,
                "service_port": service_port,
                "triggers": triggers or {},
            },
            opts,
        )
