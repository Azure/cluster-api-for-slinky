"""Pulumi dynamic resource: build a custom image into a local ctlptl registry."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
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

_DEFAULT_IMAGE_NAME = "capz/cluster-api-azure-controller"
_DEFAULT_BUILD_ARGS = {"ARCH": "amd64"}


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
    stdin: Optional[str] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        input=stdin,
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


def _image_name_prop(props: dict) -> str:
    value = props.get("image_name") or _DEFAULT_IMAGE_NAME
    if not isinstance(value, str) or not value:
        raise RuntimeError("image_name must be a non-empty string")
    return value


def _build_args_prop(props: dict) -> dict[str, str]:
    value = props.get("build_args")
    if value is None:
        return dict(_DEFAULT_BUILD_ARGS)
    if not isinstance(value, dict):
        raise RuntimeError("build_args must be an object of strings")
    return {str(key): str(item) for key, item in value.items()}


def _resolve_source_commit(source_path: str, source_ref: str) -> str:
    return oci_object.resolve_source_commit(source_path, source_ref)


def _image_tag(source_commit: str) -> str:
    return oci_object.source_tag(source_commit)


def _host_image_ref(registry_port: int, image_name: str, image_tag: str) -> str:
    return oci_object.host_ref(registry_port, image_name, image_tag)


def _cluster_image_ref(registry_name: str, image_name: str, image_tag: str) -> str:
    return oci_object.cluster_ref(registry_name, image_name, image_tag)


def _manifest_exists(registry_port: int, image_name: str, image_tag: str) -> bool:
    return oci_object.manifest_exists(registry_port, image_name, image_tag)


def _build_and_push_image(
    *,
    source_path: str,
    source_commit: str,
    host_image_ref: str,
    build_args: dict[str, str],
) -> None:
    _require_binary("docker")
    _require_binary("git")
    worktree = tempfile.mkdtemp(prefix="ca4s-image-")
    try:
        _run(
            ["git", "-C", source_path, "worktree", "add", "--detach", worktree, source_commit]
        )
        build_cmd = [
            "docker",
            "build",
        ]
        for key, value in sorted(build_args.items()):
            build_cmd.extend(["--build-arg", f"{key}={value}"])
        build_cmd.extend(["-t", host_image_ref, worktree])
        _run(build_cmd)
        _run(["docker", "push", host_image_ref])
    finally:
        _run(["git", "-C", source_path, "worktree", "remove", "--force", worktree], check=False)
        shutil.rmtree(worktree, ignore_errors=True)


def _ensure_image(props: dict) -> dict[str, object]:
    image_name = _image_name_prop(props)
    build_args = _build_args_prop(props)

    def build(source_path: str, source_commit: str, host_image_ref: str) -> None:
        _build_and_push_image(
            source_path=source_path,
            source_commit=source_commit,
            host_image_ref=host_image_ref,
            build_args=build_args,
        )

    return oci_object.ensure_source_ref_object(
        props,
        object_name=image_name,
        object_name_key="image_name",
        object_tag_key="image_tag",
        host_ref_key="host_image_ref",
        cluster_ref_key="image_ref",
        extra_outputs={"build_args": build_args},
        build=build,
        resolve_commit=_resolve_source_commit,
        probe_manifest=_manifest_exists,
    )


class _CtlptlCustomRegistryImageProvider(ResourceProvider):
    """Lifecycle hooks for a custom image built into a local ctlptl registry."""

    def check(self, olds: dict, news: dict) -> CheckResult:
        checked = dict(news)
        if checked.get("image_name") is None:
            checked["image_name"] = _DEFAULT_IMAGE_NAME
        if checked.get("build_args") is None:
            checked["build_args"] = dict(_DEFAULT_BUILD_ARGS)
        return CheckResult(inputs=checked, failures=[])

    def create(self, props: dict) -> CreateResult:
        outs = _ensure_image(props)
        return CreateResult(
            id_=str(outs["image_ref"]),
            outs=outs,
        )

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        keys = (
            "source_path",
            "repository_url",
            "source_ref",
            "registry_name",
            "registry_port",
            "image_name",
            "build_args",
        )
        return DiffResult(changes=oci_object.has_diff(olds, news, keys))

    def update(self, id_: str, olds: dict, news: dict) -> UpdateResult:
        return UpdateResult(outs=_ensure_image(news))

    def read(self, id_: str, props: dict) -> ReadResult:
        try:
            registry_port = _required_int(props, "registry_port")
            image_name = _image_name_prop(props)
            source_commit = oci_object.source_commit_for_read(
                props,
                resolve_commit=_resolve_source_commit,
            )
            image_tag = _image_tag(source_commit)
            if not _manifest_exists(registry_port, image_name, image_tag):
                return ReadResult(id_=None, outs={})
        except Exception as exc:
            print(f"failed to refresh ctlptl registry image {id_!r}: {exc}", file=sys.stderr)
            return ReadResult(id_=id_, outs=props)
        return ReadResult(
            id_=id_,
            outs={**props, "source_commit": source_commit, "image_tag": image_tag},
        )


class CtlptlCustomRegistryImage(Resource):
    """Build and push a git source ref image into a local custom registry."""

    source_path: Output[str | None]
    repository_url: Output[str | None]
    source_ref: Output[str]
    source_commit: Output[str]
    image_name: Output[str]
    image_tag: Output[str]
    host_image_ref: Output[str]
    image_ref: Output[str]
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
        image_name: Optional[Input[str]] = None,
        build_args: Optional[Input[dict[str, Input[str]]]] = None,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            _CtlptlCustomRegistryImageProvider(),
            name,
            {
                "source_path": source_path,
                "repository_url": repository_url,
                "source_ref": source_ref,
                "registry_name": registry_name,
                "registry_port": registry_port,
                "image_name": image_name,
                "build_args": build_args,
                "source_commit": None,
                "image_tag": None,
                "host_image_ref": None,
                "image_ref": None,
                "built": None,
            },
            opts,
        )