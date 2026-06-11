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
import shlex
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Optional

import pulumi
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

    def _build_git_url(self, props: dict) -> str:
        """Construct the host-reachable SSH clone URL."""
        owner = props["owner"]
        repo = props["repo_name"]
        return f"ssh://git@{props['ssh_host']}:{int(props['ssh_port'])}/{owner}/{repo}.git"

    def _git_env(self) -> dict[str, str]:
        return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def _ssh_material(self, props: dict) -> tuple[str, str]:
        private_key = str(props["ssh_private_key"])
        host_public_key = str(props["ssh_host_public_key"]).strip()
        known_hosts = f"{props['ssh_host_alias']} {host_public_key}\n"
        return private_key, known_hosts

    def _push(self, props: dict) -> None:
        private_key, known_hosts = self._ssh_material(props)
        with (
            self._temp_file(private_key, 0o600) as key_path,
            self._temp_file(known_hosts, 0o644) as known_hosts_path,
        ):
            git_url = self._build_git_url(props)
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
                    f"HostKeyAlias={shlex.quote(str(props['ssh_host_alias']))}",
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
            "ssh_host",
            "ssh_host_alias",
            "ssh_port",
            "ssh_private_key",
            "ssh_host_public_key",
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
        ssh_host_public_key: pulumi.Input[str],
        ssh_host: pulumi.Input[str],
        ssh_host_alias: pulumi.Input[str],
        ssh_port: pulumi.Input[int],
        owner: pulumi.Input[str],
        repo_name: pulumi.Input[str],
        default_branch: pulumi.Input[str],
        source_dir: str,
        triggers: Optional[pulumi.Input[dict[str, Any]]] = None,
        opts: Optional[pulumi.ResourceOptions] = None,
    ) -> None:
        head = _git_head_sha(source_dir)
        super().__init__(
            _GiteaSyncProvider(),
            name,
            {
                "ssh_private_key": ssh_private_key,
                "ssh_host_public_key": ssh_host_public_key,
                "ssh_host": ssh_host,
                "ssh_host_alias": ssh_host_alias,
                "ssh_port": ssh_port,
                "owner": owner,
                "repo_name": repo_name,
                "default_branch": default_branch,
                "source_dir": source_dir,
                "head_sha": head,
                "pushed": None,
                "triggers": triggers or {},
            },
            opts,
        )
