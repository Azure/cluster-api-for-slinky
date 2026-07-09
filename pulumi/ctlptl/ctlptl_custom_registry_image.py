"""Pulumi dynamic resource: build a custom image into a local ctlptl registry."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
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

_DEFAULT_IMAGE_NAME = "capz/cluster-api-azure-controller"
_DEFAULT_BUILD_ARGS = {"ARCH": "amd64"}
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    )
)


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
    value = props.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} must be a non-empty string")
    return value


def _required_int(props: dict, name: str) -> int:
    value = props.get(name)
    if isinstance(value, bool):
        raise RuntimeError(f"{name} must be a positive integer")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


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
    _require_binary("git")
    result = _run(
        ["git", "-C", source_path, "rev-parse", f"{source_ref}^{{commit}}"]
    )
    commit = result.stdout.strip()
    if not commit:
        raise RuntimeError(f"source_ref {source_ref!r} did not resolve to a commit")
    return commit


def _image_tag(source_commit: str) -> str:
    return f"source-{source_commit[:12]}"


def _host_image_ref(registry_port: int, image_name: str, image_tag: str) -> str:
    return f"localhost:{registry_port}/{image_name}:{image_tag}"


def _cluster_image_ref(registry_name: str, image_name: str, image_tag: str) -> str:
    return f"{registry_name}:5000/{image_name}:{image_tag}"


def _manifest_exists(registry_port: int, image_name: str, image_tag: str) -> bool:
    url = f"http://localhost:{registry_port}/v2/{image_name}/manifests/{image_tag}"
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"Accept": _MANIFEST_ACCEPT},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"registry manifest probe failed for {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"registry manifest probe failed for {url}: {exc}") from exc


def _build_and_push_image(
    *,
    source_path: str,
    source_ref: str,
    host_image_ref: str,
    build_args: dict[str, str],
) -> None:
    _require_binary("docker")
    _require_binary("git")
    worktree = tempfile.mkdtemp(prefix="ca4s-image-")
    try:
        _run(["git", "-C", source_path, "worktree", "add", "--detach", worktree, source_ref])
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
    source_path = _required_str(props, "source_path")
    source_ref = _required_str(props, "source_ref")
    registry_name = _required_str(props, "registry_name")
    registry_port = _required_int(props, "registry_port")
    image_name = _image_name_prop(props)
    build_args = _build_args_prop(props)
    source_commit = _resolve_source_commit(source_path, source_ref)
    image_tag = _image_tag(source_commit)
    host_image_ref = _host_image_ref(registry_port, image_name, image_tag)
    image_ref = _cluster_image_ref(registry_name, image_name, image_tag)

    built = False
    if not _manifest_exists(registry_port, image_name, image_tag):
        _build_and_push_image(
            source_path=source_path,
            source_ref=source_ref,
            host_image_ref=host_image_ref,
            build_args=build_args,
        )
        built = True

    return {
        "source_path": source_path,
        "source_ref": source_ref,
        "source_commit": source_commit,
        "registry_name": registry_name,
        "registry_port": registry_port,
        "image_name": image_name,
        "image_tag": image_tag,
        "host_image_ref": host_image_ref,
        "image_ref": image_ref,
        "build_args": build_args,
        "built": built,
    }


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
            "source_ref",
            "registry_name",
            "registry_port",
            "image_name",
            "build_args",
        )
        return DiffResult(changes=any(olds.get(key) != news.get(key) for key in keys))

    def update(self, id_: str, olds: dict, news: dict) -> UpdateResult:
        return UpdateResult(outs=_ensure_image(news))

    def read(self, id_: str, props: dict) -> ReadResult:
        try:
            registry_port = _required_int(props, "registry_port")
            image_name = _image_name_prop(props)
            source_commit = str(props.get("source_commit") or "")
            if not source_commit:
                source_commit = _resolve_source_commit(
                    _required_str(props, "source_path"),
                    _required_str(props, "source_ref"),
                )
            image_tag = _image_tag(source_commit)
            if not _manifest_exists(registry_port, image_name, image_tag):
                return ReadResult(id_=None, outs={})
        except Exception as exc:
            print(f"failed to refresh ctlptl registry image {id_!r}: {exc}", file=sys.stderr)
            return ReadResult(id_=id_, outs=props)
        return ReadResult(id_=id_, outs=props)


class CtlptlCustomRegistryImage(Resource):
    """Build and push a git source ref image into a local custom registry."""

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
        source_path: Input[str],
        source_ref: Input[str],
        registry_name: Input[str],
        registry_port: Input[int],
        image_name: Optional[Input[str]] = None,
        build_args: Optional[Input[dict[str, Input[str]]]] = None,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            _CtlptlCustomRegistryImageProvider(),
            name,
            {
                "source_path": source_path,
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