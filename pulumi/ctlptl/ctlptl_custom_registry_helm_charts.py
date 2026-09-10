# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Build Slinky Helm charts from a Git source ref into a ctlptl registry."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import (
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
    UpdateResult,
)

from ctlptl import ctlptl_custom_registry_oci_object as oci_object

_CHART_NAMES = ("slurm-operator-crds", "slurm-operator", "slurm")


def _resolve_source_commit(source_path: str, source_ref: str) -> str:
    return oci_object.resolve_source_commit(source_path, source_ref)


def _chart_version(source_commit: str) -> str:
    return f"0.0.0-source{source_commit[:12]}"


def _charts_exist(registry_port: int, chart_version: str) -> bool:
    return all(
        oci_object.manifest_exists(
            registry_port,
            f"charts/{chart_name}",
            chart_version,
        )
        for chart_name in _CHART_NAMES
    )


def _build_and_push_charts(
    *,
    source_path: str,
    source_commit: str,
    registry_port: int,
    chart_version: str,
) -> None:
    helm = oci_object.require_binary("helm")
    oci_object.require_binary("make")
    with oci_object.detached_worktree(
        source_path,
        source_commit,
        prefix="ca4s-slinky-charts-",
    ) as worktree:
        output_dir = Path(worktree) / "dist"
        oci_object.run(
            ["make", f"VERSION={chart_version}", "version-match"],
            cwd=worktree,
        )
        output_dir.mkdir()
        for chart_name in _CHART_NAMES:
            oci_object.run(
                [
                    helm,
                    "package",
                    "--dependency-update",
                    "--destination",
                    str(output_dir),
                    f"helm/{chart_name}",
                ],
                cwd=worktree,
            )
        for chart_name in _CHART_NAMES:
            archive = output_dir / f"{chart_name}-{chart_version}.tgz"
            oci_object.run(
                [
                    helm,
                    "push",
                    str(archive),
                    f"oci://localhost:{registry_port}/charts",
                    "--plain-http",
                ],
                cwd=worktree,
            )


def _ensure_charts(props: dict) -> dict[str, object]:
    source_ref = oci_object.required_str(props, "source_ref")
    registry_name = oci_object.required_str(props, "registry_name")
    registry_port = oci_object.required_int(props, "registry_port")
    with oci_object.source_repository(
        props,
        resolve_commit=_resolve_source_commit,
    ) as (
        repository_path,
        source_commit,
        source_path,
        repository_url,
    ):
        chart_version = _chart_version(source_commit)
        built = False
        if not _charts_exist(registry_port, chart_version):
            _build_and_push_charts(
                source_path=repository_path,
                source_commit=source_commit,
                registry_port=registry_port,
                chart_version=chart_version,
            )
            built = True

    return {
        "source_path": source_path,
        "repository_url": repository_url,
        "source_ref": source_ref,
        "source_commit": source_commit,
        "registry_name": registry_name,
        "registry_port": registry_port,
        "chart_version": chart_version,
        "built": built,
    }


class _CtlptlCustomRegistryHelmChartsProvider(ResourceProvider):
    def create(self, props: dict) -> CreateResult:
        outs = _ensure_charts(props)
        resource_id = (
            f"{outs['registry_name']}:5000/charts/slinky:{outs['chart_version']}"
        )
        return CreateResult(id_=resource_id, outs=outs)

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        keys = (
            "source_path",
            "repository_url",
            "source_ref",
            "registry_name",
            "registry_port",
        )
        return DiffResult(changes=oci_object.has_diff(olds, news, keys))

    def update(self, id_: str, olds: dict, news: dict) -> UpdateResult:
        return UpdateResult(outs=_ensure_charts(news))

    def read(self, id_: str, props: dict) -> ReadResult:
        try:
            registry_port = oci_object.required_int(props, "registry_port")
            source_commit = oci_object.source_commit_for_read(
                props,
                resolve_commit=_resolve_source_commit,
            )
            chart_version = _chart_version(source_commit)
            if not _charts_exist(registry_port, chart_version):
                return ReadResult(id_=None, outs={})
        except Exception as exc:
            print(
                f"failed to refresh ctlptl Slinky charts {id_!r}: {exc}",
                file=sys.stderr,
            )
            return ReadResult(id_=id_, outs=props)
        return ReadResult(
            id_=id_,
            outs={
                **props,
                "source_commit": source_commit,
                "chart_version": chart_version,
            },
        )


class CtlptlCustomRegistryHelmCharts(Resource):
    """Package and push Slinky charts from a Git source ref."""

    source_path: Output[str | None]
    repository_url: Output[str | None]
    source_ref: Output[str]
    source_commit: Output[str]
    registry_name: Output[str]
    registry_port: Output[int]
    chart_version: Output[str]
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
        opts: Optional[ResourceOptions] = None,
    ) -> None:
        super().__init__(
            _CtlptlCustomRegistryHelmChartsProvider(),
            name,
            {
                "source_path": source_path,
                "repository_url": repository_url,
                "source_ref": source_ref,
                "registry_name": registry_name,
                "registry_port": registry_port,
                "source_commit": None,
                "chart_version": None,
                "built": None,
            },
            opts,
        )
