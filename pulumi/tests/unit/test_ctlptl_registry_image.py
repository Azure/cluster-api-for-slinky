"""Unit tests for :mod:`ctlptl.ctlptl_registry_image`."""

from __future__ import annotations

import subprocess

import pytest

from ctlptl import ctlptl_registry_image
from ctlptl.ctlptl_registry_image import CtlptlRegistryImage  # noqa: F401


_SOURCE_COMMIT = "1234567890abcdef1234567890abcdef12345678"


@pytest.fixture
def provider(
    monkeypatch: pytest.MonkeyPatch,
) -> ctlptl_registry_image._CtlptlRegistryImageProvider:
    monkeypatch.setattr(ctlptl_registry_image.shutil, "which", lambda name: f"/bin/{name}")
    return ctlptl_registry_image._CtlptlRegistryImageProvider()


def _props() -> dict[str, object]:
    return {
        "source_path": "/src/capz",
        "source_ref": "arsdragonfly/md-vmss",
        "registry_name": "registry-test",
        "registry_port": 5002,
        "image_name": "capz/controller",
    }


def test_create_skips_build_when_source_image_exists(
    provider: ctlptl_registry_image._CtlptlRegistryImageProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ctlptl_registry_image,
        "_resolve_source_commit",
        lambda source_path, source_ref: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(ctlptl_registry_image, "_manifest_exists", lambda *args: True)

    def fail_build(**kwargs: object) -> None:
        raise AssertionError("existing image must not be rebuilt")

    monkeypatch.setattr(ctlptl_registry_image, "_build_and_push_image", fail_build)

    result = provider.create(_props())

    assert result.id == "registry-test:5000/capz/controller:source-1234567890ab"
    assert result.outs is not None
    assert result.outs["built"] is False
    assert result.outs["source_commit"] == _SOURCE_COMMIT
    assert result.outs["host_image_ref"] == "localhost:5002/capz/controller:source-1234567890ab"
    assert result.outs["image_ref"] == "registry-test:5000/capz/controller:source-1234567890ab"


def test_create_builds_and_pushes_when_source_image_is_missing(
    provider: ctlptl_registry_image._CtlptlRegistryImageProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[dict[str, object]] = []
    monkeypatch.setattr(
        ctlptl_registry_image,
        "_resolve_source_commit",
        lambda source_path, source_ref: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(ctlptl_registry_image, "_manifest_exists", lambda *args: False)
    monkeypatch.setattr(
        ctlptl_registry_image,
        "_build_and_push_image",
        lambda **kwargs: builds.append(kwargs),
    )

    result = provider.create(_props())

    assert result.outs is not None
    assert result.outs["built"] is True
    assert builds == [
        {
            "source_path": "/src/capz",
            "source_ref": "arsdragonfly/md-vmss",
            "host_image_ref": "localhost:5002/capz/controller:source-1234567890ab",
            "build_args": {"ARCH": "amd64"},
        }
    ]


def test_create_accepts_pulumi_integer_values_deserialized_as_float(
    provider: ctlptl_registry_image._CtlptlRegistryImageProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ctlptl_registry_image,
        "_resolve_source_commit",
        lambda source_path, source_ref: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(ctlptl_registry_image, "_manifest_exists", lambda *args: True)
    props = _props()
    props["registry_port"] = 5002.0

    result = provider.create(props)

    assert result.outs is not None
    assert result.outs["registry_port"] == 5002
    assert result.outs["host_image_ref"] == "localhost:5002/capz/controller:source-1234567890ab"


def test_build_and_push_uses_detached_git_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    removed: list[str] = []
    monkeypatch.setattr(ctlptl_registry_image.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(ctlptl_registry_image.tempfile, "mkdtemp", lambda prefix: "/tmp/worktree")
    monkeypatch.setattr(
        ctlptl_registry_image.shutil,
        "rmtree",
        lambda path, ignore_errors=False: removed.append(path),
    )

    def fake_run(
        cmd: list[str],
        *,
        input: str | None = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ctlptl_registry_image.subprocess, "run", fake_run)

    ctlptl_registry_image._build_and_push_image(
        source_path="/src/capz",
        source_ref="feature",
        host_image_ref="localhost:5002/capz/controller:source-1234567890ab",
        build_args={"ARCH": "amd64"},
    )

    assert calls == [
        ["git", "-C", "/src/capz", "worktree", "add", "--detach", "/tmp/worktree", "feature"],
        [
            "docker",
            "build",
            "--build-arg",
            "ARCH=amd64",
            "-t",
            "localhost:5002/capz/controller:source-1234567890ab",
            "/tmp/worktree",
        ],
        ["docker", "push", "localhost:5002/capz/controller:source-1234567890ab"],
        ["git", "-C", "/src/capz", "worktree", "remove", "--force", "/tmp/worktree"],
    ]
    assert removed == ["/tmp/worktree"]