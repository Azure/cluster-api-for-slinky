"""Pulumi dynamic resource: build a CAPZ OCI artifact into a local registry."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import (
    CheckResult,
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
    UpdateResult,
)

from ctlptl import ctlptl_custom_registry_oci_object as oci_object

_DEFAULT_ARTIFACT_NAME = "capz/cluster-api-provider-azure"
_DEFAULT_FILES = ("metadata.yaml", "infrastructure-components.yaml")


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"required binary '{name}' not found in PATH; install it before running pulumi"
        )
    return path


def _run(
    cmd: List[str],
    *,
    cwd: Optional[str] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command {cmd!r} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _required_str(props: dict, name: str) -> str:
    return oci_object.required_str(props, name)


def _required_int(props: dict, name: str) -> int:
    return oci_object.required_int(props, name)


def _artifact_name_prop(props: dict) -> str:
    value = props.get("artifact_name") or _DEFAULT_ARTIFACT_NAME
    if not isinstance(value, str) or not value:
        raise RuntimeError("artifact_name must be a non-empty string")
    return value


def _artifact_files_prop(props: dict) -> list[str]:
    value = props.get("artifact_files")
    if value is None:
        return list(_DEFAULT_FILES)
    if isinstance(value, str) or not isinstance(value, list):
        raise RuntimeError("artifact_files must be a list of non-empty strings")
    files = [str(item) for item in value]
    if not files or any(not item for item in files):
        raise RuntimeError("artifact_files must contain at least one non-empty string")
    return files


def _resolve_source_commit(source_path: str, source_ref: str) -> str:
    return oci_object.resolve_source_commit(source_path, source_ref)


def _artifact_tag(source_commit: str) -> str:
    return oci_object.source_tag(source_commit)


def _host_artifact_ref(registry_port: int, artifact_name: str, artifact_tag: str) -> str:
    return oci_object.host_ref(registry_port, artifact_name, artifact_tag)


def _cluster_artifact_ref(registry_name: str, artifact_name: str, artifact_tag: str) -> str:
    return oci_object.cluster_ref(registry_name, artifact_name, artifact_tag)


def _manifest_exists(registry_port: int, artifact_name: str, artifact_tag: str) -> bool:
    return oci_object.manifest_exists(registry_port, artifact_name, artifact_tag)


def _build_and_push_artifact(
    *,
    source_path: str,
    source_ref: str,
    host_artifact_ref: str,
    artifact_files: list[str],
) -> None:
    _require_binary("git")
    _require_binary("make")
    _require_binary("oras")
    worktree = tempfile.mkdtemp(prefix="ca4s-artifact-")
    try:
        _run(["git", "-C", source_path, "worktree", "add", "--detach", worktree, source_ref])
        _run(["make", "release-manifests", "release-metadata"], cwd=worktree)
        out_dir = Path(worktree) / "out"
        missing = [item for item in artifact_files if not (out_dir / item).is_file()]
        if missing:
            raise RuntimeError(f"CAPZ release artifact generation did not produce {missing!r}")
        _run(["oras", "push", "--plain-http", host_artifact_ref, *artifact_files], cwd=str(out_dir))
    finally:
        _run(["git", "-C", source_path, "worktree", "remove", "--force", worktree], check=False)
        shutil.rmtree(worktree, ignore_errors=True)


def _ensure_artifact(props: dict) -> dict[str, object]:
    artifact_name = _artifact_name_prop(props)
    artifact_files = _artifact_files_prop(props)

    def build(source_path: str, source_ref: str, host_artifact_ref: str) -> None:
        _build_and_push_artifact(
            source_path=source_path,
            source_ref=source_ref,
            host_artifact_ref=host_artifact_ref,
            artifact_files=artifact_files,
        )

    return oci_object.ensure_source_ref_object(
        props,
        object_name=artifact_name,
        object_name_key="artifact_name",
        object_tag_key="artifact_tag",
        host_ref_key="host_artifact_ref",
        cluster_ref_key="artifact_ref",
        extra_outputs={"artifact_files": artifact_files},
        build=build,
        resolve_commit=_resolve_source_commit,
        probe_manifest=_manifest_exists,
    )


class _CtlptlCustomRegistryOCIArtifactProvider(ResourceProvider):
    """Lifecycle hooks for a CAPZ OCI artifact built into a local registry."""

    def check(self, olds: dict, news: dict) -> CheckResult:
        checked = dict(news)
        if checked.get("artifact_name") is None:
            checked["artifact_name"] = _DEFAULT_ARTIFACT_NAME
        if checked.get("artifact_files") is None:
            checked["artifact_files"] = list(_DEFAULT_FILES)
        return CheckResult(inputs=checked, failures=[])

    def create(self, props: dict) -> CreateResult:
        outs = _ensure_artifact(props)
        return CreateResult(
            id_=str(outs["artifact_ref"]),
            outs=outs,
        )

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        keys = (
            "source_path",
            "source_ref",
            "registry_name",
            "registry_port",
            "artifact_name",
            "artifact_files",
        )
        return DiffResult(changes=oci_object.has_diff(olds, news, keys))

    def update(self, id_: str, olds: dict, news: dict) -> UpdateResult:
        return UpdateResult(outs=_ensure_artifact(news))

    def read(self, id_: str, props: dict) -> ReadResult:
        try:
            registry_port = _required_int(props, "registry_port")
            artifact_name = _artifact_name_prop(props)
            source_commit = oci_object.source_commit_for_read(
                props,
                resolve_commit=_resolve_source_commit,
            )
            artifact_tag = _artifact_tag(source_commit)
            if not _manifest_exists(registry_port, artifact_name, artifact_tag):
                return ReadResult(id_=None, outs={})
        except Exception as exc:
            print(f"failed to refresh ctlptl OCI artifact {id_!r}: {exc}", file=sys.stderr)
            return ReadResult(id_=id_, outs=props)
        return ReadResult(id_=id_, outs=props)


class CtlptlCustomRegistryOCIArtifact(Resource):
    """Build and push CAPZ release artifacts as an OCI artifact."""

    source_ref: Output[str]
    source_commit: Output[str]
    artifact_name: Output[str]
    artifact_tag: Output[str]
    artifact_files: Output[list[str]]
    host_artifact_ref: Output[str]
    artifact_ref: Output[str]
    built: Output[bool]

    def __init__(
        self,
        name: str,
        *,
        source_path: Input[str],
        source_ref: Input[str],
        registry_name: Input[str],
        registry_port: Input[int],
        artifact_name: Optional[Input[str]] = None,
        artifact_files: Optional[Input[list[Input[str]]]] = None,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            _CtlptlCustomRegistryOCIArtifactProvider(),
            name,
            {
                "source_path": source_path,
                "source_ref": source_ref,
                "registry_name": registry_name,
                "registry_port": registry_port,
                "artifact_name": artifact_name,
                "artifact_files": artifact_files,
                "source_commit": None,
                "artifact_tag": None,
                "host_artifact_ref": None,
                "artifact_ref": None,
                "built": None,
            },
            opts,
        )