# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Unit tests for shared source-ref OCI object helpers."""

from __future__ import annotations

import subprocess

import pytest

from ctlptl import ctlptl_custom_registry_oci_object as oci_object


_REPOSITORY_URL = "https://github.com/kubernetes-sigs/cluster-api-provider-azure.git"
_SOURCE_REF = "69ec3a40a818ccbc32b8ce88c84609404d8cb7a2"


def test_remote_source_repository_fetches_requested_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    removed: list[str] = []
    repository_path = "/tmp/ca4s-source"
    monkeypatch.setattr(oci_object, "require_binary", lambda name: f"/bin/{name}")
    monkeypatch.setattr(oci_object.tempfile, "mkdtemp", lambda prefix: repository_path)
    monkeypatch.setattr(
        oci_object.shutil,
        "rmtree",
        lambda path, ignore_errors=False: removed.append(path),
    )

    def fake_run(
        cmd: list[str],
        *,
        cwd: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        stdout = f"{_SOURCE_REF}\n" if cmd[-1] == "FETCH_HEAD^{commit}" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(oci_object, "run", fake_run)

    with oci_object.remote_source_repository(_REPOSITORY_URL, _SOURCE_REF) as source:
        assert source == (repository_path, _SOURCE_REF)

    assert calls == [
        ["git", "init", "--bare", repository_path],
        [
            "git",
            "-C",
            repository_path,
            "fetch",
            "--depth=1",
            _REPOSITORY_URL,
            _SOURCE_REF,
        ],
        ["git", "-C", repository_path, "rev-parse", "FETCH_HEAD^{commit}"],
    ]
    assert removed == [repository_path]


def test_detached_worktree_adds_and_removes_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bool]] = []
    removed: list[str] = []
    monkeypatch.setattr(oci_object, "require_binary", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        oci_object.tempfile,
        "mkdtemp",
        lambda prefix: "/tmp/ca4s-worktree",
    )
    monkeypatch.setattr(
        oci_object,
        "run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs.get("check", True))),
    )
    monkeypatch.setattr(
        oci_object.shutil,
        "rmtree",
        lambda path, ignore_errors=False: removed.append(path),
    )

    with oci_object.detached_worktree(
        "/src/repository",
        _SOURCE_REF,
    ) as worktree:
        assert worktree == "/tmp/ca4s-worktree"

    assert calls == [
        (
            [
                "git",
                "-C",
                "/src/repository",
                "worktree",
                "add",
                "--detach",
                "/tmp/ca4s-worktree",
                _SOURCE_REF,
            ],
            True,
        ),
        (
            [
                "git",
                "-C",
                "/src/repository",
                "worktree",
                "remove",
                "--force",
                "/tmp/ca4s-worktree",
            ],
            False,
        ),
    ]
    assert removed == ["/tmp/ca4s-worktree"]