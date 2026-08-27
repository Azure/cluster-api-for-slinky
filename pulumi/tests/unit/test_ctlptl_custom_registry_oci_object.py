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