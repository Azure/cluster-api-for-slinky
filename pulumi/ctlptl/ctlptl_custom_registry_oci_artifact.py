# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Pulumi dynamic resource: build a CAPZ provider artifact into a local registry."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

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


def _manifest_exists(registry_port: int, artifact_name: str, artifact_tag: str) -> bool:
    return oci_object.manifest_exists(registry_port, artifact_name, artifact_tag)


def _build_and_push_artifact(
    *,
    source_path: str,
    source_commit: str,
    host_artifact_ref: str,
    artifact_files: list[str],
) -> None:
    oci_object.require_binary("make")
    oci_object.require_binary("oras")
    with oci_object.detached_worktree(
        source_path,
        source_commit,
        prefix="ca4s-capz-artifact-",
    ) as worktree:
        oci_object.run(
            ["make", "release-manifests", "release-metadata"],
            cwd=worktree,
        )
        out_dir = Path(worktree) / "out"
        missing = [item for item in artifact_files if not (out_dir / item).is_file()]
        if missing:
            raise RuntimeError(f"CAPZ release artifact generation did not produce {missing!r}")
        oci_object.run(
            ["oras", "push", "--plain-http", host_artifact_ref, *artifact_files],
            cwd=str(out_dir),
        )


def _ensure_artifact(props: dict) -> dict[str, object]:
    artifact_name = _artifact_name_prop(props)
    artifact_files = _artifact_files_prop(props)

    def build(source_path: str, source_commit: str, host_artifact_ref: str) -> None:
        _build_and_push_artifact(
            source_path=source_path,
            source_commit=source_commit,
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
            "repository_url",
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
            registry_port = oci_object.required_int(props, "registry_port")
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
        return ReadResult(
            id_=id_,
            outs={
                **props,
                "source_commit": source_commit,
                "artifact_tag": artifact_tag,
            },
        )


class CtlptlCustomRegistryOCIArtifact(Resource):
    """Build and push CAPZ release artifacts as an OCI artifact."""

    source_path: Output[str | None]
    repository_url: Output[str | None]
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
        source_ref: Input[str],
        registry_name: Input[str],
        registry_port: Input[int],
        source_path: Optional[Input[str]] = None,
        repository_url: Optional[Input[str]] = None,
        artifact_name: Optional[Input[str]] = None,
        artifact_files: Optional[Input[list[Input[str]]]] = None,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            _CtlptlCustomRegistryOCIArtifactProvider(),
            name,
            {
                "source_path": source_path,
                "repository_url": repository_url,
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