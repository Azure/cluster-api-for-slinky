"""Unit tests for :mod:`ctlptl.ctlptl_custom_registry_oci_artifact`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ctlptl import ctlptl_custom_registry_oci_artifact
from ctlptl.ctlptl_custom_registry_oci_artifact import CtlptlCustomRegistryOCIArtifact  # noqa: F401


_SOURCE_COMMIT = "1234567890abcdef1234567890abcdef12345678"


@pytest.fixture
def provider(
    monkeypatch: pytest.MonkeyPatch,
) -> ctlptl_custom_registry_oci_artifact._CtlptlCustomRegistryOCIArtifactProvider:
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact.shutil,
        "which",
        lambda name: f"/bin/{name}",
    )
    return ctlptl_custom_registry_oci_artifact._CtlptlCustomRegistryOCIArtifactProvider()


def _props() -> dict[str, object]:
    return {
        "source_path": "/src/capz",
        "source_ref": "origin/arsdragonfly/md-vmss",
        "registry_name": "custom-registry",
        "registry_port": 5002,
        "artifact_name": "capz/cluster-api-provider-azure",
    }


def test_create_skips_build_when_source_artifact_exists(
    provider: ctlptl_custom_registry_oci_artifact._CtlptlCustomRegistryOCIArtifactProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact,
        "_resolve_source_commit",
        lambda source_path, source_ref: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact,
        "_manifest_exists",
        lambda *args: True,
    )

    def fail_build(**kwargs: object) -> None:
        raise AssertionError("existing artifact must not be rebuilt")

    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact,
        "_build_and_push_artifact",
        fail_build,
    )

    result = provider.create(_props())

    assert result.id == "custom-registry:5000/capz/cluster-api-provider-azure:source-1234567890ab"
    assert result.outs is not None
    assert result.outs["built"] is False
    assert result.outs["source_commit"] == _SOURCE_COMMIT
    assert result.outs["artifact_files"] == [
        "metadata.yaml",
        "infrastructure-components.yaml",
    ]
    assert result.outs["host_artifact_ref"] == (
        "localhost:5002/capz/cluster-api-provider-azure:source-1234567890ab"
    )
    assert result.outs["artifact_ref"] == (
        "custom-registry:5000/capz/cluster-api-provider-azure:source-1234567890ab"
    )


def test_create_builds_and_pushes_when_source_artifact_is_missing(
    provider: ctlptl_custom_registry_oci_artifact._CtlptlCustomRegistryOCIArtifactProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[dict[str, object]] = []
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact,
        "_resolve_source_commit",
        lambda source_path, source_ref: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact,
        "_manifest_exists",
        lambda *args: False,
    )
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact,
        "_build_and_push_artifact",
        lambda **kwargs: builds.append(kwargs),
    )

    result = provider.create(_props())

    assert result.outs is not None
    assert result.outs["built"] is True
    assert builds == [
        {
            "source_path": "/src/capz",
            "source_ref": "origin/arsdragonfly/md-vmss",
            "host_artifact_ref": (
                "localhost:5002/capz/cluster-api-provider-azure:source-1234567890ab"
            ),
            "artifact_files": ["metadata.yaml", "infrastructure-components.yaml"],
        }
    ]


def test_create_accepts_pulumi_integer_values_deserialized_as_float(
    provider: ctlptl_custom_registry_oci_artifact._CtlptlCustomRegistryOCIArtifactProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact,
        "_resolve_source_commit",
        lambda source_path, source_ref: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact,
        "_manifest_exists",
        lambda *args: True,
    )
    props = _props()
    props["registry_port"] = 5002.0

    result = provider.create(props)

    assert result.outs is not None
    assert result.outs["registry_port"] == 5002
    assert result.outs["host_artifact_ref"] == (
        "localhost:5002/capz/cluster-api-provider-azure:source-1234567890ab"
    )


def test_build_and_push_uses_capz_release_targets_and_oras(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], str | None]] = []
    removed: list[str] = []
    worktree = tmp_path / "worktree"

    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact.shutil,
        "which",
        lambda name: f"/bin/{name}",
    )
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact.tempfile,
        "mkdtemp",
        lambda prefix: str(worktree),
    )
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact.shutil,
        "rmtree",
        lambda path, ignore_errors=False: removed.append(path),
    )

    def fake_run(
        cmd: list[str],
        *,
        cwd: str | None = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        calls.append((cmd, cwd))
        if cmd == ["make", "release-manifests", "release-metadata"]:
            out_dir = worktree / "out"
            out_dir.mkdir(parents=True)
            (out_dir / "metadata.yaml").write_text("metadata", encoding="utf-8")
            (out_dir / "infrastructure-components.yaml").write_text(
                "components",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ctlptl_custom_registry_oci_artifact.subprocess, "run", fake_run)

    ctlptl_custom_registry_oci_artifact._build_and_push_artifact(
        source_path="/src/capz",
        source_ref="feature",
        host_artifact_ref="localhost:5002/capz/cluster-api-provider-azure:source-1234567890ab",
        artifact_files=["metadata.yaml", "infrastructure-components.yaml"],
    )

    assert calls == [
        (
            ["git", "-C", "/src/capz", "worktree", "add", "--detach", str(worktree), "feature"],
            None,
        ),
        (["make", "release-manifests", "release-metadata"], str(worktree)),
        (
            [
                "oras",
                "push",
                "--plain-http",
                "localhost:5002/capz/cluster-api-provider-azure:source-1234567890ab",
                "metadata.yaml",
                "infrastructure-components.yaml",
            ],
            str(worktree / "out"),
        ),
        (
            ["git", "-C", "/src/capz", "worktree", "remove", "--force", str(worktree)],
            None,
        ),
    ]
    assert removed == [str(worktree)]


def test_build_and_push_requires_generated_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact.shutil,
        "which",
        lambda name: f"/bin/{name}",
    )
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact.tempfile,
        "mkdtemp",
        lambda prefix: str(worktree),
    )
    monkeypatch.setattr(
        ctlptl_custom_registry_oci_artifact.shutil,
        "rmtree",
        lambda path, ignore_errors=False: None,
    )

    def fake_run(
        cmd: list[str],
        *,
        cwd: str | None = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        if cmd == ["make", "release-manifests", "release-metadata"]:
            (worktree / "out").mkdir(parents=True)
            (worktree / "out" / "metadata.yaml").write_text("metadata", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ctlptl_custom_registry_oci_artifact.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="infrastructure-components.yaml"):
        ctlptl_custom_registry_oci_artifact._build_and_push_artifact(
            source_path="/src/capz",
            source_ref="feature",
            host_artifact_ref="localhost:5002/capz/cluster-api-provider-azure:source-1234567890ab",
            artifact_files=["metadata.yaml", "infrastructure-components.yaml"],
        )