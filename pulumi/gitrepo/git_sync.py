# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Pulumi dynamic resource that pushes a local Git commit to an SSH remote.

``GitSync`` hydrates a Git remote from the local working tree that Pulumi is
running from. It is intentionally small and imperative: the repo already exists,
the auth material is supplied by the caller, and this resource only performs a
non-force ``git push HEAD:refs/heads/<branch>`` when its replacement inputs
change.

The canonical remote is the same SSH URL used by Flux ``GitRepository``. For a
local in-cluster Git service, the actual push can use a different host-reachable
SSH endpoint while preserving the canonical host as ``HostKeyAlias`` for strict
host-key verification.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Optional
from urllib.parse import quote, urlsplit

import pulumi
from pulumi.dynamic import (
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
)


_PUSH_RETRY_TIMEOUT_SECONDS = 180
_PUSH_RETRY_INTERVAL_SECONDS = 5
_TRANSIENT_SSH_ERRORS = (
    "kex_exchange_identification",
    "Connection reset",
    "Connection refused",
    "Connection timed out",
    "Connection closed",
    "No route to host",
    "Operation timed out",
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


def _split_ssh_url(repo_url: str):
    parsed = urlsplit(repo_url)
    if parsed.scheme != "ssh":
        raise ValueError(f"GitSync currently supports only ssh:// URLs, got {repo_url!r}")
    if not parsed.hostname:
        raise ValueError(f"GitSync SSH URL must include a hostname, got {repo_url!r}")
    if parsed.password:
        raise ValueError("GitSync SSH URLs with passwords are not supported")
    if not parsed.path or parsed.path == "/":
        raise ValueError(f"GitSync SSH URL must include a repository path, got {repo_url!r}")
    return parsed


class _GitSyncProvider(ResourceProvider):
    """Synchronizes a local Git tree to an SSH remote branch."""

    def _redact(self, text: str, props: dict) -> str:
        secret = props.get("ssh_private_key")
        return text.replace(str(secret), "<redacted>") if secret else text

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
        parsed = _split_ssh_url(str(props["repo_url"]))
        userinfo = f"{quote(parsed.username)}@" if parsed.username else ""
        return (
            f"ssh://{userinfo}{props['ssh_host']}:{int(props['ssh_port'])}"
            f"{parsed.path}"
        )

    def _git_env(self) -> dict[str, str]:
        return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def _known_hosts(self, props: dict) -> str:
        parsed = _split_ssh_url(str(props["repo_url"]))
        return f"{parsed.hostname} {str(props['ssh_host_public_key']).strip()}\n"

    def _is_transient_push_error(self, stderr: str) -> bool:
        return any(error in stderr for error in _TRANSIENT_SSH_ERRORS)

    def _push_once(self, props: dict) -> None:
        with (
            self._temp_file(str(props["ssh_private_key"]), 0o600) as key_path,
            self._temp_file(self._known_hosts(props), 0o644) as known_hosts_path,
        ):
            parsed = _split_ssh_url(str(props["repo_url"]))
            host_alias = parsed.hostname
            if host_alias is None:
                raise ValueError(
                    "GitSync SSH URL must include a hostname, "
                    f"got {props['repo_url']!r}"
                )
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
                    f"HostKeyAlias={shlex.quote(host_alias)}",
                ]
            )
            subprocess.run(
                [
                    "git",
                    "push",
                    self._build_git_url(props),
                    f"HEAD:refs/heads/{props['repo_branch']}",
                ],
                cwd=props["source_dir"],
                check=True,
                env={**self._git_env(), "GIT_SSH_COMMAND": ssh_command},
                stderr=subprocess.PIPE,
                text=True,
            )

    def _push(self, props: dict) -> None:
        deadline = time.monotonic() + _PUSH_RETRY_TIMEOUT_SECONDS
        last_error: subprocess.CalledProcessError | None = None

        while True:
            try:
                self._push_once(props)
                return
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr or ""
                if not self._is_transient_push_error(stderr):
                    raise
                last_error = exc
                if time.monotonic() >= deadline:
                    raise last_error
                time.sleep(_PUSH_RETRY_INTERVAL_SECONDS)

    def create(self, props: dict) -> CreateResult:
        head = _git_head_sha(props["source_dir"])
        try:
            self._push(props)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "git push failed without --force. The remote branch may have diverged: "
                f"{self._redact((exc.stderr or '').strip(), props)[:500]}"
            ) from exc

        return CreateResult(
            id_=f"{props['repo_url']}@{head}",
            outs={**props, "head_sha": head, "pushed": True},
        )

    def diff(self, _id: str, old: dict, new: dict) -> DiffResult:
        replaces = []
        for key in (
            "repo_url",
            "repo_branch",
            "source_dir",
            "ssh_host",
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


class GitSync(Resource):
    """Keeps an SSH Git remote branch aligned with local ``HEAD``."""

    head_sha: pulumi.Output[str]
    pushed: pulumi.Output[bool]
    repo_url: pulumi.Output[str]
    repo_branch: pulumi.Output[str]
    source_dir: pulumi.Output[str]
    triggers: pulumi.Output[dict[str, object]]

    def __init__(
        self,
        name: str,
        *,
        repo_url: pulumi.Input[str],
        repo_branch: pulumi.Input[str],
        ssh_private_key: pulumi.Input[str],
        ssh_host_public_key: pulumi.Input[str],
        ssh_host: pulumi.Input[str],
        ssh_port: pulumi.Input[int],
        source_dir: str,
        triggers: Optional[pulumi.Input[dict[str, object]]] = None,
        opts: Optional[pulumi.ResourceOptions] = None,
    ) -> None:
        head = _git_head_sha(source_dir)
        super().__init__(
            _GitSyncProvider(),
            name,
            {
                "repo_url": repo_url,
                "repo_branch": repo_branch,
                "ssh_private_key": ssh_private_key,
                "ssh_host_public_key": ssh_host_public_key,
                "ssh_host": ssh_host,
                "ssh_port": ssh_port,
                "source_dir": source_dir,
                "head_sha": head,
                "pushed": None,
                "triggers": triggers or {},
            },
            opts,
        )